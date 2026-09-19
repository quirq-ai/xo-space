"""Router aggregation for the cowork_agent subpackage.

Each route module exposes a single `router: APIRouter`. `all_routers` is the
ordered list `server.py` uses to mount them onto the FastAPI app.

Migrated from bridge/routes/__init__.py on 2026-04-20. Bridge's health route
is intentionally not migrated (xo-space's existing /health stays).
"""

from fastapi import APIRouter

from services.cowork_agent.adapters.loader import try_load_capability

from .agents import router as agents_router
from .channels import router as channels_router
from .config import router as config_router
from .files import router as files_router
from .fts import router as fts_router
from .misc import router as misc_router
from .quirq_state import router as quirq_state_router
from .skills import router as skills_router
from .workspace_memory import router as workspace_memory_router
from .bff import bff_routers
from .xo_projects_sync import router as xo_projects_sync_router


def _active_agent_routes() -> list[APIRouter]:
    """Mount the active agent's own routes, resolved by AGENT_NAME.

    Agent-specific endpoint surfaces (e.g. hermes profile management) live at
    ``services/cowork_agent/adapters/<AGENT_NAME>/routes.py``. They are mounted
    only when that agent is active — no core code names a specific agent, and
    an agent without a ``routes`` module simply contributes nothing.
    """
    mod = try_load_capability("routes")
    router = getattr(mod, "router", None) if mod else None
    return [router] if router is not None else []


all_routers: list[APIRouter] = [
    agents_router,
    config_router,
    channels_router,
    *_active_agent_routes(),
    files_router,
    workspace_memory_router,
    fts_router,
    skills_router,
    misc_router,
    quirq_state_router,
    *bff_routers,
    xo_projects_sync_router,
]
