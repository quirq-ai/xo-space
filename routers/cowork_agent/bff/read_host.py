"""The Host check on the routes that answer with a project's files.

``routers.browser_guard.read_host_allowed`` decides; this is the FastAPI
dependency the project-file routes declare, so a route function stays a
plain function its tests can call directly.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from routers.browser_guard import read_host_allowed


def require_read_host(request: Request) -> None:
    if not read_host_allowed(request):
        raise HTTPException(status_code=403, detail={
            "code": "host_not_allowed",
            "message": "Project files are only served to this machine's own addresses, this workspace's Coder "
                       "app URL, or an origin in ALLOWED_ORIGINS. Open Space through one of those, or add your "
                       "hostname's origin to ALLOWED_ORIGINS.",
        })
