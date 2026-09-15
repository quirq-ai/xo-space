"""
OpenClaw chat capability.

OpenClaw chat runs through the shared AgentDispatcher like every other
backend (``adapter.stream``). This module only contributes
``resolve_agent_id``: a prompt with no project that names ``model:
"<prefix>/<agent>"`` selects that OpenClaw agent.
"""
from __future__ import annotations

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.adapters.openclaw.paths import OPENCLAW_MODEL_PREFIX


def resolve_agent_id(body: dict) -> str | None:
    """The agent named by a ``<prefix>/<agent>`` model, or None."""
    model = body.get("model")
    if not isinstance(model, str):
        return None
    stripped = model.strip()
    if not stripped.lower().startswith(f"{OPENCLAW_MODEL_PREFIX.lower()}/"):
        return None
    rest = stripped.split("/", 1)[1].strip()
    return normalize_agent_id(rest) if rest else None
