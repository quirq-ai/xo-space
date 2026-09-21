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

  The Inbox's own routes (the work items with a session each) are
  ``routers/cowork_agent/bff/inbox.py`` (docs section 18).

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
