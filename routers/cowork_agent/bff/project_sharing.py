"""BFF routes for the commit relay — the Space UI's only relay surface.

  GET  /api/project-sharing/status                      window into the poller
  GET  /api/xo-projects/{id}/commits          local git read (origin/<branch>, behind count)
  GET  /api/xo-projects/{id}/members          proxy to swarm
  POST /api/xo-projects/{id}/share            proxy to swarm, body {workspace_id}
  POST /api/xo-projects/{id}/revoke           proxy to swarm, body {workspace_id}

Declarative over services.cowork_agent.project_sharing.service (typed errors →
HTTP here). No os/pathlib in this module (BFF rule P2). The browser never
talks to swarm and never sees the token.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.cowork_agent.bff.filters import is_valid_workspace_id
from services.cowork_agent.project_sharing import service

router = APIRouter()

MAX_COMMITS = 50


class ShareBody(BaseModel):
    workspace_id: str = ""


def _http(exc: service.RelayError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


def _workspace_id_or_422(body: ShareBody, what: str) -> str:
    if not is_valid_workspace_id(body.workspace_id):
        raise HTTPException(status_code=422, detail={
            "code": "missing_workspace_id", "message": f"Enter the {what} workspace id."})
    return body.workspace_id.strip()


@router.get("/api/project-sharing/status")
def relay_status() -> dict:
    return service.status_snapshot()


@router.get("/api/xo-projects/{project_id}/commits")
async def project_commits(project_id: str, limit: int = 20) -> dict:
    try:
        return await service.project_commits(project_id, max(1, min(int(limit), MAX_COMMITS)))
    except service.RelayError as exc:
        raise _http(exc)


@router.get("/api/xo-projects/{project_id}/members")
async def project_members(project_id: str) -> dict:
    try:
        return await service.members(project_id)
    except service.RelayError as exc:
        raise _http(exc)


@router.post("/api/xo-projects/{project_id}/share")
async def share_project(project_id: str, body: ShareBody) -> dict:
    target = _workspace_id_or_422(body, "recipient's")
    try:
        return await service.share(project_id, target)
    except service.RelayError as exc:
        raise _http(exc)


@router.post("/api/xo-projects/{project_id}/revoke")
async def revoke_project(project_id: str, body: ShareBody) -> dict:
    target = _workspace_id_or_422(body, "revoked")
    try:
        return await service.revoke(project_id, target)
    except service.RelayError as exc:
        raise _http(exc)
