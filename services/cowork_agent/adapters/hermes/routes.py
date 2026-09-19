"""
Per-hermes-profile configuration endpoints.

These routes give the FE one URL per editable surface for a single hermes
profile (aria, research, swe, …). They mirror the agent-agnostic
default-scoped routes (``/api/channels/hermes/*``, ``/api/config/hermes``,
``/api/config/hermes/providers/{id}/key``, ``/api/secrets/env``) but write
into ``~/.hermes/profiles/<name>/`` instead of ``~/.hermes/``.

Why these are hermes-only:
- OpenClaw and claude_code don't have a "profile" concept. Their analogues
  (openclaw agent entries; claude_code per-agent dirs) are already covered
  by ``/api/agents/{id}`` PATCH and the bridge routes.
- Hermes profile dirs are fully independent on-disk workspaces, so the
  edits here can be neatly scoped by ``HERMES_HOME=<profile_dir>``.

Why the default profile is rejected:
- ``~/.hermes/.env``, ``~/.hermes/config.yaml``, ``~/.hermes/SOUL.md`` are
  already owned by the default-scoped routes. Letting two URL families
  write the same file leads to inconsistent state — instead, ``default``
  here returns 400 with a hint.

Restart contract:
- Hermes loads config.yaml, .env, SOUL.md, etc. at gateway-process startup.
  Edits made via these routes only take effect after a gateway restart, so
  successful writes set ``restart_required: true`` and the FE prompts.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.registry.agent_registry import get_agent
from services.cowork_agent.adapters.hermes import gateway_pool
from services.cowork_agent.helpers import _mask_sensitive
from services.cowork_agent.adapters.hermes.profile_env import (
    EnvEntry,
    delete_env_entry,
    list_env_keys,
    load_env_entries,
    save_env_entries,
    upsert_env_entry,
)
# Default (non-profile) ~/.hermes/.env writes go through the shared
# AGENT_NAME-resolved helper. These routes mount only when hermes is the
# active agent, so get_active_agent().env_file is ~/.hermes/.env — no
# hermes-pinned env module needed. Aliased to avoid colliding with the
# profile-scoped upsert_env_entry imported above.
from services.cowork_agent.registry.agent_env import upsert_env_entry as upsert_default_env_entry
from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
from services.cowork_agent.adapters.hermes.paths import HERMES_DIR

router = APIRouter()


_PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Allowlist for memory writes. Hermes's built-in memory is anchored to
# USER.md / MEMORY.md; we accept those plus any plain ``*.md`` so users
# can drop notes alongside. No subdirectories — the file must live
# directly under ``<profile>/memories/``.
_MEMORY_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")

_HERMES = get_agent("hermes")


# ── Validation ───────────────────────────────────────────────────────────────


def _resolve_profile(profile: str) -> Path | JSONResponse:
    """Validate the path parameter and return the profile dir, or a 4xx
    JSONResponse the route can return directly. Keeps every handler one
    line shorter and the rejection reasons consistent."""
    if not _PROFILE_RE.match(profile):
        return JSONResponse(
            status_code=400,
            content={"detail": f"invalid hermes profile name: {profile!r}"},
        )
    if profile not in list_all_profile_names():
        return JSONResponse(
            status_code=404,
            content={"detail": f"hermes profile {profile!r} not found"},
        )
    # ``default`` lives at ``~/.hermes/`` (HERMES_DIR), every other profile
    # at ``~/.hermes/profiles/<name>/`` (agents_dir / name).
    profile_dir = HERMES_DIR if profile == "default" else _HERMES.agents_dir / profile
    if not profile_dir.is_dir():
        return JSONResponse(
            status_code=404,
            content={"detail": f"hermes profile dir missing: {profile_dir}"},
        )
    return profile_dir


# ── Subprocess helper ────────────────────────────────────────────────────────


async def _run_cli(profile_dir: Path, argv: list[str], *, timeout_s: float = 30.0) -> tuple[int, str]:
    """Run a ``hermes ...`` CLI command with ``HERMES_HOME=<profile_dir>``.

    Uses stdlib ``subprocess.run`` wrapped in ``asyncio.to_thread`` for the
    same reason ``_run_hermes_sh`` does — uvloop leaks pipe FDs to grandchild
    daemons (e.g. when the CLI spawns a gateway), which can hang
    ``proc.communicate()`` past the timeout. ``close_fds=True``
    + ``start_new_session=True`` cuts that chain.
    """
    import os

    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_dir)

    def _blocking() -> tuple[int, str]:
        try:
            result = subprocess.run(
                argv,
                cwd=str(profile_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                close_fds=True,
                start_new_session=True,
                check=False,
            )
        except FileNotFoundError:
            return -1, "hermes CLI not found on PATH"
        except subprocess.TimeoutExpired:
            return -1, f"hermes CLI timed out after {timeout_s}s"
        except Exception as e:  # noqa: BLE001
            return -1, f"hermes CLI failed: {e!r}"
        output = (result.stderr or result.stdout or "").strip()[:2000]
        return result.returncode, output

    return await asyncio.to_thread(_blocking)


def _gateway_base_for(profile: str) -> str | None:
    """Return the base URL of this profile's gateway if it's currently in
    the pool and listening, else None. Used by the model proxy."""
    for entry in gateway_pool.list_pool():
        if entry.get("profile") != profile:
            continue
        if entry.get("alive") and entry.get("listening"):
            return f"http://127.0.0.1:{int(entry['port'])}"
        return None
    return None


# ── Detail (mirrors GET /api/agents/{id} for hermes) ─────────────────────────


@router.get("/api/agents/hermes/{profile}")
async def hermes_profile_detail(profile: str):
    """Full snapshot for one hermes profile. The data here is the same that
    ``GET /api/agents/{profile}`` returns when the id matches a hermes
    profile — exposed under this path so the FE can fetch a profile by
    name without having to disambiguate from openclaw/claude_code agents
    sharing names."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    # Delegate to the agents-capability detail builder so the response shape
    # stays in lockstep with /api/agents/{id}.
    from services.cowork_agent.adapters.hermes.agents import _detail
    return _detail(profile)


# ── Gateway lifecycle ────────────────────────────────────────────────────────


def _pool_entry(profile: str) -> dict | None:
    for entry in gateway_pool.list_pool():
        if entry.get("profile") == profile:
            return entry
    return None


_DEFAULT_GATEWAY_PORT = 8642


async def _probe_gateway_port(port: int) -> str:
    """Liveness probe for the hermes gateway. Hits /v1/health (which the
    gateway exposes without auth — see api_server.py:_handle_health) so we
    don't conflate "gateway is alive" with "our API key was accepted."
    Auth correctness surfaces separately via the /v1/models proxy used by
    useHermesModels on the FE."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0)) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/v1/health")
        if resp.status_code == 200:
            return "up"
        if resp.status_code in (401, 403):
            return "unauthorized"
        return f"http_{resp.status_code}"
    except httpx.HTTPError:
        return "down"


def _default_gateway_entry() -> dict:
    """Synthesize a pool-shaped entry for the hermes.sh-managed default
    gateway by reading ~/.hermes/gateway.pid (written by hermes.sh)."""
    pid_file = HERMES_DIR / "gateway.pid"
    pid = 0
    started_at: float | None = None
    if pid_file.is_file():
        try:
            raw = pid_file.read_text(errors="replace").strip()
            data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw or 0)}
            pid = int(data.get("pid") or 0)
            try:
                started_at = pid_file.stat().st_mtime
            except OSError:
                started_at = None
        except (ValueError, json.JSONDecodeError, OSError):
            pid = 0

    alive = False
    if pid:
        try:
            import os
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError, OSError):
            alive = False

    return {
        "profile": "default",
        "port": _DEFAULT_GATEWAY_PORT,
        "pid": pid or None,
        "alive": alive,
        "listening": alive,  # treated as listening if pid is alive — probe gives the real answer
        "running": alive,
        "started_at": started_at,
    }


@router.get("/api/agents/hermes/{profile}/gateway")
async def hermes_profile_gateway_status(profile: str):
    """Pool snapshot for this profile + liveness probe.

    Default profile is hermes.sh-managed (not in the pool); we synthesize
    an equivalent entry from ~/.hermes/gateway.pid and probe port 8642.
    Non-default profiles are pool-managed.

    The ``probe`` field tries ``GET /v1/health`` on the gateway's
    port (auth-free liveness check): ``up`` (200), ``down``
    (connection refused / timeout). ``unauthorized`` is kept as a
    defensive code path in case the endpoint ever moves behind auth."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    if profile == "default":
        entry = _default_gateway_entry()
        probe = await _probe_gateway_port(_DEFAULT_GATEWAY_PORT) if entry["alive"] else "down"
        return {"profile": profile, "managed_by": "hermes.sh", "entry": entry, "probe": probe}

    entry = _pool_entry(profile) or {"profile": profile, "running": False}
    probe = "unknown"
    if entry.get("listening"):
        port = int(entry.get("port") or 0)
        if port:
            probe = await _probe_gateway_port(port)
    elif "running" in entry:
        probe = "down"

    return {
        "profile": profile,
        "managed_by": "pool",
        "entry": entry,
        "probe": probe,
    }


async def _run_default_hermes_sh(subcommand: str, timeout_s: float) -> tuple[int, str]:
    """Delegate default-profile gateway lifecycle to hermes.sh (which is what
    actually manages the default gateway). ``_run_hermes_sh`` is defined later
    in this module (the channel-lifecycle section); it resolves at call time."""
    return await _run_hermes_sh(subcommand, timeout_s=timeout_s)


@router.post("/api/agents/hermes/{profile}/gateway/start")
async def hermes_profile_gateway_start(profile: str):
    """Idempotent start. Default → hermes.sh start. Others → pool spawn."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    if profile == "default":
        rc, output = await _run_default_hermes_sh("start", timeout_s=60.0)
        return {
            "ok": rc == 0,
            "profile": profile,
            "status": "started" if rc == 0 else "error",
            "output": output,
            "entry": _default_gateway_entry(),
        }

    try:
        url = await asyncio.to_thread(gateway_pool.ensure_gateway, profile)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)},
        )
    return {
        "ok": True,
        "profile": profile,
        "gateway_url": url,
        "entry": _pool_entry(profile),
    }


@router.post("/api/agents/hermes/{profile}/gateway/stop")
async def hermes_profile_gateway_stop(profile: str):
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    if profile == "default":
        rc, output = await _run_default_hermes_sh("stop", timeout_s=30.0)
        return {
            "ok": rc == 0,
            "profile": profile,
            "stopped": rc == 0,
            "output": output,
        }

    stopped = await asyncio.to_thread(gateway_pool.stop_gateway, profile)
    return {"ok": True, "profile": profile, "stopped": stopped}


@router.post("/api/agents/hermes/{profile}/gateway/restart")
async def hermes_profile_gateway_restart(profile: str):
    """Real restart. Default → hermes.sh restart. Others → pool stop+ensure.

    Caller waits — typical duration is 3-6s after the new process binds
    the port. The hermes.sh path takes longer because it also brings the
    Slack/Telegram channel adapters back up."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    if profile == "default":
        rc, output = await _run_default_hermes_sh("restart", timeout_s=90.0)
        return {
            "ok": rc == 0,
            "profile": profile,
            "status": "restarted" if rc == 0 else "error",
            "output": output,
            "entry": _default_gateway_entry(),
        }

    await asyncio.to_thread(gateway_pool.stop_gateway, profile)
    try:
        url = await asyncio.to_thread(gateway_pool.ensure_gateway, profile)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {
        "ok": True,
        "profile": profile,
        "status": "restarted",
        "gateway_url": url,
        "entry": _pool_entry(profile),
    }


# ── Config (config.yaml) ─────────────────────────────────────────────────────


@router.get("/api/agents/hermes/{profile}/config")
async def hermes_profile_config_get(profile: str):
    """Parsed ``<profile>/config.yaml`` with sensitive fields masked.
    Returns 404 (not 500) when the profile hasn't run a ``hermes config``
    yet — its config.yaml only materializes after the first edit."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    config_path = resolved / "config.yaml"
    if not config_path.is_file():
        return JSONResponse(
            status_code=404,
            content={"detail": f"{config_path.name} not yet created for {profile}"},
        )
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"failed to parse {config_path.name}: {e}"},
        )
    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": f"unexpected top-level shape: {type(raw).__name__}"},
        )
    return {
        "profile": profile,
        "config_file": str(config_path),
        "config": _mask_sensitive(raw),
    }


class HermesConfigSetBody(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: Any = Field(...)


@router.put("/api/agents/hermes/{profile}/config")
async def hermes_profile_config_set(profile: str, body: HermesConfigSetBody):
    """Set one config key. Shells out to ``hermes config set <key> <value>``
    under ``HERMES_HOME=<profile>`` so we don't have to know hermes's
    on-disk schema (yaml shape, type coercion, nested keys with dots) —
    the CLI owns that contract.

    Non-string values are JSON-stringified before passing to argv so the
    CLI can coerce them. Returns ``restart_required: true`` because
    ``api_server`` reads its config once at startup."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    if isinstance(body.value, str):
        value_arg = body.value
    else:
        value_arg = json.dumps(body.value)

    argv = [_HERMES.binary, "config", "set", body.key, value_arg]
    rc, output = await _run_cli(resolved, argv)
    if rc != 0:
        return JSONResponse(
            status_code=500,
            content={"detail": f"hermes config set exited {rc}: {output}"},
        )
    return {
        "ok": True,
        "profile": profile,
        "key": body.key,
        "restart_required": True,
        "output": output,
    }


# ── Raw secrets (.env) ───────────────────────────────────────────────────────


@router.get("/api/agents/hermes/{profile}/secrets")
async def hermes_profile_secrets_get(profile: str, request: Request):
    """Return ``<profile>/.env`` as a list of entries. Values are NEVER
    masked here — the FE's secrets tab wants the raw values to let the
    user edit them. If you only need to know which keys are set, hit
    ``/api/agents/hermes/{profile}/secrets/keys``."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    entries = load_env_entries(resolved / ".env")
    return {"profile": profile, "env_file": str(resolved / ".env"), "entries": entries}


@router.get("/api/agents/hermes/{profile}/secrets/keys")
async def hermes_profile_secrets_keys(profile: str):
    """Keys-only view of the profile's .env — no secret material is
    returned. Used by onboarding flows to detect which provider/channel
    keys are already populated."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    return {"profile": profile, "keys": list_env_keys(resolved / ".env")}


class HermesSecretsPutBody(BaseModel):
    entries: list[EnvEntry]


@router.put("/api/agents/hermes/{profile}/secrets")
async def hermes_profile_secrets_put(profile: str, body: HermesSecretsPutBody):
    """Replace the entire ``<profile>/.env`` with the provided entries.
    Existing comments/blank lines are NOT preserved — for surgical edits
    use the channel / provider routes which call ``upsert_env_entry``.

    Returns ``restart_required: true`` for the profile's gateway."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    try:
        save_env_entries(resolved / ".env", body.entries)
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"failed to write .env: {e}"})
    return {"ok": True, "profile": profile, "count": len(body.entries), "restart_required": True}


# ── Channels (slack, telegram, ...) ──────────────────────────────────────────


def _hermes_channel_recipe(platform: str) -> dict | None:
    return (_HERMES.channels or {}).get(platform)


@router.get("/api/agents/hermes/{profile}/channels")
async def hermes_profile_channels_get(profile: str):
    """Per-platform connection state from ``<profile>/gateway_state.json``.
    Returns ``{channels: {...}, gateway_running}`` matching the shape
    ``/api/channels`` returns, so the FE Channels panel can reuse its
    parser."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    state_file = resolved / "gateway_state.json"
    if not state_file.is_file():
        return {"profile": profile, "channels": {}, "gateway_running": False}
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return {"profile": profile, "channels": {}, "gateway_running": False}

    platforms = state.get("platforms") or {}
    channels: dict[str, dict] = {}
    for platform_id, info in platforms.items():
        if not isinstance(info, dict) or platform_id == "api_server":
            continue
        channels[platform_id] = {
            "id": platform_id,
            "name": platform_id,
            "type": platform_id,
            "status": info.get("state") or "unknown",
            "account": info.get("error_message") or None,
        }
    return {
        "profile": profile,
        "channels": channels,
        "gateway_running": (state.get("gateway_state") == "running"),
    }


@router.post("/api/agents/hermes/{profile}/channels")
async def hermes_profile_channels_add(profile: str, request: Request):
    """Add (or update) a channel for this profile. Mirrors
    ``/api/channels/hermes/add`` but writes into ``<profile>/.env`` and
    runs the manifest's CLI follow-up under ``HERMES_HOME=<profile>``.

    Body: ``{platform: "slack"|"telegram"|..., <field>: <value>, ...}``.
    Manifest ``defaults`` apply when a field is missing (same as the
    default-scoped channel route)."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = _hermes_channel_recipe(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes channel: {platform or '(missing)'}"},
        )

    defaults = recipe.get("defaults") or {}
    to_upsert: list[tuple[str, str]] = []
    for field, env_key in (recipe.get("fields") or {}).items():
        value = (body.get(field) or "").strip() if isinstance(body.get(field), str) else ""
        if not value:
            fallback = defaults.get(field)
            if isinstance(fallback, str) and fallback:
                value = fallback
        if not value:
            return JSONResponse(
                status_code=400,
                content={"detail": f"{platform} is missing required field: {field}"},
            )
        to_upsert.append((env_key, value))

    env_file = resolved / ".env"
    try:
        for env_key, value in to_upsert:
            upsert_env_entry(env_file, env_key, value)
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"failed to write {env_file}: {e}"},
        )

    has_cli_chain = bool(recipe.get("commands"))
    if not has_cli_chain:
        return {
            "ok": True,
            "profile": profile,
            "platform": platform,
            "restart_required": True,
            "provisioning": "skipped",
        }

    # Render the manifest's commands template (same renderer the default
    # channel route uses); each rendered argv gets the profile's
    # HERMES_HOME via _run_cli.
    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"invalid hermes channel recipe: {e}"},
        )

    asyncio.create_task(_run_channel_chain_bg(profile, resolved, platform, argvs))

    return {
        "ok": True,
        "profile": profile,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }


async def _run_channel_chain_bg(profile: str, profile_dir: Path, platform: str, argvs: list[list[str]]) -> None:
    """Background CLI chain for a channel add. Aborts on first non-zero
    exit so a broken setting doesn't leave the user thinking everything
    landed when only half did. Output goes to the profile's provisioning
    log just like the default-scoped flow."""
    log_path = profile_dir / "provisioning.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for argv in argvs:
        rc, output = await _run_cli(profile_dir, argv)
        with log_path.open("a") as log:
            log.write(f"\n=== hermes channel {profile}/{platform} ===\n$ {' '.join(argv)}\n{output}\n[exit {rc}]\n")
        if rc != 0:
            with log_path.open("a") as log:
                log.write(f"[chain aborted at {' '.join(argv)}]\n")
            return


@router.delete("/api/agents/hermes/{profile}/channels/{platform}")
async def hermes_profile_channels_remove(profile: str, platform: str):
    """Clear all env entries this channel writes (e.g. SLACK_BOT_TOKEN,
    SLACK_APP_TOKEN, SLACK_ALLOWED_USERS for ``slack``), and strip the
    platform's entry from ``gateway_state.json`` so the FE channels tab
    stops showing the now-disconnected platform.

    The gateway still needs a restart for any in-process subscriber to
    actually shut down — we just remove the disk-level record so the next
    refetch + restart produces a clean state.
    """
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    platform_lc = platform.lower()
    recipe = _hermes_channel_recipe(platform_lc)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes channel: {platform}"},
        )

    env_file = resolved / ".env"
    cleared: list[str] = []
    for env_key in (recipe.get("fields") or {}).values():
        try:
            if delete_env_entry(env_file, env_key):
                cleared.append(env_key)
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"detail": f"failed to update {env_file}: {e}"},
            )

    # Strip the platform from gateway_state.json. The gateway will rewrite
    # this file on next health check, but until then the FE Channels tab
    # would still surface the stale record (a yesterday-stale "slack:
    # retrying" entry is what made this bug obvious).
    state_file = resolved / "gateway_state.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text())
            platforms = state.get("platforms")
            if isinstance(platforms, dict) and platform_lc in platforms:
                platforms.pop(platform_lc, None)
                state_file.write_text(json.dumps(state, indent=2))
        except (OSError, ValueError):
            # Non-fatal — the env-level disconnect already succeeded.
            pass

    return {
        "ok": True,
        "profile": profile,
        "platform": platform,
        "cleared": cleared,
        "restart_required": True,
    }


# ── Provider keys ────────────────────────────────────────────────────────────


def _hermes_provider_recipe(provider_id: str) -> dict | None:
    return (_HERMES.providers or {}).get(provider_id)


# Hermes natively supports OAuth (device-code) for these. Adding more
# requires upstream hermes changes — the ``hermes auth add`` subcommand's
# ``--provider`` validator currently hardcodes this set.
_HERMES_OAUTH_PROVIDERS = frozenset({"nous", "openai-codex"})


def _has_oauth_credential(profile_dir: Path, provider_id: str) -> bool:
    """Read ``<profile>/auth.json`` and report whether any OAuth-sourced
    credential is currently registered for ``provider_id``. The CLI seeds
    a ``source=device_code`` entry on successful login; missing file or
    missing provider key both count as "not configured" (False).
    """
    auth_path = profile_dir / "auth.json"
    if not auth_path.is_file():
        return False
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        return False
    pool = (data.get("credential_pool") or {}).get(provider_id) or []
    if not isinstance(pool, list):
        return False
    for cred in pool:
        if not isinstance(cred, dict):
            continue
        # Heuristic: any access_token implies the OAuth flow completed.
        # The CLI also writes ``source=device_code`` but reading that tag
        # is brittle — token presence is the real liveness signal.
        if (cred.get("access_token") or "").strip():
            return True
    return False


@router.get("/api/agents/hermes/{profile}/providers")
async def hermes_profile_providers_get(profile: str):
    """Provider catalog for this profile: every entry the FE needs to
    render a "Providers" tab.

    Two classes are merged into the response:
    - **API-key providers** declared in ``manifest.json → providers.*``.
      ``configured`` flips to ``true`` when the declared env_key is set
      in ``<profile>/.env``.
    - **OAuth providers** (currently ``nous`` and ``openai-codex``) that
      the FE drives via ``POST .../providers/{id}/login``. ``configured``
      flips when ``<profile>/auth.json`` has a token for them.

    No secret material is returned — only ids, flags, and the env_key
    name so the FE can render hints."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    env_keys = set(list_env_keys(resolved / ".env"))
    out: list[dict] = []
    for provider_id, recipe in (_HERMES.providers or {}).items():
        if not isinstance(recipe, dict):
            continue
        env_key = recipe.get("env_key")
        if not isinstance(env_key, str):
            continue
        out.append({
            "provider_id": provider_id,
            "auth_type": "api_key",
            "env_key": env_key,
            "configured": env_key in env_keys,
        })
    for oauth_id in sorted(_HERMES_OAUTH_PROVIDERS):
        out.append({
            "provider_id": oauth_id,
            "auth_type": "oauth",
            "env_key": None,
            "configured": _has_oauth_credential(resolved, oauth_id),
        })
    return {"profile": profile, "providers": out}


class HermesProviderKeyBody(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=4096)


@router.post("/api/agents/hermes/{profile}/providers/{provider_id}/key")
async def hermes_profile_provider_key_set(
    profile: str, provider_id: str, body: HermesProviderKeyBody,
):
    """Save a provider API key into ``<profile>/.env`` and kick off the
    manifest's CLI follow-up chain under ``HERMES_HOME=<profile>``. Mirrors
    ``/api/config/hermes/providers/{id}/key`` but scoped to one profile.
    """
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    recipe = _hermes_provider_recipe(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes provider: {provider_id}"},
        )
    env_key = recipe.get("env_key")
    if not isinstance(env_key, str) or not env_key:
        return JSONResponse(
            status_code=500,
            content={"detail": f"hermes provider {provider_id!r} has no env_key in manifest"},
        )

    try:
        upsert_env_entry(resolved / ".env", env_key, body.api_key.strip())
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"failed to write env: {e}"})

    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"invalid provider recipe for {provider_id!r}: {e}"},
        )

    asyncio.create_task(_run_provider_chain_bg(profile, resolved, provider_id, argvs))
    return {
        "ok": True,
        "profile": profile,
        "provider": provider_id,
        "env_key": env_key,
        "restart_required": True,
        "provisioning": "started" if argvs else "skipped",
    }


async def _run_provider_chain_bg(profile: str, profile_dir: Path, provider_id: str, argvs: list[list[str]]) -> None:
    log_path = profile_dir / "provisioning.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for argv in argvs:
        rc, output = await _run_cli(profile_dir, argv)
        with log_path.open("a") as log:
            log.write(f"\n=== hermes provider {profile}/{provider_id} ===\n$ {' '.join(argv)}\n{output}\n[exit {rc}]\n")
        if rc != 0:
            with log_path.open("a") as log:
                log.write(f"[chain aborted at {' '.join(argv)}]\n")
            return


@router.delete("/api/agents/hermes/{profile}/providers/{provider_id}/key")
async def hermes_profile_provider_key_clear(profile: str, provider_id: str):
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    recipe = _hermes_provider_recipe(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes provider: {provider_id}"},
        )
    env_key = recipe.get("env_key")
    if not isinstance(env_key, str) or not env_key:
        return JSONResponse(status_code=500, content={"detail": "missing env_key in manifest"})
    try:
        cleared = delete_env_entry(resolved / ".env", env_key)
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"failed to update env: {e}"})
    return {
        "ok": True,
        "profile": profile,
        "provider": provider_id,
        "cleared": cleared,
        "restart_required": cleared,
    }


# ── OAuth providers (nous, openai-codex) ─────────────────────────────────────
#
# These can't be configured via ``/providers/{id}/key`` because they don't
# use API keys — the user authorizes a device-code flow in their browser.
# We model them as an SSE stream so the FE can render the verification URL
# + user code as soon as hermes prints them, and a final ``done`` event
# tells the FE the credential was stored (or what went wrong).
#
# The ``logout`` route mirrors ``/providers/{id}/key`` DELETE semantics for
# OAuth: it pulls the credential out of ``<profile>/auth.json`` via the
# CLI's own remove command, which also clears any reset/exhaustion flags.

_OAUTH_LOGIN_TIMEOUT_S = 15 * 60   # match the CLI's poll window
_OAUTH_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_OAUTH_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# A "user code" looks like ``X6ZM-IST7L`` (codex) or ``ABCD-1234`` (nous).
# Permissive: 4-32 chars of letters/digits/dashes, no spaces, must contain at
# least one letter or digit and at most one dash group. Avoids matching
# random short words by requiring 4+ chars OR a dash.
_OAUTH_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,31}$")


def _strip_ansi(text: str) -> str:
    return _OAUTH_ANSI_RE.sub("", text)


def _line_is_code(stripped: str) -> bool:
    """Heuristic: stripped line is a bare user-code token. Rejects URLs,
    obvious sentences (>1 word), and very long blobs."""
    if not stripped or " " in stripped or "http" in stripped.lower():
        return False
    if not _OAUTH_CODE_RE.match(stripped):
        return False
    # Require at least one non-letter char (digit or dash) so common words
    # like "Starting" / "Waiting" don't slip through.
    return any(c.isdigit() or c == "-" for c in stripped)


def _extract_url(stripped: str) -> str | None:
    """Pull the first http(s) URL out of a line, stripping trailing
    punctuation. None if no URL on this line."""
    match = _OAUTH_URL_RE.search(stripped)
    if not match:
        return None
    return match.group(0).rstrip(".,;:)]")


async def _stream_oauth_login(
    profile: str,
    profile_dir: Path,
    provider_id: str,
):
    """Async generator yielding SSE event lines for ``hermes auth add
    <provider> --type oauth --no-browser``. Emits structured events:

    - ``{type:"log", line:"..."}`` per stdout line (ANSI-stripped)
    - ``{type:"device_code", url, code}`` once both fields parsed
    - ``{type:"done", ok:bool, returncode:int}`` at end
    - ``{type:"error", message:"..."}`` on spawn or transport failure

    Caller (FastAPI) wraps the generator in a StreamingResponse with
    text/event-stream. The subprocess inherits ``HERMES_HOME=<profile>``
    so the resulting credential lands in ``<profile>/auth.json``.
    """
    import os

    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_dir)
    # Keep python output line-buffered so we see the URL + code in real time.
    env["PYTHONUNBUFFERED"] = "1"

    argv = [
        _HERMES.binary, "auth", "add", provider_id,
        "--type", "oauth",
        "--no-browser",
        "--timeout", "30",
    ]

    def _ev(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(profile_dir),
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        yield _ev({"type": "error", "message": "hermes CLI not found on PATH"})
        yield _ev({"type": "done", "ok": False, "returncode": -1})
        return
    except Exception as exc:  # noqa: BLE001 — surface real error to FE
        yield _ev({"type": "error", "message": f"spawn failed: {exc!r}"})
        yield _ev({"type": "done", "ok": False, "returncode": -1})
        return

    url_seen: str | None = None
    code_seen: str | None = None
    device_code_emitted = False

    try:
        async def _read_loop():
            nonlocal url_seen, code_seen, device_code_emitted
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    return
                line = _strip_ansi(raw.decode(errors="replace")).rstrip("\r\n")
                yield _ev({"type": "log", "line": line})

                stripped = line.strip()
                if stripped:
                    # URL and code are typically on separate lines (the
                    # CLI prints a label line then an indented value line).
                    # Detect each independently.
                    if not url_seen:
                        candidate = _extract_url(stripped)
                        if candidate:
                            url_seen = candidate
                    if not code_seen and _line_is_code(stripped):
                        code_seen = stripped
                    if (
                        not device_code_emitted
                        and url_seen
                        and code_seen
                    ):
                        device_code_emitted = True
                        yield _ev({
                            "type": "device_code",
                            "url": url_seen,
                            "code": code_seen,
                        })

        async for chunk in _read_loop():
            yield chunk

        # Drain the process; respect the hard cap so a hung CLI doesn't
        # pin the request handler forever.
        try:
            await asyncio.wait_for(proc.wait(), timeout=_OAUTH_LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            yield _ev({"type": "error", "message": f"timed out after {_OAUTH_LOGIN_TIMEOUT_S}s"})
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            yield _ev({"type": "done", "ok": False, "returncode": -1})
            return

        rc = proc.returncode if proc.returncode is not None else -1
        yield _ev({"type": "done", "ok": rc == 0, "returncode": rc})
    except asyncio.CancelledError:
        # Client disconnected — kill the CLI so we don't leak a process
        # waiting forever on the device-code poll loop.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise


@router.post("/api/agents/hermes/{profile}/providers/{provider_id}/login")
async def hermes_profile_provider_oauth_login(profile: str, provider_id: str):
    """Drive the OAuth device-code flow for ``provider_id`` on this profile.

    Returns ``text/event-stream``. The FE consumes events:
    - ``device_code`` exposes ``{url, code}`` for the user to complete in
      their browser (we don't open it server-side).
    - ``log`` carries raw stdout lines for diagnostics.
    - ``done`` signals completion — ``ok: true`` means the credential is
      now in ``<profile>/auth.json``; ``false`` means the user cancelled,
      the code expired, or the CLI errored.

    The profile's gateway needs a restart afterwards because hermes only
    refreshes credentials at startup; the FE should prompt for one once
    ``done.ok`` is true.
    """
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    if provider_id not in _HERMES_OAUTH_PROVIDERS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    f"{provider_id!r} is not an OAuth provider. Use "
                    f"POST .../providers/{provider_id}/key for API-key providers."
                )
            },
        )

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _stream_oauth_login(profile, resolved, provider_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx proxy buffering if present
        },
    )


@router.post("/api/agents/hermes/{profile}/providers/{provider_id}/logout")
async def hermes_profile_provider_oauth_logout(profile: str, provider_id: str):
    """Remove every OAuth-sourced credential for ``provider_id`` from
    ``<profile>/auth.json``. Walks the credential_pool, calls
    ``hermes auth remove <provider> <index>`` for each entry, and reports
    how many were cleared. Safe to call when nothing's configured
    (returns ``cleared: 0``)."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    if provider_id not in _HERMES_OAUTH_PROVIDERS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    f"{provider_id!r} is not an OAuth provider. Use "
                    f"DELETE .../providers/{provider_id}/key for API-key providers."
                )
            },
        )

    # Read current pool to know how many entries to remove. We always
    # remove index 0 N times — each removal shifts later entries down,
    # so 0 always points at the next victim until the pool is empty.
    auth_path = resolved / "auth.json"
    if not auth_path.is_file():
        return {
            "ok": True,
            "profile": profile,
            "provider": provider_id,
            "cleared": 0,
            "restart_required": False,
        }
    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"failed to read auth.json: {e}"},
        )
    pool = (auth.get("credential_pool") or {}).get(provider_id) or []
    if not isinstance(pool, list):
        pool = []
    count = len(pool)
    if not count:
        return {
            "ok": True,
            "profile": profile,
            "provider": provider_id,
            "cleared": 0,
            "restart_required": False,
        }

    cleared = 0
    last_output = ""
    for _ in range(count):
        rc, output = await _run_cli(
            resolved,
            [_HERMES.binary, "auth", "remove", provider_id, "0"],
        )
        last_output = output
        if rc != 0:
            break
        cleared += 1

    return {
        "ok": cleared == count,
        "profile": profile,
        "provider": provider_id,
        "cleared": cleared,
        "remaining": max(count - cleared, 0),
        "restart_required": cleared > 0,
        "output": last_output,
    }


# ── Persona (SOUL.md) ────────────────────────────────────────────────────────


@router.get("/api/agents/hermes/{profile}/soul")
async def hermes_profile_soul_get(profile: str):
    """Read ``<profile>/SOUL.md`` (the per-profile system prompt). Returns
    empty string when not yet created — SOUL.md is created by ``hermes
    profile create`` for new profiles but may be missing for legacy ones."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    path = resolved / "SOUL.md"
    if not path.is_file():
        return {"profile": profile, "path": str(path), "content": "", "exists": False}
    try:
        content = path.read_text(errors="replace")
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"read failed: {e}"})
    return {"profile": profile, "path": str(path), "content": content, "exists": True}


class HermesSoulBody(BaseModel):
    content: str = Field(..., max_length=64_000)


@router.put("/api/agents/hermes/{profile}/soul")
async def hermes_profile_soul_put(profile: str, body: HermesSoulBody):
    """Write ``<profile>/SOUL.md``. Caller-supplied content replaces the
    file in full. Hermes reads SOUL.md at gateway startup so the FE must
    prompt for a restart to see the new persona take effect."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    try:
        (resolved / "SOUL.md").write_text(body.content)
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"write failed: {e}"})
    return {"ok": True, "profile": profile, "restart_required": True}


# ── Memory (USER.md / MEMORY.md / *.md under <profile>/memories/) ────────────


@router.get("/api/agents/hermes/{profile}/memory")
async def hermes_profile_memory_list(profile: str):
    """List markdown files under ``<profile>/memories/`` with size + a
    400-char preview. Files in subdirectories are skipped — hermes's
    built-in memory layout is flat."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    mem_dir = resolved / "memories"
    if not mem_dir.is_dir():
        return {"profile": profile, "memories_dir": str(mem_dir), "files": []}
    files: list[dict] = []
    for child in sorted(mem_dir.iterdir()):
        if not child.is_file() or not _MEMORY_FILENAME_RE.match(child.name):
            continue
        try:
            text = child.read_text(errors="replace")
        except OSError:
            continue
        files.append({
            "name": child.name,
            "size": child.stat().st_size,
            "preview": text[:400],
        })
    return {"profile": profile, "memories_dir": str(mem_dir), "files": files}


def _memory_path(profile_dir: Path, filename: str) -> Path | JSONResponse:
    """Resolve + validate that ``filename`` lives directly under
    ``<profile>/memories/``. Rejects subpaths, traversal, and unknown
    extensions."""
    if not _MEMORY_FILENAME_RE.match(filename):
        return JSONResponse(
            status_code=400,
            content={"detail": f"invalid memory filename: {filename!r}"},
        )
    candidate = (profile_dir / "memories" / filename).resolve()
    mem_root = (profile_dir / "memories").resolve()
    try:
        candidate.relative_to(mem_root)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "path escape rejected"})
    return candidate


@router.get("/api/agents/hermes/{profile}/memory/{filename}")
async def hermes_profile_memory_get(profile: str, filename: str):
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    path = _memory_path(resolved, filename)
    if isinstance(path, JSONResponse):
        return path
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"detail": f"memory file {filename!r} not found"},
        )
    try:
        content = path.read_text(errors="replace")
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"read failed: {e}"})
    return {"profile": profile, "filename": filename, "content": content}


class HermesMemoryBody(BaseModel):
    content: str = Field(..., max_length=200_000)


@router.put("/api/agents/hermes/{profile}/memory/{filename}")
async def hermes_profile_memory_put(profile: str, filename: str, body: HermesMemoryBody):
    """Write/replace a memory file. Allowlist: filename must match
    ``[A-Za-z0-9_.-]+\\.md``. Hermes reads memory at session start so the
    restart-required hint is omitted here — new sessions pick it up
    automatically."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    path = _memory_path(resolved, filename)
    if isinstance(path, JSONResponse):
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content)
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"write failed: {e}"})
    return {"ok": True, "profile": profile, "filename": filename}


# ── Models proxy ─────────────────────────────────────────────────────────────


@router.get("/api/agents/hermes/{profile}/models")
async def hermes_profile_models(profile: str):
    """Forward ``GET /v1/models`` against this profile's gateway. Useful
    for the FE to know which model the profile is advertising (set via
    ``hermes config set model.default`` for that profile).

    Returns ``running: false`` (200) when the profile's gateway isn't up
    rather than 502, so the FE can render an empty model list without an
    error banner."""
    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved
    base = _gateway_base_for(profile)
    if not base:
        return {"profile": profile, "running": False, "models": []}

    headers = {}
    if _HERMES.api_token:
        headers["Authorization"] = f"Bearer {_HERMES.api_token}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            resp = await client.get(f"{base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"gateway unreachable: {exc}"},
        )

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content={"detail": resp.text[:500]},
        )
    payload = resp.json()
    return {
        "profile": profile,
        "running": True,
        "gateway_url": base,
        "models": payload.get("data") or [],
    }


# ── Pool admin ───────────────────────────────────────────────────────────────


@router.get("/api/channels/hermes/pool")
async def hermes_pool_snapshot():
    """Full snapshot of the gateway pool (all profiles managed by this
    cowork-api). Default profile is not included — it's owned by
    hermes.sh and surfaced via ``/api/channels/hermes/status``."""
    return {"pool": gateway_pool.list_pool()}


# ── Insights (per-profile, pure SQL) ─────────────────────────────────────────
#
# `/api/usage` aggregates openclaw + claude_code + ALL hermes profiles into
# one global UsageStats — too coarse for the per-agent Overview tab. This
# endpoint reads ONE profile's state.db directly and returns the slice the
# FE Overview tab renders: overview totals + platform breakdown (sessions
# grouped by source) + top tools (from tool_calls JSON on assistant rows,
# UNION'd with the explicit tool_name column on tool-role rows for CLI flow).
#
# Pure SQL (no subprocess, no hermes Python import). Trade-off: no
# `agent.usage_pricing` cost lookup for unknown models — costs always reflect
# what's stored on the row. For the production models (kimi-k2.5,
# claude-opus-4-6) that lookup currently returns zero anyway, so we lose
# nothing in practice.


@router.get("/api/agents/hermes/{profile}/insights")
async def hermes_profile_insights(profile: str, days: int = 30):
    """Per-profile activity slice for the FE Overview tab. Returns overview
    totals, platforms (grouped by ``source``), and top tools — same shape
    consumed by ``useHermesInsights`` on the FE.
    """
    import sqlite3
    import time as _time
    from services.cowork_agent.adapters.hermes.paths import HERMES_DIR

    resolved = _resolve_profile(profile)
    if isinstance(resolved, JSONResponse):
        return resolved

    days = max(1, min(int(days), 365))
    db_path = HERMES_DIR / "state.db" if profile == "default" else (resolved / "state.db")
    empty_response = {
        "profile": profile, "days": days, "empty": True,
        "overview": {}, "platforms": [], "tools": [],
    }
    if not db_path.is_file():
        return empty_response

    cutoff = _time.time() - days * 86400

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        return JSONResponse(status_code=500, content={"detail": f"open state.db failed: {e}"})

    try:
        # Overview — single aggregate row for the window.
        row = conn.execute(
            "SELECT COUNT(*) AS sessions, "
            "       COALESCE(SUM(message_count), 0) AS messages, "
            "       COALESCE(SUM(tool_call_count), 0) AS tools, "
            "       COALESCE(SUM(input_tokens), 0) AS input_t, "
            "       COALESCE(SUM(output_tokens), 0) AS output_t, "
            "       COALESCE(SUM(cache_read_tokens), 0) AS cache_r, "
            "       COALESCE(SUM(cache_write_tokens), 0) AS cache_w, "
            "       COALESCE(SUM(reasoning_tokens), 0) AS reasoning, "
            "       COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0) AS cost "
            "FROM sessions WHERE started_at >= ?",
            (cutoff,),
        ).fetchone()
        if not row or int(row[0] or 0) == 0:
            return empty_response

        sessions, messages, tools_total, in_t, out_t, cache_r, cache_w, reasoning, cost = row
        overview = {
            "total_sessions": int(sessions or 0),
            "total_messages": int(messages or 0),
            "total_tool_calls": int(tools_total or 0),
            "total_input_tokens": int(in_t or 0),
            "total_output_tokens": int(out_t or 0),
            "total_cache_read_tokens": int(cache_r or 0),
            "total_cache_write_tokens": int(cache_w or 0),
            "total_tokens": int((in_t or 0) + (out_t or 0) + (cache_r or 0) + (cache_w or 0) + (reasoning or 0)),
            "estimated_cost": float(cost or 0),
            "actual_cost": float(cost or 0),
        }

        # Platforms — GROUP BY source. The `source` column is api_server / cli /
        # slack / telegram / etc. NULL sources fall under "unknown".
        platforms = [
            {
                "platform": (p_src or "unknown"),
                "sessions": int(p_sessions or 0),
                "messages": int(p_messages or 0),
                "total_tokens": int((p_in or 0) + (p_out or 0) + (p_cache or 0)),
            }
            for (p_src, p_sessions, p_messages, p_in, p_out, p_cache) in conn.execute(
                "SELECT source, COUNT(*), "
                "       COALESCE(SUM(message_count), 0), "
                "       COALESCE(SUM(input_tokens), 0), "
                "       COALESCE(SUM(output_tokens), 0), "
                "       COALESCE(SUM(cache_read_tokens), 0) "
                "FROM sessions WHERE started_at >= ? GROUP BY source ORDER BY COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
        ]

        # Tools — two sources, merged:
        # 1. assistant rows: tool names live inside a `tool_calls` JSON array
        #    as `[{function: {name: <tool>}, ...}, ...]` (api_server flow)
        # 2. tool-role rows: `tool_name` column is populated (CLI flow)
        # We use sqlite's json1 to extract names from #1, then UNION ALL with
        # the simpler #2 query and aggregate in Python.
        tool_counter: dict[str, int] = {}
        try:
            for name, count in conn.execute(
                "SELECT json_extract(je.value, '$.function.name') AS tool, COUNT(*) "
                "FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "JOIN json_each(m.tool_calls) je "
                "WHERE s.started_at >= ? AND m.tool_calls IS NOT NULL AND m.role = 'assistant' "
                "GROUP BY tool",
                (cutoff,),
            ):
                if name:
                    tool_counter[name] = tool_counter.get(name, 0) + int(count)
        except sqlite3.OperationalError:
            # json1 missing or unexpected JSON shape — skip silently
            pass

        try:
            for name, count in conn.execute(
                "SELECT m.tool_name, COUNT(*) "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.started_at >= ? AND m.tool_name IS NOT NULL AND m.role = 'tool' "
                "GROUP BY m.tool_name",
                (cutoff,),
            ):
                if name:
                    tool_counter[name] = tool_counter.get(name, 0) + int(count)
        except sqlite3.OperationalError:
            pass

        total_tool_calls = sum(tool_counter.values()) or 1
        tools_list = [
            {"tool": name, "count": count, "percentage": (count / total_tool_calls) * 100.0}
            for name, count in sorted(tool_counter.items(), key=lambda kv: -kv[1])
        ][:10]
    finally:
        conn.close()

    return {
        "profile": profile,
        "days": days,
        "empty": False,
        "overview": overview,
        "platforms": platforms,
        "tools": tools_list,
    }


# ── Hermes channel provisioning ──────────────────────────────────────────────
#
# The agent-agnostic ``/api/channels/add`` (in api/channels/routes.py)
# writes the *active* agent's env + provisioning chain. These hermes-pinned
# routes always target the hermes manifest regardless of ``AGENT_NAME`` — every
# token lands in ``~/.hermes/.env`` and the CLI follow-up uses hermes's own
# ``cli_timeout_seconds`` and provisioning log. They mount only when hermes is
# the active agent (this module is the active-agent ``routes`` capability).


async def _run_one_hermes(platform: str, argv: list[str]) -> int:
    """Run one hermes CLI argv against the hermes manifest's cwd and log."""
    log_path = _HERMES.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_HERMES.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_HERMES.cli_timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            with log_path.open("a") as log:
                log.write(f"\n=== {ts} hermes channel: {platform} ===\n$ {' '.join(argv)}\n[timed out]\n")
            return -1
    except FileNotFoundError:
        with log_path.open("a") as log:
            log.write(f"\n=== {ts} hermes channel: {platform} ===\n[hermes CLI not found in PATH]\n")
        return -1
    except Exception as e:  # noqa: BLE001 — log and carry on
        with log_path.open("a") as log:
            log.write(f"\n=== {ts} hermes channel: {platform} ===\n[exception] {e}\n")
        return -1

    with log_path.open("a") as log:
        log.write(f"\n=== {ts} hermes channel: {platform} ===\n$ {' '.join(argv)}\n")
        log.write(stdout.decode(errors="replace"))
        log.write(f"\n[exit {proc.returncode}]\n")
    return proc.returncode


async def _run_hermes_channel_bg(platform: str, recipe: dict) -> None:
    """Run a hermes channel's CLI follow-up chain in order, aborting on first non-zero.

    Unlike openclaw, hermes has no `config_set_batch` — each setting goes
    through one `hermes config set <path> <value>` call. The manifest's
    ``commands`` block is rendered as an ordered argv list.
    """
    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        with _HERMES.provisioning_log.open("a") as log:
            log.write(f"[hermes channel {platform} aborted: invalid commands: {e}]\n")
        return

    for argv in argvs:
        rc = await _run_one_hermes(platform, argv)
        if rc != 0:
            with _HERMES.provisioning_log.open("a") as log:
                log.write(f"[hermes channel {platform} aborted at: {' '.join(argv)}]\n")
            return


@router.post("/api/channels/hermes/add")
async def add_hermes_channel(request: Request):
    """Hermes channel onboarding (slack, telegram — whatsapp deliberately omitted).

    Mirrors ``/api/channels/add`` but anchored to the hermes manifest:
    every token lands in ``~/.hermes/.env`` (never ``~/.openclaw/.env``),
    and the CLI follow-up uses hermes's own ``cli_timeout_seconds`` and
    provisioning log. A hermes-named route must always target hermes
    regardless of ``AGENT_NAME``.

    Body: ``{platform: "slack"|"telegram", <field>: <value>, ...}``.
    Field names per platform come from the manifest's
    ``channels.<platform>.fields`` map. Missing fields fall back to
    ``channels.<platform>.defaults`` so the FE form doesn't have to
    collect things like ``allowed_users``.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = _HERMES.channels.get(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes channel: {platform or '(missing)'}"},
        )

    # Collect + validate every field before touching disk so we never write a
    # half-populated env file. Fields not supplied in the body fall back to
    # the manifest's ``defaults`` map — keeps the FE form minimal (e.g. user
    # never has to type "allowed_users: *"). To restrict access, edit the
    # default in ``config/agents/hermes/manifest.json``.
    defaults = recipe.get("defaults") or {}
    to_upsert: list[tuple[str, str]] = []
    for field, env_key in (recipe.get("fields") or {}).items():
        value = (body.get(field) or "").strip() if isinstance(body.get(field), str) else ""
        if not value:
            default = defaults.get(field)
            if isinstance(default, str) and default:
                value = default
        if not value:
            return JSONResponse(
                status_code=400,
                content={"detail": f"{platform} is missing required field: {field}"},
            )
        to_upsert.append((env_key, value))

    try:
        for env_key, value in to_upsert:
            upsert_default_env_entry(env_key, value)
    except Exception as e:  # noqa: BLE001 — surface the real reason
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to write hermes env file: {e}"},
        )

    has_cli_chain = bool(recipe.get("commands"))
    if not has_cli_chain:
        return {
            "ok": True,
            "platform": platform,
            "restart_required": True,
            "provisioning": "skipped",
            "detail": f"{platform} tokens saved. Restart the hermes gateway to pick up the change.",
        }

    asyncio.create_task(_run_hermes_channel_bg(platform, recipe))

    return {
        "ok": True,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }


# ── Hermes gateway lifecycle ─────────────────────────────────────────────────
#
# The hermes gateway is started by ``agent.sh start`` (PID file in /tmp).
# After the user saves a key or channel, ~/.hermes/.env has changed but the
# already-running gateway has the old env baked in — it needs a restart to
# pick up the new values. These routes give the FE a "Restart Gateway"
# button so the user doesn't have to drop to a terminal.
#
# All three lifecycle routes shell out to the same ``agent.sh`` script
# that ships in this repo. The script handles PID tracking, log rotation,
# orphan cleanup, and the auto-restart loop.

_HERMES_SH = Path(__file__).resolve().parents[4] / "config" / "agents" / "hermes" / "agent.sh"
_HERMES_GATEWAY_STATE_FILE = _HERMES.home_dir / "gateway_state.json"


def _run_hermes_sh_sync(subcommand: str, timeout_s: float) -> tuple[int, str]:
    """Blocking helper: invoke ``agent.sh <subcommand>`` and return
    ``(returncode, output)``.

    We deliberately use stdlib ``subprocess.run`` (not
    ``asyncio.create_subprocess_exec``) because the script daemonizes the
    gateway via ``nohup bash -c '...' &``. Under uvloop, the daemonized
    child inherits the read/write pipe FDs from the FastAPI worker —
    ``proc.communicate()`` then never sees EOF, hits the timeout, and the
    follow-up ``proc.kill()`` raises ``ProcessLookupError`` because the
    bash parent has already exited. Stdlib ``subprocess`` honors
    ``close_fds=True`` and ``start_new_session=True``, which severs that
    inheritance chain so the script returns cleanly.
    """
    try:
        result = subprocess.run(
            ["bash", str(_HERMES_SH), subcommand],
            cwd=str(_HERMES_SH.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            close_fds=True,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"agent.sh {subcommand} timed out after {timeout_s}s"
    except FileNotFoundError:
        return -1, f"agent.sh not found at {_HERMES_SH}"
    except Exception as e:  # noqa: BLE001
        return -1, f"agent.sh {subcommand} failed: {e!r}"

    output = (result.stdout or b"").decode(errors="replace")[-2000:]
    return result.returncode, output


async def _run_hermes_sh(subcommand: str, timeout_s: float = 60.0) -> tuple[int, str]:
    """Async wrapper for ``agent.sh <subcommand>`` — see ``_run_hermes_sh_sync``
    for why we route through a thread."""
    return await asyncio.to_thread(_run_hermes_sh_sync, subcommand, timeout_s)


def _read_hermes_gateway_state() -> dict | None:
    """Return the parsed ``~/.hermes/gateway_state.json``, or None if absent/invalid."""
    if not _HERMES_GATEWAY_STATE_FILE.is_file():
        return None
    try:
        return json.loads(_HERMES_GATEWAY_STATE_FILE.read_text())
    except Exception:
        return None


@router.get("/api/channels/hermes/status")
async def hermes_status():
    """Liveness for the hermes gateway. Combines a probe to ``/v1/models``
    on port 8642 with the on-disk ``gateway_state.json`` so the UI can
    show "running but channel X is retrying" diagnostics."""
    parsed = urlparse(_HERMES.api_url)
    port = parsed.port or 8642
    models_url = _HERMES.api_url.replace("/v1/chat/completions", "/v1/models")

    running = False
    try:
        resp = httpx.get(models_url, timeout=3.0, headers={"Authorization": f"Bearer {_HERMES.api_token}"} if _HERMES.api_token else None)
        running = resp.status_code in (200, 401, 405)  # 401 = up but unauthorized
    except Exception:
        running = False

    state = _read_hermes_gateway_state() or {}
    return {
        "installed": True,
        "running": running,
        "port": port if running else None,
        "ws_url": None,
        "gateway_state": state.get("gateway_state"),
        "active_agents": state.get("active_agents"),
        "platforms": state.get("platforms") or {},
        "updated_at": state.get("updated_at"),
    }


@router.post("/api/channels/hermes/start")
async def hermes_start():
    rc, output = await _run_hermes_sh("start", timeout_s=60.0)
    return {"ok": rc == 0, "status": "started" if rc == 0 else "error", "output": output}


@router.post("/api/channels/hermes/stop")
async def hermes_stop():
    rc, output = await _run_hermes_sh("stop", timeout_s=30.0)
    return {"ok": rc == 0, "status": "stopped" if rc == 0 else "error", "output": output}


@router.post("/api/channels/hermes/restart")
async def hermes_restart():
    """Restart the hermes gateway. The single button users will hit most —
    after saving a provider key or channel token, ``~/.hermes/.env`` has
    new values but the running gateway holds the old env in memory.
    """
    rc, output = await _run_hermes_sh("restart", timeout_s=90.0)
    return {"ok": rc == 0, "status": "restarted" if rc == 0 else "error", "output": output}


# ── Hermes config + provider keys ────────────────────────────────────────────
#
# The agent-agnostic /api/config/* routes (api/config/routes.py)
# already target the active agent. These hermes-scoped variants live here so
# they mount only when hermes is active — at which point get_active_agent() is
# hermes, so the shared agent_env helper writes ~/.hermes/.env. No core code
# names hermes and no hermes-pinned env module is needed.


@router.get("/api/config/hermes")
def get_hermes_config():
    """Return ``~/.hermes/config.yaml`` parsed to JSON with sensitive fields masked.

    Returns 404 if the file doesn't exist yet (fresh install before
    ``hermes setup`` runs).
    """
    config_path = _HERMES.config_file
    if not config_path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"{config_path.name} not found at {config_path}"},
        )

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to parse {config_path.name}: {e}"},
        )

    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": f"Unexpected top-level shape in {config_path.name}: {type(raw).__name__}"},
        )

    return _mask_sensitive(raw)


async def _run_provider_provisioning(provider_id: str, argvs: list[list[str]]) -> None:
    """Run hermes's CLI chain for a provider, appending output to the
    provisioning log. Argvs are pre-rendered from the manifest's command
    templates — no user input is interpolated. Chain aborts on the first
    non-zero exit so a broken ``models set`` doesn't leave later aliases
    pointing at nothing.
    """
    log_path = _HERMES.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as log:
        log.write(f"\n=== {ts} provisioning: {provider_id} ===\n")
        for argv in argvs:
            log.write(f"$ {' '.join(argv)}\n")
            log.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(_HERMES.cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await proc.communicate()
                log.write(stdout.decode(errors="replace"))
                log.write(f"[exit {proc.returncode}]\n")
                if proc.returncode != 0:
                    log.write("[chain aborted]\n")
                    return
            except Exception as e:  # noqa: BLE001 — log every failure, keep going
                log.write(f"[exception] {e}\n[chain aborted]\n")
                return
        log.write("[chain ok]\n")


@router.post("/api/config/hermes/providers/{provider_id}/key")
async def save_hermes_provider_key(provider_id: str, request: Request):
    """Persist a provider API key into ``~/.hermes/.env`` and (optionally) kick
    off the hermes CLI follow-up chain declared in the manifest. Provider list
    lives in ``config/agents/hermes/manifest.json`` → ``providers.*``.

    Returns 200 once the key is written; any CLI follow-up runs in the
    background and is logged to ``~/.hermes/provisioning.log``.
    """
    recipe = _HERMES.providers.get(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes provider: {provider_id}"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    api_key = (body.get("api_key") or "").strip() if isinstance(body, dict) else ""
    if not api_key:
        return JSONResponse(status_code=400, content={"detail": "api_key is required"})

    try:
        upsert_default_env_entry(recipe["env_key"], api_key)
    except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
        return JSONResponse(status_code=500, content={"detail": f"Failed to save key: {e}"})

    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Invalid hermes provider recipe for '{provider_id}': {e}"},
        )

    if argvs:
        asyncio.create_task(_run_provider_provisioning(provider_id, argvs))

    return {"ok": True, "provider": provider_id, "env_key": recipe["env_key"], "provisioning": "started" if argvs else "skipped"}
