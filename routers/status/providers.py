"""
Providers status API router.

Reports connection state (OAuth + API key) for every model provider that is
``enabled: true`` in xo.json's ``models`` section. ``agent`` and ``api_keys``
are ``space_agent()``'s, since each agent has its own env source for API keys;
``oauth`` also carries each other installed runtime's own entries, read from
the space advisory's runtime facts, since every runtime signs in on every Space.

Disabled providers are omitted from the response entirely — the frontend
drives section visibility off xo.json's `enabled` flags, so this endpoint
only reports on what is in scope to render. ``connected: false`` therefore
always means "enabled but not authenticated", never "disabled in manifest".

Unlike ``/models/status`` and ``/channels/status`` this result is **not**
mirrored into xo.json: connection state can flip between probes (the user
runs ``claude auth login``, edits .env, etc.), so we keep it live-only.
"""

from fastapi import APIRouter, HTTPException

from services import advisory
from services.cowork_agent.adapters.loader import load_capability
from services.cowork_agent.providers_status_lib import _leaf_enabled, _read_xo_models
from services.cowork_agent.registry.agent_registry import runtimes, space_agent
from services.xo_manifest import own_oauth

READ_WAIT_S = 5.0       # how long one runtime's login read may take

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
        status = await mod.get_providers_status()
        await _add_runtimes_oauth(status, agent)
        return status
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": f"unexpected error: {e}", "agent": agent},
        )


async def _add_runtimes_oauth(status: dict, agent: str) -> None:
    """Add the xo.json-enabled oauth entries of each other installed runtime that ``status`` lacks, from the advisory."""
    oauth = status.get("oauth")
    if not isinstance(oauth, dict):
        return
    models = _read_xo_models(agent)
    wanted: dict[str, list[str]] = {}
    for runtime in runtimes():
        keys = [key for key in own_oauth(runtime.name) if key not in oauth and _leaf_enabled(models, "oauth", key)]
        if runtime.name != agent and keys:
            wanted[runtime.name] = keys
    if not wanted:
        return
    facts = await advisory.current().read_runtimes(READ_WAIT_S, wanted)
    for name, keys in wanted.items():
        known = facts.get(name)
        if known is not None and known.installed:
            connected = known.signed_in and not known.via_api_key
            oauth.update({key: {"connected": connected} for key in keys if key not in oauth})
