"""BFF (Backend-for-Frontend) layer.

Intent-named endpoints that wrap the OS-direct routes under
``/api/files/*`` with curated, filtered responses. The frontend talks
to these routes in nouns (projects, secrets) and never sees raw
filesystem paths.

See docs/bff-endpoints-design.md for the design rules. The aggregator
below is consumed by the parent package's ``all_routers`` so
``server.py`` picks the routes up at mount time.
"""

from fastapi import APIRouter

from .relay import router as relay_router
from .secrets import router as secrets_router
from .visualizer import router as visualizer_router
from .workspace_visualizer import router as workspace_visualizer_router
from .xo_projects import router as xo_projects_router

bff_routers: list[APIRouter] = [
    xo_projects_router,
    relay_router,
    secrets_router,
    visualizer_router,
    workspace_visualizer_router,
]
