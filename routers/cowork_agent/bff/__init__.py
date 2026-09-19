"""BFF (Backend-for-Frontend) layer.

Intent-named endpoints that wrap the OS-direct routes under
``/api/files/*`` with curated, filtered responses. The frontend talks
to these routes in nouns (projects, secrets) and never sees raw
filesystem paths.

See docs/bff-endpoints-design.md for the design rules. The aggregator
below is consumed by the parent package's ``all_routers`` so
``server.py`` picks the routes up at mount time. The project list, the
project records and the workitem rollup moved to ``modules/projects``,
which the registry mounts on its own; the usage and analytics routes stay
here until the telemetry module takes them.
"""

from fastapi import APIRouter

from .inbox import router as inbox_router
from .workspace_visualizer import router as workspace_visualizer_router

bff_routers: list[APIRouter] = [
    inbox_router,
    workspace_visualizer_router,
]
