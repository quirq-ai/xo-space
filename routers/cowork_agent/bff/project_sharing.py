"""BFF routes for the commit relay — the Space UI's only relay surface.

  GET  /api/project-sharing/status                      window into the poller
  GET  /api/xo-projects/{id}/commits          local git read (origin/<branch>, behind count)
  GET  /api/xo-projects/{id}/members          proxy to swarm
  POST /api/xo-projects/{id}/share            proxy to swarm, body {workspace_id}
  POST /api/xo-projects/{id}/revoke           proxy to swarm, body {workspace_id}
  POST /api/xo-projects/{id}/apply            git merge --ff-only origin/<branch> (the Apply button)
  POST /api/project-sharing/check             nudge the poller: next tick now (the Check now button)

Declarative over services.cowork_agent.project_sharing.service (typed errors →
HTTP here). share, revoke and apply run as qq commands first (routers/qq_ops.py);
the *_in_process functions are their fallback and what those commands run. The
relay's nudge lives in this process, so the route nudges after a qq answer. No os/pathlib in this module (BFF rule P2). The browser never
talks to swarm and never sees the token.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.cowork_agent.bff.filters import is_valid_workspace_id
from routers.qq_ops import qq_first
from services.cowork_agent.project_sharing import service

router = APIRouter()

MAX_COMMITS = 50
QQ_TIMEOUT = 60.0   # one swarm call or one git fast-forward


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


@router.post("/api/project-sharing/check")
def relay_check() -> dict:
    return service.check_now()


@router.post("/api/xo-projects/{project_id}/apply")
async def apply_project(project_id: str) -> dict:
    result = await qq_first(["apply", f"--project={project_id}"], QQ_TIMEOUT,
                            lambda: apply_project_in_process(project_id))
    service.check_now()   # the behind count in the next status snapshot drops to 0
    return result


async def apply_project_in_process(project_id: str) -> dict:
    try:
        return await service.apply(project_id)
    except service.RelayError as exc:
        raise _http(exc)


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
    result = await qq_first(["share", f"--project={project_id}", f"--space={target}"], QQ_TIMEOUT,
                            lambda: share_project_in_process(project_id, body))
    service.check_now()   # our own status flips to "shared" within a second
    return result


async def share_project_in_process(project_id: str, body: ShareBody) -> dict:
    target = _workspace_id_or_422(body, "recipient's")
    try:
        return await service.share(project_id, target)
    except service.RelayError as exc:
        raise _http(exc)


@router.post("/api/xo-projects/{project_id}/revoke")
async def revoke_project(project_id: str, body: ShareBody) -> dict:
    target = _workspace_id_or_422(body, "revoked")
    result = await qq_first(["revoke", f"--project={project_id}", f"--space={target}"], QQ_TIMEOUT,
                            lambda: revoke_project_in_process(project_id, body))
    service.check_now()
    return result


async def revoke_project_in_process(project_id: str, body: ShareBody) -> dict:
    target = _workspace_id_or_422(body, "revoked")
    try:
        return await service.revoke(project_id, target)
    except service.RelayError as exc:
        raise _http(exc)
