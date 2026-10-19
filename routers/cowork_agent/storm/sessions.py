"""Storm's sessions (ch 17 §2.1):

  GET   /api/chat/sessions?chat=&thread=&role=&state=&after_seq=&before_seq=&limit=
        storm's sessions, paged by seq (a session's seq is its creation event's)
  GET   /api/chat/sessions/{id}
  PATCH /api/chat/sessions/{id}   body {model?, effort?, permission_mode?}

Each session carries its agent, model, mode, ``native_id`` and current turn.
A change the runtime's feature list doesn't offer is ``501 unsupported`` with
the feature key; an unknown model or effort is 400. 503 ``engine_elsewhere``
when another server runs the engine.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.errors import ServiceError

router = APIRouter()


class SessionUpdate(ForbidExtra):
    model: Optional[str] = None
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None
    permission_mode: Optional[Literal["ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass"]] = None


@router.get("/api/chat/sessions")
async def list_sessions(chat: Optional[str] = None, thread: Optional[str] = None, role: Optional[str] = None,
                        state: Optional[str] = None, after_seq: int = Query(0, ge=0),
                        before_seq: Optional[int] = Query(None, ge=1),
                        limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        sessions = await engine.current().list_sessions(chat=chat, thread=thread, role=role, state=state,
                                                        after_seq=after_seq, before_seq=before_seq, limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
    return {"sessions": sessions, "next_after_seq": sessions[-1]["seq"] if len(sessions) == limit else None}


@router.get("/api/chat/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    try:
        return await engine.current().get_session(session_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.patch("/api/chat/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate) -> dict:
    try:
        return await engine.current().update(session_id, model=body.model, effort=body.effort,
                                             permission_mode=body.permission_mode)
    except ServiceError as exc:
        raise storm_error(exc)
