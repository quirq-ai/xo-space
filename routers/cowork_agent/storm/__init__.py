"""Storm's routes, all under ``/api/chat/`` (design ch 17): thin handlers over
``services.cowork_agent.storm``, one file per route group. Literal segments
are registered before parameter routes.

Mounted from ``routers/cowork_agent/__init__.py`` as ``storm_routers``.
"""

from routers.cowork_agent.storm.runtimes import router as runtimes_router
from routers.cowork_agent.storm.sessions import router as sessions_router
from routers.cowork_agent.storm.status import router as status_router
from routers.cowork_agent.storm.stream import router as stream_router

storm_routers = [status_router, runtimes_router, sessions_router, stream_router]
