"""``/api/sessions``, ``/api/messages`` and ``/api/chat``: the sessions
module's routes, mounted by the registry behind the ``sessions`` api gate.
The three prefixes are the manifest's aliases, so every client that
learned the old surface keeps working; ``routers/cowork_agent/sessions.py``
and ``chat.py`` still resolve to this module.

  GET    /api/sessions?limit=50&offset=0        the listing, newest first
  GET    /api/sessions/search?q=&limit=&offset=  {session, snippet} rows by title
  GET    /api/sessions/{session_id}              one session (404 "Session not found")
  POST   /api/sessions                           {id, title: "New Chat"}
  PATCH  /api/sessions/{session_id}              {directory}: set the working directory
  DELETE /api/sessions/{session_id}              {ok}
  GET    /api/sessions/{session_id}/transcript   one text bubble per turn (?tools=true adds tool lines)
  GET    /api/sessions/{session_id}/todos        {todos: []}
  GET    /api/sessions/{session_id}/files        {files: []}
  GET    /api/messages/{session_id}              {total, offset, messages}: the full record
  POST   /api/chat/prompt                        {text, session_id?, agent_name?, ...} -> {stream_id, session_id}
  GET    /api/chat/stream/{stream_id}            server-sent events: text-delta, session-created, done, ...
  POST   /api/chat/abort                         {stream_id} -> {ok}
  POST   /api/chat/respond                       {ok}

Route order matters: ``/api/sessions/search`` registers before
``/api/sessions/{session_id}`` so the literal path is not swallowed by the
path-parameter route. The session routes are thin over
``modules.sessions.service``; a typed failure reaches the wire through the
app's service error handler as the bare ``{"detail": message}`` these
routes have always answered.

The chat routes are backend-agnostic. An agent may contribute a ``chat``
capability (``services/cowork_agent/adapters/<name>/chat.py``):
  - ``handle_prompt(...)``: fully owns POST /api/chat/prompt (e.g. openclaw's
    direct prefetch path). When absent, the prompt goes through AgentDispatcher.
  - ``get_sse_generator(stream_id, stream_info)``: the SSE generator for a
    stream that handler registered.
  - ``resolve_agent_id(body)``: resolve an agent_id/profile from the prompt
    body (e.g. hermes ``model: "hermes/<profile>"``).
All agents without a custom handler stream through AgentDispatcher via
_dispatcher_sse. No backend is named in this file. Whichever path served
the stream, the session's index row is stamped ``purpose: "chat"`` when the
stream ends (``service.record_purpose``; a row that already has a purpose
keeps it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine.chat_state import active_streams
from services.errors import ServiceError
from services.xo_manifest import resolve_agent_name

from . import service

log = logging.getLogger(__name__)

# Tracks recently-started streams so a fast reconnect (e.g. navigation-caused
# double-mount) gets a graceful done event rather than "Stream not found".
# Maps stream_id -> {session_id, started_at}
_recently_started: dict[str, dict] = {}
_RECENTLY_STARTED_TTL = 600  # seconds; must outlast SSE_HEARTBEAT_TIMEOUT (45s) + full reconnect backoff

router = APIRouter()


# ── Sessions: read side (GET); order matters so /search beats /{session_id} ──


@router.get("/api/sessions")
def list_sessions(limit: int = 50, offset: int = 0):
    """The sessions the active backend knows, newest first, one page."""
    return service.list_sessions(limit, offset)


@router.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 20, offset: int = 0):
    """Sessions whose title contains q, as {session, snippet} rows."""
    return service.search(q, limit, offset)


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    """One session by id."""
    return service.get(session_id)


@router.get("/api/messages/{session_id}")
def get_messages(session_id: str, limit: int = 50, offset: int = -1):
    """The full record of a session: every message with its parts."""
    return service.messages(session_id, limit, offset)


# ── Sessions: write side ─────────────────────────────────────────────────────


@router.post("/api/sessions")
async def create_session(request: Request):
    """A fresh session id; the session itself starts with the first prompt."""
    return service.create()


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    """Set the session's working directory ({directory})."""
    return service.update(session_id, await request.body())


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """Acknowledge a delete; the agent's store keeps the record."""
    return service.delete(session_id)


# ── Sessions: per-session read-only extras ───────────────────────────────────


@router.get("/api/sessions/{session_id}/transcript")
def session_transcript_view(session_id: str, tools: bool = False):
    """{title, messages:[{id, role, content}]}: one text bubble per turn.
    The full record (tool calls, reasoning, usage) stays on /api/messages."""
    return service.transcript(session_id, include_tools=tools)


@router.get("/api/sessions/{session_id}/todos")
def session_todos(session_id: str):
    """The session's todos (always empty here; the projects module holds them)."""
    return service.todos(session_id)


@router.get("/api/sessions/{session_id}/files")
def session_files(session_id: str):
    """The session's files (always empty here)."""
    return service.files(session_id)


# ── Chat ─────────────────────────────────────────────────────────────────────


async def _resolve_user_id(request: Request) -> str | None:
    """Resolve the Composio user_id for an incoming chat request.

    Composio runs on the user's own key; ``body.user_id`` is never trusted. Returns None
    when no Composio API key is configured, in which case the turn runs without Composio
    tools, the only safe answer.
    """
    from services.cowork_agent.connectors.composio.identity import resolve_user

    user_id = await resolve_user(request)
    if not user_id:
        log.debug(
            "chat: no Composio API key configured; the turn runs without Composio tools."
        )
    return user_id


def _resolve_backend_for_session(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None (caller uses AGENT_NAME default)."""
    return service.backend_of(session_id)


def _remember_chat_purpose(session_id: str | None) -> None:
    """The row this chat created (or continued) is a chat: stamp it once
    the stream is over. A purpose another starter recorded is kept."""
    if session_id:
        service.record_purpose(session_id, "chat", overwrite=False)


def _adapter_sse_generator(stream_info: dict, stream_id: str):
    """Return an adapter-owned SSE generator for this stream, or None.

    A stream registered by an agent's ``chat.handle_prompt`` is tagged with
    ``backend``; that adapter's ``get_sse_generator`` produces the stream. The
    router stays backend-agnostic.
    """
    backend = stream_info.get("backend")
    if not backend:
        return None
    chat_mod = try_load_capability("chat", agent=backend)
    factory = getattr(chat_mod, "get_sse_generator", None) if chat_mod else None
    if factory is None:
        return None
    return factory(stream_id, stream_info)


def _session_id_from_sse(chunk: str) -> str | None:
    """Best-effort extract a ``session_id`` from an SSE chunk's data payload.

    Adapter-owned streams resolve their session id mid-stream (e.g. an
    openclaw prefetch only learns it from the gateway, then emits it in a
    ``session-created`` event). This keeps ``_recently_started``'s session_id
    current so a post-``done`` reconnect can replay session-created + done.
    Backend-agnostic: parses only the generic SSE wire shape.
    """
    for line in chunk.split("\n"):
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except (ValueError, TypeError):
                return None
            if isinstance(payload, dict):
                sid = payload.get("session_id")
                if isinstance(sid, str) and sid:
                    return sid
            return None
    return None


_KEEPALIVE_INTERVAL = 20  # seconds of silence before emitting an SSE keepalive comment

_SENTINEL = object()  # marks end-of-stream in the keepalive queue


async def _dispatcher_sse(stream_info: dict, _session_id_out: list | None = None):
    """
    SSE generator for non-OpenClaw agents using AgentDispatcher.

    Emits named SSE events matching SSE_EVENTS in the frontend:
      event: text-delta    data: {"text":"..."}
      event: session-created  data: {"session_id":"..."}
      event: agent-error   data: {"error_message":"..."}
      event: done          data: {"session_id":"..."}

    During long tool-call runs, emits `event: heartbeat` named events every
    20 s so the frontend's heartbeat timer is reset and idle connections stay open.

    Keepalives use an asyncio.Queue producer-task pattern, NOT
    asyncio.wait_for(__anext__), because cancelling __anext__ on an
    async generator corrupts its internal state and causes it to stop
    early (the bug that made text disappear after the first timeout).

    Adapters are responsible for all session tracking (session_key, native IDs,
    session persistence). This function is adapter-agnostic.
    """
    from services.cowork_agent.engine.dispatcher import AgentDispatcher

    agent_name = stream_info["agent_name"]
    question = stream_info["question"]
    our_session_id = stream_info.get("our_session_id") or stream_info.get("session_id")
    agent_type = stream_info.get("agent_type")
    agent_id = stream_info.get("agent_id")
    model = stream_info.get("model")
    is_new_session = stream_info.get("is_new_session", False)
    user_id = stream_info.get("user_id")

    if is_new_session and our_session_id:
        event_id = 1
        yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': our_session_id})}\n\n"
        event_id += 1
    else:
        event_id = 1

    dispatcher = AgentDispatcher(agent_name)
    final_native_session_id = None
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for event in dispatcher.stream(
                question,
                None,
                agent_type=agent_type,
                our_session_id=our_session_id,
                agent_id=agent_id,
                model=model,
                is_new_session=is_new_session,
                user_id=user_id,
            ):
                await queue.put(event)
        except Exception as exc:
            await queue.put({"type": "error", "error": str(exc)})
        finally:
            await queue.put(_SENTINEL)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue

            if item is _SENTINEL:
                break

            event = item
            if event.get("done"):
                final_native_session_id = event.get("native_session_id")
                break
            elif event.get("type") == "token":
                yield f"id: {event_id}\nevent: text-delta\ndata: {json.dumps({'text': event.get('token', '')})}\n\n"
                event_id += 1
            elif event.get("type") == "model-loading":
                # Hermes runs tool calls (RAG recall, web search, etc.) before
                # streaming text and emits ``hermes.tool.progress`` events with a
                # human-readable label. Forward them as model-loading so the UI
                # shows live activity during the 10-30 s tool-call phase
                # instead of looking frozen.
                yield f"id: {event_id}\nevent: model-loading\ndata: {json.dumps({'label': event.get('label', '')})}\n\n"
                event_id += 1
            elif event.get("type") == "error":
                yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': event.get('error', 'Stream error')})}\n\n"
                event_id += 1
    finally:
        producer.cancel()

    resolved_session_id = our_session_id or final_native_session_id
    if _session_id_out is not None:
        _session_id_out.append(resolved_session_id)
    yield f"id: {event_id}\nevent: done\ndata: {json.dumps({'finish_reason': 'stop', 'session_id': resolved_session_id})}\n\n"


@router.post("/api/chat/prompt")
async def chat_prompt(request: Request):
    """Register a turn: {text, session_id?, agent_name?, agent_id?, workspace?, agent_type?, model?}
    answers {stream_id, session_id}; the stream route plays it."""
    body = await request.json()
    text = body.get("text", "").strip()
    session_id = body.get("session_id")
    agent_name = body.get("agent_name")

    if not text:
        raise ServiceError(None, "Empty message", 400)

    # For existing sessions: auto-detect backend (claude_code sessions take precedence)
    if session_id and not agent_name:
        detected = _resolve_backend_for_session(session_id)
        if detected:
            agent_name = detected

    # Agent switched mid-session (e.g., user picked a different agent from the
    # sidebar dropdown while a chat was open): the incoming session_id belongs
    # to a different backend than the one the user just selected. Treat as a
    # fresh session under the new agent.
    if session_id and agent_name:
        detected = _resolve_backend_for_session(session_id)
        if detected and detected != agent_name:
            print(f"[chat] agent switch detected (session backend={detected!r} new agent={agent_name!r}); starting fresh session")
            session_id = None

    if not agent_name:
        agent_name = resolve_agent_name()

    print(f"[chat] routing -> agent_name={agent_name!r} agent_id={body.get('agent_id')!r} session_id={session_id!r} workspace={body.get('workspace')!r}")

    is_new_session = not bool(session_id)

    # Resolve agent_id from explicit field or workspace hint (all agents, new sessions only).
    # For openclaw this becomes xo_agent_id (xo-projects subdir for the transcript tee).
    agent_id = body.get("agent_id")
    if not agent_id and is_new_session:
        workspace_hint = body.get("workspace", "")
        if workspace_hint:
            from services.cowork_agent.project_layout import xo_projects_root
            from services.cowork_agent.registry.settings import CLAUDE_COWORK_DIR
            try:
                ws_path = __import__("pathlib").Path(workspace_hint).expanduser().resolve()
                xo_root = xo_projects_root().resolve()
                cc_path = CLAUDE_COWORK_DIR.resolve()
                if str(ws_path).startswith(str(xo_root) + "/"):
                    agent_id = ws_path.relative_to(xo_root).parts[0]
                elif str(ws_path).startswith(str(cc_path) + "/"):
                    agent_id = ws_path.relative_to(cc_path).parts[0]
            except Exception:
                pass

    # Optional adapter hook: resolve an agent_id/profile from the prompt body
    # (e.g. hermes ``model: "hermes/<profile>"``). Explicit agent_id wins.
    if not agent_id:
        chat_mod = try_load_capability("chat", agent=agent_name)
        resolver = getattr(chat_mod, "resolve_agent_id", None) if chat_mod else None
        if resolver:
            agent_id = resolver(body)

    # Optional adapter hook: an agent may fully own the prompt path (e.g.
    # openclaw's direct prefetch/streaming). When present it returns the
    # response; otherwise we fall through to the shared dispatcher path.
    chat_mod = try_load_capability("chat", agent=agent_name)
    handle_prompt = getattr(chat_mod, "handle_prompt", None) if chat_mod else None
    if handle_prompt:
        return await handle_prompt(
            body=body,
            text=text,
            session_id=session_id,
            agent_id=agent_id,
            is_new_session=is_new_session,
        )

    # Default: route through AgentDispatcher.
    our_session_id = str(uuid.uuid4()) if is_new_session else session_id
    stream_id = str(uuid.uuid4())
    active_streams[stream_id] = {
        "question": text,
        "session_id": our_session_id,
        "our_session_id": our_session_id,
        "agent_name": agent_name,
        "agent_type": body.get("agent_type"),
        "agent_id": agent_id,
        "model": body.get("model"),
        "is_new_session": is_new_session,
        "user_id": await _resolve_user_id(request),
    }
    return {"stream_id": stream_id, "session_id": our_session_id}


@router.get("/api/chat/stream/{stream_id}")
async def chat_stream(stream_id: str):
    """Play a registered turn as server-sent events (text-delta, session-created, done, ...)."""
    # Purge stale recently-started records
    now = time.time()
    stale = [k for k, v in _recently_started.items() if now - v["started_at"] > _RECENTLY_STARTED_TTL]
    for k in stale:
        _recently_started.pop(k, None)

    stream_info = active_streams.get(stream_id)
    adapter_gen = _adapter_sse_generator(stream_info, stream_id) if stream_info else None
    if not stream_info:
        # Reconnect after double-mount: wait for the original stream to finish,
        # then send done so the client refetches messages from DB.
        recent = _recently_started.get(stream_id)
        if recent:
            done_event = recent.get("done_event")
            async def reconnect_done():
                if done_event and not done_event.is_set():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                sid = recent["session_id"]
                if sid:
                    yield f"id: 1\nevent: session-created\ndata: {json.dumps({'session_id': sid})}\n\n"
                yield f"id: 2\nevent: done\ndata: {json.dumps({'session_id': sid})}\n\n"
            generator = reconnect_done()
        else:
            async def not_found():
                yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Stream not found'})}\n\n"
            generator = not_found()
    elif adapter_gen is not None:
        # Adapter-owned stream (e.g. openclaw prefetch / live gateway stream).
        # Give it the same reconnect grace as the dispatcher path below: a native
        # EventSource auto-reconnects the instant the server closes the
        # connection after `done`, and that reconnect can land before the client
        # calls .close(). Without a _recently_started record it would hit the
        # not-found branch and surface a spurious "Stream not found" error even
        # though the response completed. The adapter owns active_streams cleanup,
        # so we only track completion + the resolved session_id here.
        done_event = asyncio.Event()
        _recently_started[stream_id] = {
            "session_id": stream_info.get("session_id") or stream_info.get("our_session_id"),
            "started_at": now,
            "done_event": done_event,
        }
        async def _adapter_with_signal():
            try:
                async for chunk in adapter_gen:
                    sid = _session_id_from_sse(chunk)
                    if sid:
                        _recently_started[stream_id]["session_id"] = sid
                    yield chunk
            finally:
                done_event.set()
                _remember_chat_purpose(_recently_started.get(stream_id, {}).get("session_id"))
        generator = _adapter_with_signal()
    elif stream_info.get("agent_name"):
        # Shared dispatcher path with reconnect signal.
        active_streams.pop(stream_id, None)
        done_event = asyncio.Event()
        _recently_started[stream_id] = {
            "session_id": stream_info.get("our_session_id"),
            "started_at": now,
            "done_event": done_event,
        }
        session_id_out: list = []
        async def _dispatcher_with_signal():
            try:
                async for chunk in _dispatcher_sse(stream_info, session_id_out):
                    yield chunk
            finally:
                if session_id_out:
                    _recently_started[stream_id]["session_id"] = session_id_out[0]
                done_event.set()
                _remember_chat_purpose(_recently_started.get(stream_id, {}).get("session_id"))
        generator = _dispatcher_with_signal()
    else:
        async def unknown_stream():
            yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Unknown stream type'})}\n\n"
        generator = unknown_stream()

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/chat/abort")
async def chat_abort(request: Request):
    """Forget a registered turn that has not been played ({stream_id})."""
    body = await request.json()
    stream_id = body.get("stream_id")
    if stream_id:
        active_streams.pop(stream_id, None)
    return {"ok": True}


@router.post("/api/chat/respond")
async def chat_respond(request: Request):
    """Acknowledge a permission response (no-op)."""
    return {"ok": True}
