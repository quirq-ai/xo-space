"""Storm's worktrees (ch 13 §4, §6; ch 17):

  GET    /api/chat/worktrees?chat=
         XO's thread worktrees that are still there, each with what removing
         it would lose (``would_lose``: null when nothing); and the worktrees
         CLIs made on their own that were reported, listed, never managed
  DELETE /api/chat/worktrees/{chat}/{ts}?confirm=
         remove a thread's worktree. Nothing would be lost: removed. Something
         would: ``409 would_lose_work {what, confirm}``; the same request
         with ``?confirm=<confirm>`` removes it with ``--force`` and records
         what was lost (a tree that changed since gets a new ``confirm``).
         ``409 session_busy`` while the thread's session isn't cold;
         ``409 not_xo_worktree`` when its marker and record disagree.

503 ``engine_elsewhere`` when another server runs the engine.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm.worktrees import service
from services.errors import ServiceError

router = APIRouter()


@router.get("/api/chat/worktrees")
async def list_worktrees(chat: Optional[str] = None) -> dict:
    try:
        return await service.current().listing(chat=chat)
    except ServiceError as exc:
        raise storm_error(exc)


@router.delete("/api/chat/worktrees/{chat_id}/{thread_ts}")
async def remove_worktree(chat_id: str, thread_ts: str,
                          confirm: Optional[str] = Query(None, min_length=1, max_length=64)) -> dict:
    try:
        return await service.current().remove(chat_id, thread_ts, confirm=confirm)
    except ServiceError as exc:
        raise storm_error(exc)
