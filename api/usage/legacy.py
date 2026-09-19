"""
Backward-compat URL alias for legacy ``/openclaw/usage/*`` paths.

Every endpoint here binds to the **same handler function** from
``api/usage/routes.py``. ``/openclaw/usage/X`` and
``/api/usage/X`` return byte-identical JSON because they ARE the same
handler under two URLs — no duplicate logic.

All usage computation lives in the per-agent module at
``services/cowork_agent/adapters/<AGENT_NAME>/usage.py``, resolved via
``services.cowork_agent.engine.usage_loader.load_usage_module()``. This file
contains only route registration; flip a route by editing the canonical
handler in ``api/usage/routes.py``. The alias is an ``absolute_router``: the
loader mounts it with no folder prefix.
"""
from fastapi import APIRouter

from api.usage.routes import (
    usage_analytics,
    usage_summary,
    usage_summary_card,
    usage_sessions,
    usage_session,
)

absolute_router = APIRouter(prefix="/openclaw/usage", tags=["openclaw-usage-legacy"])

absolute_router.get("/analytics")(usage_analytics)
absolute_router.get("/summary")(usage_summary)
absolute_router.get("/summary/card")(usage_summary_card)
absolute_router.get("/sessions")(usage_sessions)
absolute_router.get("/sessions/{session_id}")(usage_session)
