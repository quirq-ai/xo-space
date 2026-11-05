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

from .connections import router as connections_router
from .deploy import router as deploy_router
from .inbox import router as inbox_router
from .project_clones import router as project_clones_router
from .project_sharing import router as project_sharing_router
from .project_management import router as project_management_router
from .secrets import router as secrets_router
from .visualizer import router as visualizer_router
from .workspace_visualizer import router as workspace_visualizer_router
from .xo_project_files import router as xo_project_files_router
from .xo_projects import router as xo_projects_router

bff_routers: list[APIRouter] = [
    xo_projects_router,
    xo_project_files_router,
    project_management_router,
    project_sharing_router,
    inbox_router,
    connections_router,
    deploy_router,
    secrets_router,
    visualizer_router,
    workspace_visualizer_router,
    # Last: /api/xo-projects/clones/{id} must not shadow the routes of a
    # project named "clones" (/api/xo-projects/{project_id}/tree, ...).
    project_clones_router,
]
