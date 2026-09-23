"""CLI access controls and a fixed, read-only HTTP command surface."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import StrictBool
from starlette.responses import FileResponse

from routers.browser_guard import is_local_mutation, origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services import cli_access as service
from services.errors import ServiceError
from services.space_tools import InboxStatus, ProjectDocument

router = APIRouter()


def _manage(request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    if not is_local_mutation(request):
        raise HTTPException(403, "Manage CLI access from this Space's local Setup page.", headers={"Cache-Control": "no-store"})


def _authorize(request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    if not origin_allowed(request):
        raise HTTPException(403, "This browser origin is not allowed.", headers={"Cache-Control": "no-store"})
    try:
        service.authenticate(request.headers.get("authorization", ""))
    except ServiceError as exc:
        headers = {"Cache-Control": "no-store"}
        if exc.status == 401:
            headers["WWW-Authenticate"] = 'Bearer realm="Space CLI"'
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}, headers=headers) from exc


class SettingsRequest(ForbidExtra):
    enabled: StrictBool


@router.get("/api/cli-access", dependencies=[Depends(_manage)])
def get_settings() -> dict:
    try:
        return service.status()
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.put("/api/cli-access", dependencies=[Depends(_manage)])
def put_settings(body: SettingsRequest) -> dict:
    try:
        return service.configure(enabled=body.enabled)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.post("/api/cli-access/rotate-token", dependencies=[Depends(_manage)])
def rotate_token() -> dict:
    try:
        return service.configure(rotate=True)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.get(service.DOWNLOAD_PATH, dependencies=[Depends(_manage)])
def download_client() -> FileResponse:
    return FileResponse(service.client_path(), media_type="application/octet-stream", filename="space",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/cli/status", dependencies=[Depends(_authorize)])
def command_status() -> dict:
    return {"enabled": True, "commands": service.COMMANDS}


@router.get("/api/cli/projects", dependencies=[Depends(_authorize)])
async def projects(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    try:
        return await service.list_projects(limit, offset)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.get("/api/cli/document", dependencies=[Depends(_authorize)])
async def document(project_id: str = Query(min_length=1, max_length=200), document: ProjectDocument = Query()) -> dict:
    try:
        return await service.read_document(project_id, document)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.get("/api/cli/todos", dependencies=[Depends(_authorize)])
async def todos(project_id: str = Query(min_length=1, max_length=200), limit: int = Query(50, ge=1, le=100)) -> dict:
    try:
        return await service.list_todos(project_id, limit)
    except ServiceError as exc:
        raise http_error(exc) from exc


@router.get("/api/cli/inbox", dependencies=[Depends(_authorize)])
async def inbox(status: InboxStatus = "open", limit: int = Query(50, ge=1, le=100)) -> dict:
    try:
        return await service.list_inbox(status, limit)
    except ServiceError as exc:
        raise http_error(exc) from exc
