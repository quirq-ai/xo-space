"""What a chat's threads made (v2; ``services/cowork_agent/storm/made.py``):

  GET    /api/chat/chats/{chat}/made        ?since=&before=&thread=&pinned=&limit=
         the chat's items, the most recently made or touched first: each
         {id, kind: file|image|link|branch, source, path, url, name, mime,
         bytes, thread_ts, message_id, turn_id, created_at, updated_at,
         pinned, pinned_at, exists, where, project_id, relative_path}; a
         branch carries its ``git`` (merged, PR). ``next_before`` pages back.
  PUT    /api/chat/chats/{chat}/made/{id}/pin
  DELETE /api/chat/chats/{chat}/made/{id}/pin

A file is served by ``/api/xo-projects/{project_id}/file|image|download``
with its ``relative_path``. 404 ``chat_not_found`` / ``made_not_found``;
400 ``bad_since`` / ``bad_before``; 503 when the engine runs elsewhere.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import chats, made
from services.errors import ServiceError

router = APIRouter()


@router.get("/api/chat/chats/{chat_id}/made")
async def list_made(chat_id: str, since: Optional[str] = Query(None, max_length=40),
                    before: Optional[str] = Query(None, max_length=40), thread: Optional[str] = None,
                    pinned: Optional[bool] = None,
                    limit: int = Query(made.DEFAULT_LIMIT, ge=1, le=made.MAX_LIMIT)) -> dict:
    try:
        chat = await chats.current().chat(chat_id)
        found = await made.current().listing(chat, since=since, before=before, thread=thread, pinned=pinned,
                                             limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
    return {"made": found, "next_before": found[-1]["updated_at"] if len(found) == limit else None}


@router.put("/api/chat/chats/{chat_id}/made/{made_id}/pin")
async def pin_made(chat_id: str, made_id: str) -> dict:
    return await _pin(chat_id, made_id, True)


@router.delete("/api/chat/chats/{chat_id}/made/{made_id}/pin")
async def unpin_made(chat_id: str, made_id: str) -> dict:
    return await _pin(chat_id, made_id, False)


async def _pin(chat_id: str, made_id: str, pinned: bool) -> dict:
    try:
        chat = await chats.current().chat(chat_id)
        return await made.current().pin(chat, made_id, pinned)
    except ServiceError as exc:
        raise storm_error(exc)
