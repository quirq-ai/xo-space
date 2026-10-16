"""
Models status API router.

Dispatches on `AGENT_NAME` and returns the active agent's live model status
in the openclaw envelope `{default, models:[{id, status}], ...}`. Hermes
extends the envelope with a `fallback_providers` list; consumers that don't
care about it can ignore the extra field. Agents without a status source
(e.g. claude_code) return HTTP 501.

After every successful fetch the result is mirrored into
`<projects root>/.xo/xo.json` under `models.status` (fire-and-forget) so the
frontend can read a single file for the latest known state.
"""

import asyncio

from fastapi import APIRouter, HTTPException

from services.cowork_agent.adapters.cli_status import CliStatusError
from services.cowork_agent.adapters.loader import load_capability
from services.xo_manifest import patch_status, resolve_agent_name

router = APIRouter(prefix="/models", tags=["models"])

# Map adapter error `code` strings → HTTP status. The codes are the union of
# what both openclaw and hermes status adapters raise.
_ERROR_STATUS = {
    "binary_not_found": 503,
    "timeout": 504,
    "execution_failed": 502,
    "invalid_json": 502,
    "invalid_output": 502,
}


@router.get("/status")
async def models_status():
    """Return per-agent model-centric status. Dispatches on AGENT_NAME."""
    agent = resolve_agent_name()

    # Resolve the active agent's models-status module by AGENT_NAME — no
    # if/elif over agent names. A missing module → 501 ("agent has no status
    # source yet"), distinct from "agent unknown".
    try:
        mod = load_capability("models_status", agent=agent)
    except ModuleNotFoundError:
        raise HTTPException(
            status_code=501,
            detail={
                "ok": False,
                "error": f"no live models-status source for agent '{agent}'",
                "agent": agent,
            },
        )

    try:
        result = await mod.get_models_status()
        # Mirror into xo.json without delaying the response.
        asyncio.create_task(patch_status("models", result))
        return result
    except CliStatusError as e:
        raise HTTPException(
            status_code=_ERROR_STATUS.get(e.code, 502),
            detail={
                "ok": False,
                "error": str(e),
                "code": e.code,
                "detail": e.detail,
                "agent": agent,
            },
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": f"unexpected error: {e}", "agent": agent},
        )
