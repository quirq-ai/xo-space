"""Projects chat routes: the workspace chat, project chats, threads.

  GET  /api/projects-chat/status
  GET  /api/projects-chat/chats
  GET  /api/projects-chat/chats/{chat}/messages?thread=&after=&limit=
  POST /api/projects-chat/chats/{chat}/messages     {text, thread_ts?}
  GET  /api/projects-chat/chats/{chat}/threads
  GET  /api/projects-chat/chats/{chat}/threads/{ts}
  GET  /api/projects-chat/chats/{chat}/stream       SSE: new messages and thread changes

``{chat}`` is ``workspace`` or a project's folder name. Posting stores the
owner's message and wakes whoever handles it (the chat's coordinator, or
in a thread, that thread's agent); the reply arrives later through the store,
so the stream tails the store and never holds an agent turn open. With the
router off the chat refuses new messages (409); a backend without the
``coordinator`` capability gets 501, like every capability-backed route.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from routers.cowork_agent.bff.errors import http_error
from services.cowork_agent import project_layout
from services.cowork_agent.adapters.loader import try_load_capability
from services.errors import ServiceError
from services.projects_chat import coordinators, store, turns
from services.projects_chat.router import mode
from services.xo_manifest import resolve_agent_name

router = APIRouter(prefix="/api/projects-chat")

WORKSPACE = "workspace"


class PostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    thread_ts: Optional[str] = None


def _project(chat: str) -> Optional[str]:
    return None if chat == WORKSPACE else chat


def _supported() -> bool:
    return try_load_capability("coordinator", agent=resolve_agent_name()) is not None


@router.get("/status")
def status():
    return {"enabled": mode.enabled(), "supported": _supported(), "agent": resolve_agent_name(),
            "modes": {level: mode.mode(level) for level in mode.LEVELS},
            "decision_model": mode.decision_model(), "max_turns": mode.max_turns(),
            "thread_budget_usd": mode.thread_budget_usd()}


@router.get("/chats")
def chats():
    out = [{"chat": WORKSPACE, "id": store.WORKSPACE_CHAT_ID, "kind": "workspace"}]
    root = project_layout.xo_projects_root()
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        names = []
    for name in names:
        doc = store.read_chat(name)
        threads = store.list_threads(name) if doc else []
        out.append({"chat": name, "id": doc["id"] if doc else None, "kind": "project",
                    "open_threads": sum(1 for t in threads if t.get("state") != "resolved")})
    return {"chats": out}


@router.get("/chats/{chat}/messages")
def messages(chat: str, thread: Optional[str] = None, after: Optional[str] = None, limit: int = 200):
    try:
        store.chat(_project(chat))
    except ServiceError as exc:
        raise http_error(exc) from exc
    return {"messages": store.read_messages(_project(chat), thread_ts=thread, after=after,
                                            limit=max(1, min(limit, 1000)))}


@router.post("/chats/{chat}/messages")
def post(chat: str, body: PostBody):
    if not mode.enabled():
        raise HTTPException(409, {"code": "router_off", "message": "The router is off (XO_ROUTER_MODE=off)."})
    if not _supported():
        raise HTTPException(501, {"code": "not_supported",
                                  "message": f"The {resolve_agent_name()} backend has no projects chat."})
    project = _project(chat)
    try:
        store.ensure_chat(project)
        row = store.post_message(project, author_kind="owner", author_id="owner", text=body.text,
                                 thread_ts=body.thread_ts)
        target = turns.deliver_owner_message(project, row)
    except ServiceError as exc:
        raise http_error(exc) from exc
    return {"message": row, "delivered_to": {"session_id": target["id"], "role": target["role"]}}


@router.get("/chats/{chat}/threads")
def threads(chat: str):
    return {"threads": store.list_threads(_project(chat))}


@router.get("/chats/{chat}/threads/{ts}")
def thread(chat: str, ts: str):
    project = _project(chat)
    doc = store.get_thread(project, ts)
    if doc is None:
        raise HTTPException(404, {"code": "no_such_thread", "message": "There is no such thread."})
    session = store.get_session(project, doc.get("session_id") or "") or {}
    return {"thread": doc, "card": store.get_card(project, ts), "status": store.read_status(project, ts),
            "messages": store.read_messages(project, thread_ts=ts),
            "session": {k: session.get(k) for k in ("id", "status", "turns", "epoch", "last_error")},
            "busy": turns.engine.busy(doc.get("session_id") or "")}


@router.get("/chats/{chat}/stream")
async def stream(chat: str, request: Request, after: Optional[str] = None):
    project = _project(chat)

    async def events():
        last = after
        seen_threads: dict[str, str] = {}
        coordinator = coordinators.live(project)
        while not await request.is_disconnected():
            fresh = await asyncio.to_thread(store.read_messages, project, after=last)
            for row in fresh:
                last = row["id"]
                yield f"event: message\ndata: {json.dumps(row)}\n\n"
            for t in await asyncio.to_thread(store.list_threads, project):
                stamp = f"{t.get('last_activity_at')}|{(t.get('card') or {}).get('updated_at')}|{t.get('state')}"
                if seen_threads.get(t["ts"]) != stamp:
                    seen_threads[t["ts"]] = stamp
                    yield f"event: thread\ndata: {json.dumps(t)}\n\n"
            busy = bool(coordinator and turns.engine.busy(coordinator["id"]))
            yield f"event: heartbeat\ndata: {json.dumps({'coordinator_busy': busy})}\n\n"
            await asyncio.sleep(1.0)
            coordinator = coordinator or coordinators.live(project)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
