"""Workspace and project chats, their messages, threads, hops and memory
(design ch 17 §2.2; ch 11):

  GET   /api/chat/chats
  GET   /api/chat/chats/{chat}                     the chat with its router override and pin
  PATCH /api/chat/chats/{chat}                     {routing?, agent?, model?, effort?}
  GET   /api/chat/chats/{chat}/messages            ?thread=&after_seq=&before_seq=&limit=  (by seq)
  POST  /api/chat/chats/{chat}/messages            {text, thread_ts?, client_id}
  GET   /api/chat/chats/{chat}/threads
  GET   /api/chat/chats/{chat}/threads/{ts}
  PATCH /api/chat/chats/{chat}/threads/{ts}        {resolved?, paused?, title?, label?, intelligence?}
  GET   /api/chat/chats/{chat}/hops                ?thread=&after_seq=&before_seq=&limit=  (by seq)
  GET   /api/chat/chats/{chat}/memory
  GET   /api/chat/chats/{chat}/memory/{path}

A message posted to a thread goes to its agent; one posted on a timeline
goes through the router (``409 router_off`` when it is off). A retried POST
with the same ``client_id`` stores one message. Resolve and reopen are
idempotent and never start or stop a turn. ``409 agent_locked``,
``400 unsupported_model`` / ``unsupported_effort``; 503 when the engine runs
elsewhere.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Query
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import chats
from services.cowork_agent.storm.chats import delivery, views
from services.errors import ServiceError

router = APIRouter()

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Routing(ForbidExtra):
    mode: Literal["off", "agent", "decision"]
    decision_model: Optional[str] = None


class ChatUpdate(ForbidExtra):
    routing: Optional[Routing] = None          # null clears the chat's override
    agent: Optional[str] = Field(None, max_length=64)
    model: Optional[str] = Field(None, max_length=200)
    effort: Optional[Effort] = None


class NewMessage(ForbidExtra):
    text: str = Field(min_length=1, max_length=100_000)
    thread_ts: Optional[str] = Field(None, max_length=64)
    client_id: str = Field(min_length=1, max_length=200)


class ThreadIntelligence(ForbidExtra):
    agent: Optional[str] = Field(None, max_length=64)
    model: Optional[str] = Field(None, max_length=200)
    effort: Optional[Effort] = None


class ThreadUpdate(ForbidExtra):
    resolved: Optional[bool] = None
    paused: Optional[bool] = None
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    label: Optional[str] = Field(None, max_length=32)       # null clears it
    intelligence: Optional[ThreadIntelligence] = None


def _service() -> chats.Chats:
    return chats.current()


@router.get("/api/chat/chats")
async def list_chats() -> dict:
    try:
        return {"chats": await views.list_chats(_service())}
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}")
async def get_chat(chat_id: str) -> dict:
    try:
        return await views.get_chat(_service(), chat_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.patch("/api/chat/chats/{chat_id}")
async def update_chat(chat_id: str, body: ChatUpdate) -> dict:
    sent: dict[str, Any] = {k: getattr(body, k) for k in body.model_fields_set}
    if "routing" in sent and body.routing is not None:
        sent["routing"] = body.routing.model_dump()
    try:
        return await views.update_chat(_service(), chat_id, **sent)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/messages")
async def list_messages(chat_id: str, thread: Optional[str] = None, after_seq: int = Query(0, ge=0),
                        before_seq: Optional[int] = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        found = await views.messages(_service(), chat_id, thread=thread, after_seq=after_seq, before_seq=before_seq,
                                     limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
    return {"messages": found, **_cursors(found, limit, before_seq)}


@router.post("/api/chat/chats/{chat_id}/messages")
async def post_message(chat_id: str, body: NewMessage) -> dict:
    try:
        return await delivery.post(_service(), chat_id, text=body.text, thread_ts=body.thread_ts,
                                   client_id=body.client_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/threads")
async def list_threads(chat_id: str) -> dict:
    try:
        return {"threads": await views.list_threads(_service(), chat_id)}
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/threads/{ts}")
async def get_thread(chat_id: str, ts: str) -> dict:
    try:
        return await views.get_thread(_service(), chat_id, ts)
    except ServiceError as exc:
        raise storm_error(exc)


@router.patch("/api/chat/chats/{chat_id}/threads/{ts}")
async def update_thread(chat_id: str, ts: str, body: ThreadUpdate) -> dict:
    sent: dict[str, Any] = {k: getattr(body, k) for k in body.model_fields_set}
    if body.intelligence is not None:
        sent["intelligence"] = body.intelligence.model_dump(exclude_none=True)
    try:
        return await views.update_thread(_service(), chat_id, ts, **sent)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/hops")
async def list_hops(chat_id: str, thread: Optional[str] = None, after_seq: int = Query(0, ge=0),
                    before_seq: Optional[int] = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        found = await views.hops(_service(), chat_id, thread=thread, after_seq=after_seq, before_seq=before_seq,
                                 limit=limit)
    except ServiceError as exc:
        raise storm_error(exc)
    return {"hops": found, **_cursors(found, limit, before_seq)}


def _cursors(found: list[dict], limit: int, before_seq: int | None) -> dict:
    """Where the next page starts: forward from the last row, and, for a
    page read backwards (``before_seq``), further back from the first."""
    full = len(found) == limit
    out = {"next_after_seq": found[-1]["seq"] if full else None}
    if before_seq is not None:
        out["next_before_seq"] = found[0]["seq"] if full else None
    return out


@router.get("/api/chat/chats/{chat_id}/memory")
async def list_memory(chat_id: str) -> dict:
    try:
        return {"files": await views.memory_files(_service(), chat_id)}
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/memory/{path}")
async def read_memory(chat_id: str, path: str) -> dict:
    try:
        return await views.memory_file(_service(), chat_id, path)
    except ServiceError as exc:
        raise storm_error(exc)
