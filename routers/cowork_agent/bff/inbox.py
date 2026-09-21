"""BFF routes for the Space Inbox (docs/work-and-workitems.md section 18).

  GET    /api/inbox?section=&entity=&state=&limit=      the rows, newest first, plus the sections summary
  GET    /api/inbox/sections                             one row per section: label, counts, entities, policy; runner {enabled}
  PUT    /api/inbox/sections/{section}                   the policy (strict body: sessions{...}, retention_days)
  POST   /api/inbox                                      201: {title, body?, kind?, source?, project_id?, link?, url?} -> a post work item row
  GET    /api/inbox/{project_id}/{workitem_id}           the row, fact, session, outcome, claim, workitem (projected record),
                                                         transcript {session_id, native_session_id}, policy, running, can_reply, can_send
  POST   /api/inbox/{project_id}/{workitem_id}/reply     {text} -> 202 {session_id}
  POST   /api/inbox/{project_id}/{workitem_id}/start     ?retry=true -> 202 {session_id}
  POST   /api/inbox/{project_id}/{workitem_id}/send      202
  POST   /api/inbox/{project_id}/{workitem_id}/archive   {reason?: completed | not_planned} -> the row
  POST   /api/inbox/{project_id}/{workitem_id}/reopen    the row

Declarative over services.inbox.service (the Inbox is a property of the
Space, so its package sits beside swarm_api rather than under
cowork_agent; typed errors become HTTP through ``bff/errors.py``). The
reads and the marks are plain ``def`` handlers (the service does file
I/O, so FastAPI runs them in its threadpool); the actions that start or
resume a session are ``async`` because the runner is. No os/pathlib in
this module (BFF rule P2). Bodies are strict: an unknown key is a 422.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import StrictBool

from services.errors import ServiceError
from services.inbox import service

from routers.cowork_agent.bff.errors import ForbidExtra, http_error

router = APIRouter()


class CreateItemBody(ForbidExtra):
    title: str
    body: str = ""
    kind: str = "note"
    source: str = "api"
    project_id: Optional[str] = None
    link: Optional[dict] = None
    url: Optional[str] = None


class SessionsPolicyBody(ForbidExtra):
    mode: Optional[str] = None
    kinds: Optional[list[str]] = None
    agent_type: Optional[str] = None
    runtime: Optional[str] = None
    max_concurrent: Optional[int] = None
    max_per_hour: Optional[int] = None
    timeout_s: Optional[int] = None
    act: Optional[StrictBool] = None


class PolicyBody(ForbidExtra):
    sessions: Optional[SessionsPolicyBody] = None
    retention_days: Optional[int] = None


class ReplyBody(ForbidExtra):
    text: str


class ArchiveBody(ForbidExtra):
    reason: Optional[str] = None


@router.get("/api/inbox")
def list_inbox(section: Optional[str] = Query(None), entity: Optional[str] = Query(None), state: str = Query("open"),
               limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        return service.list_rows(section=section, entity=entity, state=state, limit=limit)
    except ServiceError as exc:
        raise http_error(exc)


@router.get("/api/inbox/sections")
def inbox_sections() -> dict:
    try:
        return service.sections()
    except ServiceError as exc:
        raise http_error(exc)


@router.put("/api/inbox/sections/{section}")
def set_section_policy(section: str, body: PolicyBody) -> dict:
    try:
        return service.set_policy(section, body.model_dump(exclude_unset=True))
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox", status_code=201)
def create_inbox_item(body: CreateItemBody) -> dict:
    try:
        return service.create_post(title=body.title, body=body.body, kind=body.kind, source=body.source,
                                   project_id=body.project_id, link=body.link, url=body.url)
    except ServiceError as exc:
        raise http_error(exc)


@router.get("/api/inbox/{project_id}/{workitem_id}")
def inbox_item(project_id: str, workitem_id: str) -> dict:
    try:
        return service.item_detail(project_id, workitem_id)
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox/{project_id}/{workitem_id}/reply", status_code=202)
async def reply_inbox_item(project_id: str, workitem_id: str, body: ReplyBody) -> dict:
    try:
        return await service.reply(project_id, workitem_id, body.text)
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox/{project_id}/{workitem_id}/start", status_code=202)
async def start_inbox_item(project_id: str, workitem_id: str, retry: bool = Query(False)) -> dict:
    try:
        return await service.start(project_id, workitem_id, retry=retry)
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox/{project_id}/{workitem_id}/send", status_code=202)
async def send_inbox_item(project_id: str, workitem_id: str) -> dict:
    try:
        return await service.send(project_id, workitem_id)
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox/{project_id}/{workitem_id}/archive")
def archive_inbox_item(project_id: str, workitem_id: str, body: Optional[ArchiveBody] = None) -> dict:
    try:
        return service.archive(project_id, workitem_id, reason=body.reason if body else None)
    except ServiceError as exc:
        raise http_error(exc)


@router.post("/api/inbox/{project_id}/{workitem_id}/reopen")
def reopen_inbox_item(project_id: str, workitem_id: str) -> dict:
    try:
        return service.reopen(project_id, workitem_id)
    except ServiceError as exc:
        raise http_error(exc)
