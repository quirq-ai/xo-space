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
from .chat import router as chat_router
from .config import router as config_router
from .files import router as files_router
from .fts import router as fts_router
from .connectors.gdrive import router as gdrive_router
from .connectors.github_cli import router as github_cli_router
from .connectors.github_pat import router as github_pat_router
from .connectors.magicpath import router as magicpath_router
from .connectors.manus import router as manus_router
from .misc import router as misc_router
from .onboarding import router as onboarding_router
from .quirq_state import router as quirq_state_router
from .runtime_config import router as runtime_config_router
from .connectors.onedrive import router as onedrive_router
from .secrets import router as secrets_router
from .sessions import router as sessions_router
from .skills import router as skills_router
from .usage import router as usage_router
from .connectors.vercel import router as vercel_router
from .workspace_memory import router as workspace_memory_router
from .bff import bff_routers
from .opentelemetry import router as opentelemetry_router
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
    sessions_router,
    opentelemetry_router,
    chat_router,
    agents_router,
    config_router,
    channels_router,
    *_active_agent_routes(),
    files_router,
    workspace_memory_router,
    secrets_router,
    usage_router,
    fts_router,
    skills_router,
    misc_router,
    onboarding_router,
    quirq_state_router,
    runtime_config_router,
    gdrive_router,
    onedrive_router,
    github_pat_router,
    github_cli_router,
    # magicpath before vercel: its GET /callback dispatcher must match first;
    # it delegates vercel-shaped requests to vercel_oauth_callback unchanged.
    magicpath_router,
    vercel_router,
    manus_router,
    *bff_routers,
    xo_projects_sync_router,
]
