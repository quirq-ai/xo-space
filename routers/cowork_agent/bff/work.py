"""BFF routes for the Work section (docs/work-and-workitems.md, section 11).

  GET    /api/work/inbox                      the Inbox page in one read: decisions, calendar, completed, work
  GET    /api/work/attention                  the decisions alone: {items, counts, total}
  GET    /api/work/summary                    the badge's small call: {badge, decisions, completed, ...}
  GET    /api/feed?limit&before&since&sources&kinds&project   the merged stream, newest first
  POST   /api/feed                            201, an agent's note: {title, body?, kind?, source?, project_id?, link?, url?, ref?}
  PUT    /api/work/watermark                  {ts}: seen up to here; only moves forward
  POST   /api/work/dismiss                    {key, since}: hide one condition; 204
  DELETE /api/work/dismiss/{key}              bring it back; 204, idempotent
  POST   /api/work/ack                        {key}: a Completed row, looked at; 204
  DELETE /api/work/ack/{key}                  204, idempotent
  POST   /api/work/promote                    {key, project_id, title?, assignee?}: the entry becomes a work item; 201, or 200 when it already had
  PATCH  /api/work/pins                       {key, pinned}; 204

  The connection folders and their items (design section 17):
  GET    /api/work/inbox/connections                        one row per connection folder, plus the configured connections without one
  PUT    /api/work/inbox/connections/{toolkit}              the policy (items, sessions, retention_days); strict; makes the folder
  GET    /api/work/inbox/items?connection=&status=&limit=   the items, newest first
  GET    /api/work/inbox/items/{toolkit}/{item_id}          the item, its session, its outcome, the workbench files
  GET    /api/work/inbox/items/{toolkit}/{item_id}/thread   the item as a conversation: the fact, the turns, the outcome, running
  POST   /api/work/inbox/items/{toolkit}/{item_id}/reply    {text}: the person writes on the thread; 202 {session_id}; starts the session when there is none
  POST   /api/work/inbox/items/{toolkit}/{item_id}/start    202 {session_id}; ?retry=true runs a failed item again
  POST   /api/work/inbox/items/{toolkit}/{item_id}/decide   {action: accept | dismiss | track, project_id?, title?, assignee?}
  POST   /api/work/inbox/items/{toolkit}/{item_id}/send     202; only when the policy allows acting and a reply is drafted

Declarative over services.work.service (typed errors become HTTP through
``bff/errors.py``). Plain ``def`` handlers: the service does file I/O, so
FastAPI runs them in its threadpool. No os/pathlib here (BFF rule P2).
Bodies are strict (``ForbidExtra``): an unknown key is a 422.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Response
from pydantic import StrictBool

from services.work import service

from routers.cowork_agent.bff.errors import ForbidExtra, http_error

router = APIRouter()


class PostBody(ForbidExtra):
    title: str
    body: str = ""
    kind: str = "note"
    source: str = "api"
    project_id: Optional[str] = None
    link: Optional[dict] = None
    url: Optional[str] = None
    ref: Optional[dict] = None


class WatermarkBody(ForbidExtra):
    ts: str


class DismissBody(ForbidExtra):
    key: str
    since: Optional[str] = None


class KeyBody(ForbidExtra):
    key: str


class PromoteBody(ForbidExtra):
    key: str
    project_id: str
    title: Optional[str] = None
    assignee: Optional[str] = None


class PinBody(ForbidExtra):
    key: str
    pinned: StrictBool


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
    items: dict[str, StrictBool] = {}
    sessions: Optional[SessionsPolicyBody] = None
    retention_days: Optional[int] = None


class ReplyBody(ForbidExtra):
    text: str


class DecideBody(ForbidExtra):
    action: str
    project_id: Optional[str] = None
    title: Optional[str] = None
    assignee: Optional[str] = None


def _csv(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


@router.get("/api/work/inbox")
def work_inbox() -> dict:
    try:
        return service.inbox_page()
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/work/attention")
def work_attention() -> dict:
    try:
        return service.attention_items()
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/work/summary")
def work_summary() -> dict:
    try:
        return service.summary()
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/feed")
def feed(limit: int = Query(100, ge=1, le=500), before: Optional[str] = Query(None), since: Optional[str] = Query(None),
         sources: Optional[str] = Query(None), kinds: Optional[str] = Query(None), project: Optional[str] = Query(None)) -> dict:
    try:
        return service.feed(limit=limit, before=before, since=since, sources=_csv(sources), kinds=_csv(kinds), project=project)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/feed", status_code=201)
def create_post(body: PostBody) -> dict:
    try:
        return service.create_post(title=body.title, body=body.body, kind=body.kind, source=body.source,
                                   project_id=body.project_id, link=body.link, url=body.url, ref=body.ref)
    except service.WorkError as exc:
        raise http_error(exc)


@router.put("/api/work/watermark")
def set_watermark(body: WatermarkBody) -> dict:
    try:
        return service.set_watermark(body.ts)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/work/dismiss", status_code=204, response_class=Response)
def dismiss(body: DismissBody) -> Response:
    try:
        service.dismiss(body.key, body.since)
    except service.WorkError as exc:
        raise http_error(exc)
    return Response(status_code=204)


@router.delete("/api/work/dismiss/{key:path}", status_code=204, response_class=Response)
def undismiss(key: str) -> Response:
    try:
        service.undismiss(key)
    except service.WorkError as exc:
        raise http_error(exc)
    return Response(status_code=204)


@router.post("/api/work/ack", status_code=204, response_class=Response)
def ack(body: KeyBody) -> Response:
    try:
        service.ack(body.key)
    except service.WorkError as exc:
        raise http_error(exc)
    return Response(status_code=204)


@router.delete("/api/work/ack/{key:path}", status_code=204, response_class=Response)
def unack(key: str) -> Response:
    try:
        service.unack(key)
    except service.WorkError as exc:
        raise http_error(exc)
    return Response(status_code=204)


@router.post("/api/work/promote")
def promote(body: PromoteBody, response: Response) -> dict:
    try:
        result = service.promote(key=body.key, project_id=body.project_id, title=body.title, assignee=body.assignee)
    except service.WorkError as exc:
        raise http_error(exc)
    response.status_code = 201 if result["created"] else 200
    return result


@router.patch("/api/work/pins", status_code=204, response_class=Response)
def set_pin(body: PinBody) -> Response:
    try:
        service.set_pin(body.key, body.pinned)
    except service.WorkError as exc:
        raise http_error(exc)
    return Response(status_code=204)


# ── the connection folders and their items ──────────────────────────────────


@router.get("/api/work/inbox/connections")
def inbox_connections() -> dict:
    try:
        return service.inbox_connections()
    except service.WorkError as exc:
        raise http_error(exc)


@router.put("/api/work/inbox/connections/{toolkit}")
def set_connection_policy(toolkit: str, body: PolicyBody) -> dict:
    try:
        return service.set_connection_policy(toolkit, body.model_dump(exclude_unset=True))
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/work/inbox/items")
def list_inbox_items(connection: Optional[str] = Query(None), status: Optional[str] = Query(None),
                     limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        return service.list_inbox_items(connection=connection, status=status, limit=limit)
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/work/inbox/items/{toolkit}/{item_id}")
def inbox_item(toolkit: str, item_id: str) -> dict:
    try:
        return service.inbox_item(toolkit, item_id)
    except service.WorkError as exc:
        raise http_error(exc)


@router.get("/api/work/inbox/items/{toolkit}/{item_id}/thread")
def inbox_item_thread(toolkit: str, item_id: str) -> dict:
    try:
        return service.item_thread(toolkit, item_id)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/work/inbox/items/{toolkit}/{item_id}/reply", status_code=202)
async def reply_inbox_item(toolkit: str, item_id: str, body: ReplyBody) -> dict:
    try:
        return await service.reply_inbox_item(toolkit, item_id, body.text)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/work/inbox/items/{toolkit}/{item_id}/start", status_code=202)
async def start_inbox_item(toolkit: str, item_id: str, retry: bool = Query(False)) -> dict:
    try:
        return await service.start_inbox_item(toolkit, item_id, retry=retry)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/work/inbox/items/{toolkit}/{item_id}/decide")
def decide_inbox_item(toolkit: str, item_id: str, body: DecideBody) -> dict:
    try:
        return service.decide_inbox_item(toolkit, item_id, body.action, project_id=body.project_id, title=body.title,
                                         assignee=body.assignee)
    except service.WorkError as exc:
        raise http_error(exc)


@router.post("/api/work/inbox/items/{toolkit}/{item_id}/send", status_code=202)
async def send_inbox_item(toolkit: str, item_id: str) -> dict:
    try:
        return await service.send_inbox_item(toolkit, item_id)
    except service.WorkError as exc:
        raise http_error(exc)
