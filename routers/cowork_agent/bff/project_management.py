"""Local project management. All filesystem and sharing policy is in services."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import StrictBool, StrictStr

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services import project_clones
from services import project_management as service
from services.errors import ServiceError

router = APIRouter()


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
    #: As on ``POST /api/files/mkdir`` with ``scaffold``: written to
    #: ``.xo/project.json``; blank or absent means the folder name / no brief.
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    #: False: answer 202 with a clone job at once (``/api/xo-projects/clones/{id}``).
    wait: StrictBool = True


def clone_error(exc: ServiceError) -> HTTPException:
    """``{code, message}``, plus ``detail`` (git's last line) for a failed clone."""
    error = http_error(exc)
    if getattr(exc, "detail", None):
        error.detail["detail"] = exc.detail
    return error


class RemoveProject(ForbidExtra):
    confirm_project_id: StrictStr


@router.post("/api/xo-projects", status_code=201)
async def clone_project(body: CloneProject, request: Request):
    """Clone a repository into a new project.

    Waits for the clone by default (201 ``{project_id, created}``). With
    ``"wait": false`` it answers 202 with the clone job to poll; see
    ``routers/cowork_agent/bff/project_clones.py``. Clone failures carry
    ``code`` ``repo_not_found_or_private``, ``auth_failed``,
    ``host_unreachable``, ``timed_out`` or ``clone_failed``.
    """
    _require_mutation(request)
    details = {"display_name": body.display_name, "description": body.description}
    try:
        if not body.wait:
            job = project_clones.start_clone(body.project_id, body.repository_url, **details)
            return JSONResponse(status_code=202, content=job)
        return await service.clone_project(body.project_id, body.repository_url, **details)
    except ServiceError as exc:
        raise clone_error(exc) from exc


@router.get("/api/xo-projects/{project_id}/removal")
async def removal_status(project_id: str) -> dict:
    try:
        return await service.removal_status(project_id)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.delete("/api/xo-projects/{project_id}")
async def remove_project(project_id: str, body: RemoveProject, request: Request) -> dict:
    _require_mutation(request)
    try:
        return await service.remove_project(project_id, body.confirm_project_id)
    except ServiceError as exc:
        raise http_error(exc) from exc
