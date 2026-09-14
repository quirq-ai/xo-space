"""Local project management. All filesystem and sharing policy is in services."""
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import StrictStr

from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services import project_management as service
from services.errors import ServiceError

router = APIRouter()


def _require_mutation(request: Request) -> None:
    """Require JSON and the same origin for browsers, including remote Space.

    CLI clients do not send Origin. Proxy-provided scheme/host are handled by
    the server's normal trusted forwarding configuration, not arbitrary headers.
    """
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail={"code": "json_required", "message": "Send an application/json request."})
    origin = request.headers.get("origin")
    denied = request.headers.get("sec-fetch-site") == "cross-site"
    if origin is not None:
        try:
            value = urlsplit(origin)
            port = value.port or (443 if value.scheme == "https" else 80)
            request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
            denied = denied or (value.scheme, value.hostname, port) != (request.url.scheme, request.url.hostname, request_port)
            denied = denied or bool(value.username or value.password or value.path or value.query or value.fragment)
        except ValueError:
            denied = True
    if denied:
        raise HTTPException(status_code=403, detail={"code": "same_origin_required", "message": "Manage projects from this Space's Setup page."})


class CloneProject(ForbidExtra):
    project_id: StrictStr
    repository_url: StrictStr


class RemoveProject(ForbidExtra):
    confirm_project_id: StrictStr


@router.post("/api/xo-projects", status_code=201)
async def clone_project(body: CloneProject, request: Request) -> dict:
    _require_mutation(request)
    try:
        return await service.clone_project(body.project_id, body.repository_url)
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
    try:
        return await service.remove_project(project_id, body.confirm_project_id)
    except ServiceError as exc:
        raise http_error(exc) from exc
