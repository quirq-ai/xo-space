"""Every broker route, mounted as the agent module's api.

Order is the order ``server.py`` mounted these for years: the auth and
setup routers first, then the status routers, then the ``/api/*`` surface
(``routers/cowork_agent``: the active agent's own routes are resolved
inside it), then the local-layer routers that have not moved into a
module of their own yet.
"""

from __future__ import annotations

from fastapi import APIRouter

from routers.auth.auth import router as auth_router
from routers.auth.claude_setup_token import router as claude_setup_token_router
from routers.auth.codex_setup import router as codex_setup_router
from routers.cowork_agent import all_routers as cowork_agent_routers
from routers.cowork_agent.legacy.openclaw_usage import router as openclaw_usage_router
from routers.status.channels import router as channels_router
from routers.status.models import router as models_router
from routers.status.providers import router as providers_router

router = APIRouter()

for _r in (
    auth_router,
    claude_setup_token_router,
    codex_setup_router,
    openclaw_usage_router,
    models_router,
    channels_router,
    providers_router,
    *cowork_agent_routers,
):
    router.include_router(_r)
