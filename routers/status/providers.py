"""
Providers status API router.

Reports connection state (OAuth + API key) for every model provider that is
``enabled: true`` in xo.json's ``models`` section. Dispatches on
``space_agent()`` because each agent has its own env source for API keys, even
though the OAuth probes (claude / codex) are agent-independent.

Disabled providers are omitted from the response entirely — the frontend
drives section visibility off xo.json's `enabled` flags, so this endpoint
only reports on what is in scope to render. ``connected: false`` therefore
always means "enabled but not authenticated", never "disabled in manifest".

Unlike ``/models/status`` and ``/channels/status`` this result is **not**
mirrored into xo.json: connection state can flip between probes (the user
runs ``claude auth login``, edits .env, etc.), so we keep it live-only.
"""

from fastapi import APIRouter, HTTPException

from services.cowork_agent.adapters.loader import load_capability
from services.cowork_agent.registry.agent_registry import space_agent

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/status")
async def providers_status():
    """Return per-agent provider connection status."""
    agent = space_agent()

    # Resolve the active agent's providers-status module by space_agent() — no
    # if/elif over agent names. Missing module → 501.
    try:
        mod = load_capability("providers_status", agent=agent)
    except ModuleNotFoundError:
        raise HTTPException(
            status_code=501,
            detail={
                "ok": False,
                "error": f"no providers-status source for agent '{agent}'",
                "agent": agent,
            },
        )

    try:
        return await mod.get_providers_status()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": f"unexpected error: {e}", "agent": agent},
        )
