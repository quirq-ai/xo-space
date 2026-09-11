"""BFF routes for the Space inbox.

  GET    /api/inbox?status=open|done|all&limit=N   counts plus the newest-first slice
  POST   /api/inbox                                 201, body {title, body?, kind?, source?, project_id?, link?, url?}
  PATCH  /api/inbox/{item_id}                       body {status}
  DELETE /api/inbox/{item_id}                       {item_id, deleted} (idempotent)

Declarative over services.inbox.service (the Inbox is a property of the
Space, so its package sits beside swarm_api rather than under
cowork_agent; typed errors become
HTTP here). Plain ``def`` handlers: the service does file I/O, so FastAPI
runs them in its threadpool. No os/pathlib in this module (BFF rule P2).
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from services.inbox import service

router = APIRouter()

ITEM_ID_RE = re.compile(r"[0-9a-f]{8}")
LIST_STATUSES = ("open", "done", "all")


class _ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateItemBody(_ForbidExtra):
    title: str
    body: str = ""
    kind: str = "note"
    source: str = "api"
    project_id: Optional[str] = None
    link: Optional[dict] = None
    url: Optional[str] = None


class UpdateItemBody(_ForbidExtra):
    status: str


def _http(exc: service.InboxError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


def _item_id_or_404(item_id: str) -> str:
    if not ITEM_ID_RE.fullmatch(item_id):
        raise HTTPException(status_code=404, detail={"code": "item_not_found", "message": "Inbox item not found."})
    return item_id


@router.get("/api/inbox")
def list_inbox(status: str = Query("open"), limit: int = Query(200, ge=1, le=500)) -> dict:
    if status not in LIST_STATUSES:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_status", "message": "status must be open, done or all."})
    try:
        return service.list_items(status=status, limit=limit)
    except service.InboxError as exc:
        raise _http(exc)


@router.post("/api/inbox", status_code=201)
def create_inbox_item(body: CreateItemBody) -> dict:
    try:
        return service.create_item(title=body.title, body=body.body, kind=body.kind, source=body.source,
                                   project_id=body.project_id, link=body.link, url=body.url)
    except service.InboxError as exc:
        raise _http(exc)


@router.patch("/api/inbox/{item_id}")
def update_inbox_item(item_id: str, body: UpdateItemBody) -> dict:
    try:
        return service.update_item(_item_id_or_404(item_id), body.status)
    except service.InboxError as exc:
        raise _http(exc)


@router.delete("/api/inbox/{item_id}")
def delete_inbox_item(item_id: str) -> dict:
    try:
        return {"item_id": item_id, "deleted": service.delete_item(_item_id_or_404(item_id))}
    except service.InboxError as exc:
        raise _http(exc)
