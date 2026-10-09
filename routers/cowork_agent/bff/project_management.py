"""Local project management. All filesystem and sharing policy is in services.

Add and remove run as qq commands first (``qq projects add|remove``, routers/qq_ops.py); the
*_in_process functions are their fallback and what those commands run. The request guards stay
here, before qq."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import StrictStr

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from routers.qq_ops import qq_first
from services import project_management as service
from services.errors import ServiceError

router = APIRouter()

CLONE_QQ_TIMEOUT = service.CLONE_TIMEOUT + 60   # the clone's own limit, plus the checks around it
REMOVE_QQ_TIMEOUT = 120.0


def _require_mutation(request: Request) -> None:
    """Require JSON and the same origin for browsers, including remote Space.

    CLI clients do not send Origin. No loopback-peer requirement here, unlike
    the command routes: a Docker install reaches the server from its bridge
    address. See ``routers/browser_guard.py`` for the origin rule.
    """
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail={"code": "json_required", "message": "Send an application/json request."})
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail={"code": "same_origin_required", "message": "Manage projects from this Space's Setup page."})


class CloneProject(ForbidExtra):
    project_id: StrictStr
    repository_url: StrictStr


class RemoveProject(ForbidExtra):
    confirm_project_id: StrictStr


@router.post("/api/xo-projects", status_code=201)
async def clone_project(body: CloneProject, request: Request) -> dict:
    _require_mutation(request)
    return await qq_first(
        ["projects", "add", f"--project={body.project_id}", f"--url={body.repository_url}"], CLONE_QQ_TIMEOUT,
        lambda: clone_project_in_process(body.project_id, body.repository_url))


async def clone_project_in_process(project_id: str, repository_url: str) -> dict:
    try:
        return await service.clone_project(project_id, repository_url)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.get("/api/xo-projects/{project_id}/removal")
async def removal_status(project_id: str) -> dict:
    try:
        return await service.removal_status(project_id)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.delete("/api/xo-projects/{project_id}")
async def remove_project(project_id: str, body: RemoveProject, request: Request) -> dict:
    _require_mutation(request)
    return await qq_first(
        ["projects", "remove", f"--project={project_id}", f"--confirm={body.confirm_project_id}"],
        REMOVE_QQ_TIMEOUT, lambda: remove_project_in_process(project_id, body.confirm_project_id))


async def remove_project_in_process(project_id: str, confirm_project_id: str) -> dict:
    try:
        return await service.remove_project(project_id, confirm_project_id)
    except ServiceError as exc:
        raise http_error(exc) from exc
