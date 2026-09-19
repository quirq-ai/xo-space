"""BFF routes for the Space inbox.

  GET    /api/inbox?status=open|done|all&limit=N   counts plus the newest-first slice
  POST   /api/inbox                                 201, body {title, body?, kind?, source?, project_id?, link?, url?}
  PATCH  /api/inbox                                 body {ids: [1..500 ids], status}; {updated, missing} in one write
  PATCH  /api/inbox/{item_id}                       body {status}
  DELETE /api/inbox/{item_id}                       {item_id, deleted} (idempotent)

Declarative over services.inbox.service (the Inbox is a property of the
Space, so its package sits beside swarm_api rather than under
cowork_agent; typed errors become HTTP through ``bff/errors.py``).
Plain ``def`` handlers: the service does file I/O, so FastAPI
runs them in its threadpool. No os/pathlib in this module (BFF rule P2).
The id shape and the list statuses are the service's own, not a copy.
Bodies are strict: an unknown key is a 422, and so is an ``ids`` entry
that is not a string; an empty or oversized ``ids`` list is the service's
400 ``invalid_value``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import StrictStr

from services.inbox import service

from routers.errors import ForbidExtra, http_error

router = APIRouter()

ITEM_ID_RE = service.ID_RE
LIST_STATUSES = service.LIST_STATUSES


class CreateItemBody(ForbidExtra):
    title: str
    body: str = ""
    kind: str = "note"
    source: str = "api"
    project_id: Optional[str] = None
    link: Optional[dict] = None
    url: Optional[str] = None


class UpdateItemBody(ForbidExtra):
    status: str


class UpdateManyBody(ForbidExtra):
    ids: list[StrictStr]
    status: str


def _item_id_or_404(item_id: str) -> str:
    if not ITEM_ID_RE.fullmatch(item_id):
        raise HTTPException(status_code=404, detail={"code": "item_not_found", "message": "Inbox item not found."})
    return item_id


@router.get("")
def list_inbox(status: str = Query("open"), limit: int = Query(200, ge=1, le=500)) -> dict:
    if status not in LIST_STATUSES:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_status", "message": "status must be open, done or all."})
    try:
        return service.list_items(status=status, limit=limit)
    except service.InboxError as exc:
        raise http_error(exc)


@router.post("", status_code=201)
def create_inbox_item(body: CreateItemBody) -> dict:
    try:
        return service.create_item(title=body.title, body=body.body, kind=body.kind, source=body.source,
                                   project_id=body.project_id, link=body.link, url=body.url)
    except service.InboxError as exc:
        raise http_error(exc)


@router.patch("")
def update_inbox_items(body: UpdateManyBody) -> dict:
    try:
        return service.update_many(body.ids, body.status)
    except service.InboxError as exc:
        raise http_error(exc)


@router.patch("/{item_id}")
def update_inbox_item(item_id: str, body: UpdateItemBody) -> dict:
    try:
        return service.update_item(_item_id_or_404(item_id), body.status)
    except service.InboxError as exc:
        raise http_error(exc)


@router.delete("/{item_id}")
def delete_inbox_item(item_id: str) -> dict:
    try:
        return {"item_id": item_id, "deleted": service.delete_item(_item_id_or_404(item_id))}
    except service.InboxError as exc:
        raise http_error(exc)
