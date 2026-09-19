"""``GET /api/models``: one model row per agent/profile under the active backend."""

from fastapi import APIRouter

from services.cowork_agent.adapters.loader import try_load_capability

router = APIRouter()


@router.get("")
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
