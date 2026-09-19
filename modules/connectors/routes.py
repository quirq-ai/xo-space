"""The connectors' HTTP surface: one router over the eight connector
routers under ``routers/``, in the order the broker mounted them for years.

  /api/connectors/gdrive/*        Google Drive remotes and OAuth sessions (rclone)
  /api/connectors/onedrive/*      OneDrive remotes and OAuth sessions (rclone)
  /api/connectors/github/*        a pasted token or the gh device flow; status, disconnect
  /api/connectors/magicpath/*     the MagicPath skill and CLI: setup, login, logout, status
  /callback                       the OAuth landing MagicPath and Vercel share
  /api/connectors/vercel/*        a pasted token or PKCE OAuth; status, disconnect
  /.well-known/oauth-protected-resource   Vercel's resource metadata
  /api/connectors/composio/*      the Composio key, toolkits, connect, accounts, scope, prefs, tools
  /mcp/composio-proxy/*           the MCP proxy agents talk to (and /mcp/cowork-proxy/*)

``magicpath`` sits before ``vercel`` on purpose: both answer ``GET /callback``,
and the MagicPath dispatcher claims code-only JWT requests and delegates the
rest to ``vercel_oauth_callback`` unchanged. ``server.py`` mounts the router
through the registry behind the ``connectors`` api gate; the manifest's
aliases name the paths outside ``/api/connectors``. ``GET /api/connectors``
itself still lives in ``routers/cowork_agent/misc.py``.

The eight routers' routes are gathered into one flat list rather than
included: ``include_router`` in this FastAPI is lazy (the parent holds a
placeholder, not the routes), and the registry's namespace check and the
route reference read ``router.routes``. No prefix, tag or dependency is
added on the way, so the flat list is exactly what including them would
mount; the gate is attached by ``server.py`` when it includes this router.
"""

from __future__ import annotations

from fastapi import APIRouter

from .routers import composio, composio_mcp_proxy, gdrive, github_cli, github_pat, magicpath, onedrive, vercel

#: The mount order, magicpath before vercel (see the module docstring).
ROUTERS = (
    gdrive.router,
    onedrive.router,
    github_pat.router,
    github_cli.router,
    magicpath.router,
    vercel.router,
    composio.router,
    composio_mcp_proxy.router,
)

router = APIRouter()
for _r in ROUTERS:
    router.routes.extend(_r.routes)
