"""Storm's worktrees (ch 13 §4, §6; ch 17):

  GET    /api/chat/worktrees?chat=&limit=&cursor=
         XO's thread worktrees that are still there, each with what removing
         it would lose (``would_lose``: null when nothing; each kind's whole
         count, its first 50 paths) and its branch's ``git`` state
         (ahead/behind the default branch, merged and when, upstream, last
         commit, pull request, and ``tree``; ``worktrees/state.py``); the
         threads whose worktree was removed (``removed``), with the git state
         last read; and the worktrees CLIs made on their own that were
         reported, listed, never managed (on the first page). Read from the
         store only: XO keeps each thread's state as it changes
         (``worktrees/tracking.py``) and sends each change as a ``thread.git``
         event; ``checked_at`` says when a tree was read. Newest thread
         first; every thread unless ``limit`` (1 to 500) is given, then that
         many and the ``next_cursor`` of the next, older page
  POST   /api/chat/worktrees/{chat}/{ts}/check
         read one thread's tree and branch now, stored and sent as a
         ``thread.git`` event; answered as its stored listing row.
         ``504 check_timeout`` with the last stored ``row`` past 10 s
         (the check goes on); 404 ``thread_not_found`` / ``no_worktree``
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
async def list_worktrees(chat: Optional[str] = None,
                         limit: Optional[int] = Query(None, ge=1, le=service.PAGE_MAX),
                         cursor: Optional[str] = Query(None, min_length=1, max_length=512)) -> dict:
    try:
        return await service.current().listing(chat=chat, limit=limit, cursor=cursor)
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/worktrees/{chat_id}/{thread_ts}/check")
async def check_worktree(chat_id: str, thread_ts: str) -> dict:
    try:
        return await service.current().check(chat_id, thread_ts)
    except ServiceError as exc:
        raise storm_error(exc)


@router.delete("/api/chat/worktrees/{chat_id}/{thread_ts}")
async def remove_worktree(chat_id: str, thread_ts: str,
                          confirm: Optional[str] = Query(None, min_length=1, max_length=64)) -> dict:
    try:
        return await service.current().remove(chat_id, thread_ts, confirm=confirm)
    except ServiceError as exc:
        raise storm_error(exc)
