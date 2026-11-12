"""The founder's Stop, pause and resume, and paid usage (w2-w3 design §4):

  GET  /api/chat/intent              {intents: [...]}: every intent that holds now
  POST /api/chat/intent/stop         {scope, scope_id, cites?}: stop the scope's work until resumed
  POST /api/chat/intent/pause        {scope, scope_id, cites?}: hold it, keeping what is queued
  POST /api/chat/intent/resume       {scope, scope_id}: lift the stop or pause; {resumed}
  POST /api/chat/intent/paid         {scope, scope_id, value, until?, cites?}: paid usage never or ask, for
                                     the Space (scope_id '') or one pool, until a time or for good;
                                     {intent, halted}

``scope`` is ``space`` (``scope_id`` ''), ``chat`` or ``coordinator`` (a
chat id), ``thread`` (``<chat_id>/<ts>``) or ``session`` (a plain chat's
session). Stop and pause answer once every live session in the scope is
halted, with what was dropped, withdrawn and halted, and the times; paid
answers once each pool it governs is recomputed, with the sessions halted
where ``never`` meets an account past its threshold. ``400
intent_invalid`` for a scope or time the store wouldn't hold. Until the
founder has a credential of their own (W8), the changes come only from this
machine (``403 not_local``), like Space's other control routes; the browser
write guard covers them as every write. 503 when the engine runs elsewhere.
"""

from __future__ import annotations

import dataclasses
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from routers.browser_guard import is_local_mutation
from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.cowork_agent.storm.control import founder
from services.errors import ServiceError

router = APIRouter()

Scope = Literal["space", "chat", "coordinator", "thread", "session"]


class Held(ForbidExtra):
    scope: Scope
    scope_id: str = Field(max_length=300)
    cites: Optional[str] = Field(None, max_length=300)


class Lifted(ForbidExtra):
    scope: Scope
    scope_id: str = Field(max_length=300)


class PaidMode(ForbidExtra):
    scope: Literal["space", "pool"]
    scope_id: str = Field(max_length=300)
    value: Literal["never", "ask"]
    until: Optional[str] = Field(None, max_length=40)
    cites: Optional[str] = Field(None, max_length=300)


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail={
            "code": "not_local",
            "message": "Only the founder stops, pauses or resumes work or sets paid usage: from this machine's own "
                       "pages, or through a proxy on this machine."})


@router.get("/api/chat/intent")
async def list_intents() -> dict:
    try:
        return {"intents": [dataclasses.asdict(i) for i in await founder.live(engine.current())]}
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/intent/stop", dependencies=[Depends(_require_local)])
async def stop(body: Held) -> dict:
    try:
        return (await founder.stop(engine.current(), body.scope, body.scope_id, cites=body.cites)).record()
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/intent/pause", dependencies=[Depends(_require_local)])
async def pause(body: Held) -> dict:
    try:
        return (await founder.pause(engine.current(), body.scope, body.scope_id, cites=body.cites)).record()
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/intent/resume", dependencies=[Depends(_require_local)])
async def resume(body: Lifted) -> dict:
    try:
        return {"resumed": await founder.resume(engine.current(), body.scope, body.scope_id)}
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/intent/paid", dependencies=[Depends(_require_local)])
async def paid(body: PaidMode) -> dict:
    try:
        return (await founder.set_paid(engine.current(), body.scope, body.scope_id, body.value, until=body.until,
                                       cites=body.cites)).record()
    except ServiceError as exc:
        raise storm_error(exc)
