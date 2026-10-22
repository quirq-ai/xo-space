"""Storm's routes, all under ``/api/chat/`` (design ch 17): thin handlers over
``services.cowork_agent.storm``, one file per route group. Literal segments
are registered before parameter routes.

Mounted from ``routers/cowork_agent/__init__.py`` as ``storm_routers``. The
comms endpoint (``comms.py``) sits beside today's ``/mcp/*`` proxies, not
under ``/api/chat/`` (ch 17 §2.9).
"""

from routers.cowork_agent.storm.chats import router as chats_router
from routers.cowork_agent.storm.comms import router as comms_router
from routers.cowork_agent.storm.review import router as review_router
from routers.cowork_agent.storm.router import router as router_router
from routers.cowork_agent.storm.rules import router as rules_router
from routers.cowork_agent.storm.runtimes import router as runtimes_router
from routers.cowork_agent.storm.sessions import router as sessions_router
from routers.cowork_agent.storm.status import router as status_router
from routers.cowork_agent.storm.stream import router as stream_router
from routers.cowork_agent.storm.worktrees import router as worktrees_router

storm_routers = [status_router, runtimes_router, sessions_router, stream_router, review_router, rules_router,
                 worktrees_router, chats_router, router_router, comms_router]
