"""Storm's review queue (ch 17 §2.5; ch 15 §5–§7):

  GET  /api/chat/review?state=&kind=&chat=&thread=&session=&after_seq=&before_seq=&limit=
       items of every kind, paged by seq; open items in the queue's order
       (destructive first, then blocking, then by age), none hidden
  GET  /api/chat/review/{id}
  POST /api/chat/review/{id}/decision
       {decision, scope?, option?, answer?, note?, edited_input?, rule_sentence?, idempotency_key}

The decision route is the only writer of an owner decision: ``decided_by``
is the server's, so a body naming it is a 422 like any unknown key. A retry
with the same ``idempotency_key`` returns the same result. ``409
already_decided``, ``410 expired``, ``400 scope_not_allowed`` (and
``rule_sentence_mismatch`` with the server's sentence). It has no owner
credential yet (§0.27; risk D4): the browser write guard covers it like every
write. 503 ``engine_elsewhere`` when another server runs the engine.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Query
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm.review import queue
from services.cowork_agent.storm.review.decisions import Request
from services.errors import ServiceError

router = APIRouter()

Kind = Literal["permission", "question", "approval", "action", "create_project", "create_job", "usage", "integrity",
               "suggestion"]


class DecisionBody(ForbidExtra):
    decision: Literal["allow", "deny", "answer", "approve", "send_back", "done", "cant", "continue", "wait"]
    scope: Optional[Literal["once", "thread", "rule"]] = None
    option: Optional[str] = Field(None, max_length=200)
    answer: Optional[Union[str, dict[str, Union[str, list[str]]]]] = None
    note: Optional[str] = Field(None, max_length=4000)
    edited_input: Optional[dict[str, Any]] = None
    rule_sentence: Optional[str] = Field(None, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.get("/api/chat/review")
async def list_review(state: Optional[Literal["open", "decided", "expired", "withdrawn"]] = None,
                      kind: Optional[Kind] = None, chat: Optional[str] = None, thread: Optional[str] = None,
                      session: Optional[str] = None, after_seq: int = Query(0, ge=0),
                      before_seq: Optional[int] = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        items = await queue.current().list_items(state=state, kind=kind, chat=chat, thread=thread, session=session,
                                                 after_seq=after_seq, before_seq=before_seq, limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
    return {"items": items, "next_after_seq": items[-1]["seq"] if len(items) == limit else None}


@router.get("/api/chat/review/{item_id}")
async def get_review(item_id: str) -> dict:
    try:
        return await queue.current().get_item(item_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/review/{item_id}/decision")
async def decide(item_id: str, body: DecisionBody) -> dict:
    try:
        return await queue.current().decide(item_id, Request(**body.model_dump()))
    except ServiceError as exc:
        raise storm_error(exc)
