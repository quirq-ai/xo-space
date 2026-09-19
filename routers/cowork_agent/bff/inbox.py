"""BFF routes for the Space inbox.

  GET    /api/inbox?status=open|done|all&limit=N   counts plus the newest-first slice
  POST   /api/inbox                                 201, body {title, body?, kind?, source?, project_id?, link?, url?}
  PATCH  /api/inbox                                 body {ids: [1..500 ids], status}; {updated, missing} in one write
  PATCH  /api/inbox/{item_id}                       body {status}
  DELETE /api/inbox/{item_id}                       {item_id, deleted} (idempotent)

Declarative over services.inbox.service (the Inbox is a property of the
Space, so its package sits beside swarm_api rather than under
cowork_agent). The item id goes through as given: the service answers a
malformed one with its own 404 before the file is opened, and every typed
failure reaches the wire through the app's service error handler
(``routers/errors.py``). Plain ``def`` handlers: the service does file I/O,
so FastAPI runs them in its threadpool. No os/pathlib in this module (BFF
rule P2). Bodies are strict: an unknown key is a 422, and so is an ``ids``
entry that is not a string; an empty or oversized ``ids`` list is the
service's 400 ``invalid_value``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import StrictStr

from routers.errors import ForbidExtra
from services.errors import ServiceError
from services.inbox import service

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


@router.get("/api/inbox")
def list_inbox(status: str = Query("open"), limit: int = Query(200, ge=1, le=500)) -> dict:
    if status not in LIST_STATUSES:
        raise ServiceError("invalid_status", "status must be open, done or all.")
    return service.list_items(status=status, limit=limit)


@router.post("/api/inbox", status_code=201)
def create_inbox_item(body: CreateItemBody) -> dict:
    return service.create_item(title=body.title, body=body.body, kind=body.kind, source=body.source,
                               project_id=body.project_id, link=body.link, url=body.url)


@router.patch("/api/inbox")
def update_inbox_items(body: UpdateManyBody) -> dict:
    return service.update_many(body.ids, body.status)


@router.patch("/api/inbox/{item_id}")
def update_inbox_item(item_id: str, body: UpdateItemBody) -> dict:
    return service.update_item(item_id, body.status)


@router.delete("/api/inbox/{item_id}")
def delete_inbox_item(item_id: str) -> dict:
    return {"item_id": item_id, "deleted": service.delete_item(item_id)}
