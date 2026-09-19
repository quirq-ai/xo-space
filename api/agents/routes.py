"""
Agent CRUD endpoints — thin forwarding to each backend's ``agents`` capability.

Dispatch (no backend is named here — everything resolves through
``load_capability("agents", …)``):

- ``GET  /api/agents``        → the ACTIVE backend's ``agents.list_agents()``
- ``POST /api/agents``        → ``agents.create_agent(body)`` for ``body.backend``
                                (defaults to the active agent when omitted)
- ``GET/PATCH/DELETE /api/agents/{id}`` → ownership resolution: try each
  installed adapter's ``agents`` capability in registry order; the first whose
  hook returns non-None handles the request.

The per-backend logic (openclaw.json mutation, hermes profile CLI, claude_code
project records) lives in ``adapters/<name>/agents.py``.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.registry.agent_registry import get_active_agent
from services.cowork_agent.registry.adapter_registry import list_adapters
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.helpers import normalize_agent_id

router = APIRouter()


# ── Pydantic request bodies (shared request shapes; passed opaquely to adapters) ─


class CreateAgentBody(BaseModel):
    """Payload for POST /api/agents."""

    name: str = Field(..., min_length=1, max_length=200)
    id: str | None = Field(None, max_length=80)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    # Target backend; defaults to the active agent when omitted. Validated at
    # dispatch against the installed adapters (via load_capability), so no
    # backend name is hardcoded here.
    backend: str | None = Field(None, max_length=80)


class UpdateAgentBody(BaseModel):
    """PATCH /api/agents/{id} — only fields present in the JSON body are applied."""

    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, max_length=400)
    identity_name: str | None = Field(None, max_length=200)
    identity_emoji: str | None = Field(None, max_length=32)
    # Hermes-only today: writes to ``<profile>/SOUL.md``. OpenClaw uses
    # ``identity_*`` for the same role; claude_code doesn't take it.
    system_prompt: str | None = Field(None, max_length=64_000)


# ── Ownership resolution ──────────────────────────────────────────────────────


def get_agent_detail(agent_id: str) -> dict | None:
    """Full agent snapshot from whichever backend owns ``agent_id``.

    Tries each installed adapter's ``agents.get_detail`` in registry order and
    returns the first non-None result (an id is owned by one backend in
    practice). Returns None if no backend recognizes it.
    """
    for name in list_adapters():
        mod = try_load_capability("agents", agent=name)
        getter = getattr(mod, "get_detail", None) if mod else None
        if getter:
            detail = getter(agent_id)
            if detail is not None:
                return detail
    return None


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("")
def list_agents():
    """Return the sidebar agents for the active backend (``AGENT_NAME`` env).

    Only the active backend's agents are surfaced; the others stay invisible —
    matching the user's mental model that ``AGENT_NAME`` decides which world
    we're in, so chats can't accidentally route through the wrong backend.
    """
    mod = try_load_capability("agents")
    if mod is None or not hasattr(mod, "list_agents"):
        return []
    return mod.list_agents()


@router.post("")
def create_agent(body: CreateAgentBody):
    agent_id = normalize_agent_id((body.id or body.name).strip())
    if agent_id == "main":
        return JSONResponse(status_code=400, content={"detail": 'Agent id "main" is reserved; choose another id or name.'})

    backend = (body.backend or "").strip() or get_active_agent().name
    mod = try_load_capability("agents", agent=backend)
    if mod is None or not hasattr(mod, "create_agent"):
        return JSONResponse(status_code=400, content={"detail": f"Unknown or unsupported backend: {backend}"})
    return mod.create_agent(body)


@router.get("/{agent_id}")
def get_agent(agent_id: str):
    detail = get_agent_detail(agent_id)
    if not detail:
        return JSONResponse(status_code=404, content={"detail": f'Agent "{agent_id}" not found'})
    return detail


@router.delete("/{agent_id}")
def delete_agent(agent_id: str):
    """Delete an agent, resolved to whichever backend owns the id. Backends
    without a delete contract (openclaw / claude_code today) return None, so an
    unknown id falls through to a generic 404."""
    for name in list_adapters():
        mod = try_load_capability("agents", agent=name)
        deleter = getattr(mod, "delete", None) if mod else None
        if deleter:
            result = deleter(agent_id)
            if result is not None:
                return result
    return JSONResponse(
        status_code=404,
        content={"detail": f'Agent "{agent_id}" not found, or its backend has no delete contract.'},
    )


@router.patch("/{agent_id}")
def patch_agent(agent_id: str, body: UpdateAgentBody):
    for name in list_adapters():
        mod = try_load_capability("agents", agent=name)
        patcher = getattr(mod, "patch", None) if mod else None
        if patcher:
            result = patcher(agent_id, body)
            if result is not None:
                return result
    return JSONResponse(status_code=404, content={"detail": f'Agent "{agent_id}" not found'})
