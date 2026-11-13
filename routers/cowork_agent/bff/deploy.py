"""Deploying a project to XO Launchpad: the Deploy panel's routes.

  GET    /api/deploy/flow                    flow.json: states, steps, failures, decisions
  GET    /api/deploy/sizes                   ?template=: machine sizes, the template's default and allowed ones
  GET    /api/deploy/{project}/check         the state and the filled-in form (files only)
  GET    /api/deploy/{project}/preflight     what a deploy would meet on GitHub and Launchpad
  POST   /api/deploy/{project}               deploy; server-sent events (attaches to a running one)
  DELETE /api/deploy/{project}               stop the running deploy
  GET    /api/deploy/{project}/watch         server-sent events: the running deploy, else the live state;
                                             ?run=&after= or Last-Event-ID resume a run (204: it has ended)
  GET    /api/deploy/{project}/apps          Launchpad apps to link, best matches first
  POST   /api/deploy/{project}/link          {app}: link an existing app
  DELETE /api/deploy/{project}/link          ?forget=: unlink (the app keeps running)
  POST   /api/deploy/{project}/agent         {note?, client_id?}: an agent prepares the project

Thin handlers over ``services/launchpad`` (``runs``, ``agent``); the contract
is ``services/launchpad/API.md``. Errors answer ``{"detail": {"code",
"message", "fix"?, "data"?}}``.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator, model_validator

from routers.cowork_agent.bff.errors import ForbidExtra
from services.errors import ServiceError
from services.launchpad import agent, launchpad, runs

router = APIRouter()

APP_NAME = r"^[a-z0-9](?:[a-z0-9-]{0,23}[a-z0-9])?$"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
HEALTH = re.compile(r"/[\x21-\x7e]{0,199}\Z")
MAX_ENV = 200
MAX_ENV_VALUE = 32 * 1024


class DeployRequest(ForbidExtra):
    name: Optional[str] = Field(None, pattern=APP_NAME)
    port: Optional[int] = Field(None, ge=1, le=65535)
    health_path: Optional[str] = Field(None, max_length=200)
    create_repo: bool = False
    repo_name: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    #: Confirms launchpad.json's repo when it is not one of the project's own remotes (``needs confirm_repo``).
    use_repo: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
    recreate: bool = False
    rebuild: bool = False
    size: Optional[str] = Field(None, max_length=20)
    storage_path: Optional[str] = Field(None, max_length=200)
    storage_size: Optional[int] = Field(None, ge=1, le=20)
    env: dict[str, str] = Field(default_factory=dict)
    sensitive: list[str] = Field(default_factory=list, max_length=MAX_ENV)

    @field_validator("size")
    @classmethod
    def _size(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        name = launchpad.size_name(value)
        if name is None:
            raise ValueError("one of " + ", ".join(launchpad.size_names()) + " (or mini, small, medium, large)")
        return name

    @field_validator("storage_path")
    @classmethod
    def _storage_path(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not launchpad.storage_ok(value):
            raise ValueError("an absolute path other than /, without .. or spaces")
        return value

    @model_validator(mode="after")
    def _storage(self) -> "DeployRequest":
        if self.storage_size is not None and not self.storage_path:
            raise ValueError("storage_size needs storage_path")
        return self

    @field_validator("health_path")
    @classmethod
    def _health(cls, value: Optional[str]) -> Optional[str]:
        if value not in (None, "") and not HEALTH.fullmatch(value):
            raise ValueError("must start with / and contain no spaces; empty for a port check")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_ENV:
            raise ValueError(f"at most {MAX_ENV} variables")
        for key, item in value.items():
            if not ENV_NAME.fullmatch(key):
                raise ValueError("names are letters, digits and _, not starting with a digit")
            if len(item) > MAX_ENV_VALUE:
                raise ValueError(f"the value of {key} is too long")
        return value

    @field_validator("sensitive")
    @classmethod
    def _sensitive(cls, value: list[str]) -> list[str]:
        for key in value:
            if not ENV_NAME.fullmatch(key):
                raise ValueError("names are letters, digits and _, not starting with a digit")
        return value


class LinkRequest(ForbidExtra):
    app: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")


class AgentRequest(ForbidExtra):
    note: Optional[str] = Field(None, max_length=4000)
    client_id: Optional[str] = Field(None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")


def _error(exc: ServiceError) -> HTTPException:
    """``{code, message, action}`` plus ``fix`` and ``data`` when there are:
    API.md promises every error outside a stream an ``action`` (``manual``
    for a code flow.json doesn't list, such as ``project_not_found``)."""
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    for extra in ("fix", "action", "data"):
        if getattr(exc, extra, None):
            detail[extra] = getattr(exc, extra)
    detail.setdefault("action", launchpad.action(exc.code))
    return HTTPException(status_code=exc.status, detail=detail)


def _require_json(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail={"code": "json_required",
                                                     "message": "Send an application/json request."})


def _events(source: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(source, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.get("/api/deploy/flow")
async def deploy_flow() -> dict:
    return launchpad.flow()


@router.get("/api/deploy/sizes")
async def deploy_sizes(template: str = Query("github", pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")) -> dict:
    return await runs.sizes(template)


@router.get("/api/deploy/{project}/check")
async def deploy_check(project: str) -> dict:
    try:
        return await runs.check(project)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.get("/api/deploy/{project}/preflight")
async def deploy_preflight(project: str, name: Optional[str] = Query(None, pattern=APP_NAME)) -> dict:
    try:
        return await runs.preflight(project, name)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.post("/api/deploy/{project}")
async def deploy(project: str, request: Request, body: Optional[DeployRequest] = None) -> StreamingResponse:
    _require_json(request)
    body = body or DeployRequest()
    try:
        run, attached = await runs.start_deploy(project, body.model_dump(exclude={"env"}), dict(body.env))
    except ServiceError as exc:
        raise _error(exc) from exc
    return _events(runs.stream(run, attached))


@router.delete("/api/deploy/{project}")
async def deploy_stop(project: str) -> dict:
    try:
        return await runs.stop_deploy(project)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.get("/api/deploy/{project}/watch", response_model=None)
async def deploy_watch(project: str, run: Optional[str] = Query(None, max_length=64),
                       after: Optional[int] = Query(None, ge=0),
                       last_event_id: Optional[str] = Header(None, max_length=100)) -> StreamingResponse | Response:
    try:
        found = await runs.start_watch(project, runs.parse_resume(last_event_id, run, after))
    except ServiceError as exc:
        raise _error(exc) from exc
    if found is None:
        return Response(status_code=204)
    watched, attached, since = found
    return _events(runs.stream(watched, attached, since))


@router.get("/api/deploy/{project}/apps")
async def deploy_apps(project: str) -> dict:
    try:
        return await runs.apps(project)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.post("/api/deploy/{project}/link")
async def deploy_link(project: str, body: LinkRequest, request: Request) -> dict:
    _require_json(request)
    try:
        return await runs.link(project, body.app)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.delete("/api/deploy/{project}/link")
async def deploy_unlink(project: str, forget: bool = False) -> dict:
    try:
        return await runs.unlink(project, forget)
    except ServiceError as exc:
        raise _error(exc) from exc


@router.post("/api/deploy/{project}/agent")
async def deploy_agent(project: str, request: Request, body: Optional[AgentRequest] = None) -> dict:
    _require_json(request)
    body = body or AgentRequest()
    try:
        return await agent.ask_agent(project, note=body.note, client_id=body.client_id)
    except ServiceError as exc:
        raise _error(exc) from exc
