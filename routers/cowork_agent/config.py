"""
Configuration / provider / model-list endpoints.

Groups `/api/config/*` (api-key, providers, openclaw, openyak-account,
ollama, local, openai-subscription) and the per-agent model listing
(`/api/models`). Responses here shape what the UI sees in provider menus
and the settings screen.

Provider onboarding recipes now come from the active agent's manifest
(`config/agents/<agent>.json` → `providers.*`) instead of an inline dict,
so adding a provider (or swapping agents) is a config change.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from utils.commands import run

from services.cowork_agent.registry.agent_registry import get_agent, get_active_agent
from services.cowork_agent.helpers import _mask_sensitive
from services.cowork_agent.registry.agent_env import upsert_env_entry
from services.cowork_agent.registry.agent_settings import (
    clear_settings_env,
    merge_settings_env,
)
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.adapters.loader import try_load_capability

router = APIRouter()

_AGENT = get_active_agent()


async def _run_provider_provisioning(provider_id: str, argvs: list[list[str]]) -> None:
    """Run the active agent's CLI chain for a provider, appending output to
    its provisioning log.

    Argvs are pre-rendered from the manifest's command templates — no
    user input is interpolated, and every step runs through the shared
    runner (`utils.commands.run`) as an argv list, so nothing reaches a shell.
    Chain aborts on the first non-zero exit so a broken `models set`
    doesn't leave later aliases pointing at nothing.
    """
    log_path = _AGENT.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as log:
        log.write(f"\n=== {ts} provisioning: {provider_id} ===\n")
        for argv in argvs:
            log.write(f"$ {' '.join(argv)}\n")
            log.flush()
            result = await run(argv, cwd=str(_AGENT.cwd))
            if result.binary_missing or result.exception is not None:
                log.write(f"[exception] {result.output}\n[chain aborted]\n")
                return
            log.write(result.output)
            log.write(f"[exit {result.returncode}]\n")
            if result.returncode != 0:
                log.write("[chain aborted]\n")
                return
        log.write("[chain ok]\n")


def _render_settings_env(template: dict, api_key: str) -> dict[str, str]:
    """Substitute the user's key into a ``settings_env`` recipe template.

    Values are treated as plain strings; the only placeholder is ``{api_key}``
    (replaced literally, so base URLs with no braces pass through untouched and
    there's no ``str.format`` brace-escaping to worry about). The rendered dict
    is written to the agent's config file via ``json.dump``, so the key is JSON-
    escaped at write time — no injection surface.
    """
    rendered: dict[str, str] = {}
    for key, value in template.items():
        if not isinstance(value, str):
            raise ValueError(f"settings_env value for '{key}' must be a string")
        rendered[key] = value.replace("{api_key}", api_key)
    return rendered


@router.post("/api/config/providers/{provider_id}/key")
async def save_provider_key(provider_id: str, request: Request):
    """Persist a provider API key and kick off the agent's CLI chain.

    Returns 200 as soon as the key is safely written to the agent's env
    file. The CLI chain runs in the background; its output is appended to
    the agent's provisioning log for later inspection.
    """
    recipe = _AGENT.providers.get(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported provider: {provider_id}"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    api_key = (body.get("api_key") or "").strip() if isinstance(body, dict) else ""
    if not api_key:
        return JSONResponse(status_code=400, content={"detail": "api_key is required"})

    # Settings-env recipes (gateway-style providers, e.g. OpenRouter) configure
    # the agent by merging an `env` block into its JSON config_file, which the
    # CLI reads at launch — not by writing `.env` + running a CLI verb. Presence
    # of `settings_env` selects this path; no agent name is branched on here.
    settings_env = recipe.get("settings_env")
    if settings_env:
        try:
            merge_settings_env(_AGENT.config_file, _render_settings_env(settings_env, api_key))
        except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
            return JSONResponse(status_code=500, content={"detail": f"Failed to save key: {e}"})
        return {"ok": True, "provider": provider_id}

    try:
        upsert_env_entry(recipe["env_key"], api_key)
    except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
        return JSONResponse(status_code=500, content={"detail": f"Failed to save key: {e}"})

    try:
        argvs = _AGENT.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Invalid provider recipe for '{provider_id}': {e}"},
        )

    asyncio.create_task(_run_provider_provisioning(provider_id, argvs))

    return {"ok": True, "provider": provider_id, "provisioning": "started"}


@router.delete("/api/config/providers/{provider_id}/key")
async def disconnect_provider_key(provider_id: str):
    """Remove a settings-env provider's keys from the agent's config file.

    Only defined for `settings_env` recipes (gateway-style providers), which
    write into the agent's JSON config and would otherwise stay active — e.g.
    an OpenRouter `ANTHROPIC_AUTH_TOKEN` outranks a native Claude login, so a
    user needs a way to fall back. The legacy `.env` + CLI-verb providers have
    no defined teardown, so disconnect isn't offered for them.
    """
    recipe = _AGENT.providers.get(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported provider: {provider_id}"},
        )

    settings_env = recipe.get("settings_env")
    if not settings_env:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Disconnect not supported for provider: {provider_id}"},
        )

    try:
        clear_settings_env(_AGENT.config_file, list(settings_env.keys()))
    except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
        return JSONResponse(status_code=500, content={"detail": f"Failed to disconnect: {e}"})

    return {"ok": True, "provider": provider_id, "disconnected": True}


# ── Model listing ────────────────────────────────────────────────────────────


@router.get("/api/models")
def list_models():
    """Return one model row per agent/profile under the active backend.

    Dispatch is resolved through the active agent's ``models`` capability
    (``adapters/<AGENT_NAME>/models.py`` → ``list_models()``): hermes scans
    ``~/.hermes/profiles/``; openclaw (and claude_code, which re-exports it)
    scans ``~/.openclaw/agents/``. No core code names a specific backend.
    """
    mod = try_load_capability("models")
    if mod is None or not hasattr(mod, "list_models"):
        return []
    return mod.list_models()


# ── /api/config/* routes ─────────────────────────────────────────────────────


@router.get("/api/config/api-key")
def config_api_key():
    return {"has_key": True, "provider": _AGENT.name}


@router.get("/api/config/providers")
def config_providers():
    return []


@router.get("/api/config/openai-subscription")
def openai_subscription():
    return {"is_connected": False, "email": "", "needs_reauth": False}


@router.get("/api/config/openyak-account")
def openyak_account():
    return {"linked": False}


@router.get("/api/config/ollama")
def ollama_config():
    return {"installed": False}


@router.get("/api/config/local")
def local_provider():
    return {"available": False}


@router.get("/api/config/agents/{name}")
def get_agent_config(name: str):
    """Unified per-agent config loader the FE Settings → Config tab calls.

    Returns ``{agent, config_file, config}`` where ``config`` is the parsed
    config file (json for openclaw, yaml for hermes) with sensitive values
    masked. The FE refresh button hits this route — without it both
    backends 404 and the panel hangs at "Failed to load configuration".

    Dispatch by manifest name so we don't accidentally re-anchor to the
    active default agent (same rule that protects the hermes-named routes).
    """
    try:
        manifest = get_agent(name)
    except (KeyError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unknown agent backend: {name}"},
        )

    config_path = manifest.config_file
    if not config_path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"{config_path.name} not found at {config_path}"},
        )

    suffix = config_path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml
            raw = yaml.safe_load(config_path.read_text()) or {}
        else:
            import json as _json
            raw = _json.loads(config_path.read_text() or "{}")
    except (OSError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to read {config_path}: {e}"},
        )

    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": f"Unexpected top-level shape in {config_path.name}: {type(raw).__name__}"},
        )

    return {
        "agent": name,
        "config_file": str(config_path),
        "config": _mask_sensitive(raw),
    }


@router.get("/api/config/workspace")
def get_workspace_config():
    """
    Return workspace root paths for each agent backend.

    The unified ``xo-projects`` root (``XO_PROJECTS_ROOT`` env, default
    ``~/xo-projects``) is exposed under whichever backend is active so the
    new-project picker keeps working. We also surface each installed
    backend's native home so the frontend's
    ``agentNameForWorkspace(workspaceDirectory)`` lookup can recognize
    backend-specific paths like ``~/.openclaw/agents/<id>`` and route
    chats accordingly — without that, every chat falls back to the
    active backend's default agent regardless of which sidebar agent
    the user picked.

    Response shape:
      {
        "roots": {
           "<active_backend>": "/home/coder/xo-projects",
           "<backend>": "/home/coder/.<backend>",
           ...
        },
        "default": "<active_backend>"
      }
    """
    from services.cowork_agent.registry.agent_registry import _discover_manifests

    projects_root = str(xo_projects_root())
    default_backend = os.getenv("AGENT_NAME", _AGENT.name)

    # Start with the active backend rooted at xo-projects (legacy contract).
    roots: dict[str, str] = {default_backend: projects_root}

    # Add each installed backend's native home so the frontend can match
    # backend-specific paths (~/.openclaw/agents/<id>, ~/.hermes/profiles/<name>).
    for name, manifest in _discover_manifests().items():
        # Don't overwrite the active backend's xo-projects entry.
        roots.setdefault(name, str(manifest.home_dir))

    return {"roots": roots, "default": default_backend}
