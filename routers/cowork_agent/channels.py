"""
Channel provisioning endpoints.

The onboarding "Channels" step (and later the Settings → Channels tab) POSTs
a platform id + bot tokens here. This module:

1. Upserts the platform's tokens into the active agent's env file
   (line-level, comment-preserving) via `agent_env.upsert_env_entry`.
2. Schedules the manifest's `config_set_batch` (followed by any entries in
   `post_commands`) as a **background task** — the handler does not wait
   on the CLI. This mirrors the provider-key flow and keeps the UI
   responsive: the user gets a "Connected" + "Restart Gateway" verdict
   instantly, and stdout/stderr for each step lands in the agent's
   provisioning log for diagnosis. Tokens are always referenced by
   env-var name in the batch payload — their values never enter argv.
3. Returns `{ok: true, restart_required: true}` once the env-file write
   succeeds, so the frontend can flip the row to Connected and show the
   gateway-restart notice immediately.

Recipes (fields, config_batch, post_commands) are sourced from
`config/agents/<agent>.json` → `channels.*`, not hardcoded here.
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from utils.commands import run

from services.cowork_agent.registry.agent_registry import get_active_agent
from services.cowork_agent.registry.agent_env import upsert_env_entry

router = APIRouter()

_AGENT = get_active_agent()


async def _run_one(platform: str, argv: list[str]) -> int:
    """Run one CLI argv, append stdout+stderr to the provisioning log,
    and return the exit code (or -1 on a tooling failure).

    The return value is only used to decide whether to continue the chain —
    it is never surfaced to the UI.
    """
    log_path = _AGENT.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()

    result = await run(argv, cwd=str(_AGENT.cwd), timeout=_AGENT.cli_timeout_seconds)
    if result.timed_out:
        _append_log(ts, platform, argv, f"[timed out after {_AGENT.cli_timeout_seconds}s]", rc="timeout")
        return -1
    if result.binary_missing:
        _append_log(ts, platform, argv, f"{_AGENT.binary} CLI not found in PATH", rc="missing-binary")
        return -1
    if result.exception is not None:
        _append_log(ts, platform, argv, result.output, rc="exception")
        return -1

    _append_log(ts, platform, argv, result.output, rc=str(result.returncode))
    return result.returncode


async def _run_provisioning_bg(platform: str, recipe: dict) -> None:
    """Background provisioning chain: batch first, then each post-command.

    Aborts on the first non-zero exit so we don't enable a plugin whose
    config failed to write.
    """
    batch = recipe.get("config_batch") or []
    if batch:
        batch_argv = _AGENT.command("config_set_batch", batch_json=json.dumps(batch))
        rc = await _run_one(platform, batch_argv)
        if rc != 0:
            _note_chain_aborted(platform, "batch")
            return

    try:
        post_argvs = _AGENT.render_recipe_commands(recipe.get("post_commands") or [])
    except (KeyError, ValueError) as e:
        _note_chain_aborted(platform, f"invalid post_commands: {e}")
        return

    for argv in post_argvs:
        rc = await _run_one(platform, argv)
        if rc != 0:
            _note_chain_aborted(platform, " ".join(argv))
            return


def _note_chain_aborted(platform: str, where: str) -> None:
    with _AGENT.provisioning_log.open("a") as log:
        log.write(f"[chain aborted for {platform} at: {where}]\n")


def _append_log(ts: str, platform: str, argv: list[str], output: str, rc: str) -> None:
    """Append one provisioning run to the shared log file. Tokens are never
    in argv (the batch uses `ref: {id: ENV_NAME}`), so logging the full
    argv is safe."""
    with _AGENT.provisioning_log.open("a") as log:
        log.write(f"\n=== {ts} channel-provisioning: {platform} ===\n")
        log.write(f"$ {' '.join(repr(a) if ' ' in a else a for a in argv)}\n")
        log.write(output)
        if not output.endswith("\n"):
            log.write("\n")
        log.write(f"[exit {rc}]\n")


@router.post("/api/channels/add")
async def add_channel(request: Request):
    """Onboarding + Settings → Channels target.

    Body: `{platform: "slack"|"telegram"|"discord", <tokens>...}`. Writes
    tokens to the agent's env file synchronously, then (if the platform has
    a CLI recipe) spawns `_run_provisioning_bg` and returns immediately with
    `restart_required: true` so the UI can show the gateway-restart notice.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = _AGENT.channels.get(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported platform: {platform or '(missing)'}"},
        )

    # Collect + validate every required token before touching disk — we don't
    # want to write half the .env, then fail on a missing second token.
    # Fields not supplied in the body fall back to the manifest's ``defaults``
    # map so the FE form doesn't have to collect knobs like ``allowed_users``
    # (which the manifest defaults to ``*``). To restrict access, edit the
    # default in the active agent's ``config/agents/<agent>/manifest.json``.
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

    try:
        for env_key, value in to_upsert:
            upsert_env_entry(env_key, value)
    except Exception as e:  # noqa: BLE001 — surface the real reason
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to write env file: {e}"},
        )

    has_cli_chain = bool(recipe.get("config_batch")) or bool(recipe.get("post_commands"))
    if not has_cli_chain:
        return {
            "ok": True,
            "platform": platform,
            "restart_required": False,
            "provisioning": "skipped",
            "detail": f"{platform} token saved. Config automation not configured yet.",
        }

    asyncio.create_task(_run_provisioning_bg(platform, recipe))

    return {
        "ok": True,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }
