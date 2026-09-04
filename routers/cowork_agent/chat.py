"""
Chat prompt / streaming / abort routes.

The router is backend-agnostic. An agent may contribute a ``chat`` capability
(``services/cowork_agent/adapters/<name>/chat.py``):
  - ``handle_prompt(...)`` — fully owns POST /api/chat/prompt (e.g. openclaw's
    direct prefetch path). When absent, the prompt goes through AgentDispatcher.
  - ``get_sse_generator(stream_id, stream_info)`` — the SSE generator for a
    stream that handler registered.
  - ``resolve_agent_id(body)`` — resolve an agent_id/profile from the prompt
    body (e.g. hermes ``model: "hermes/<profile>"``).
All agents without a custom handler stream through AgentDispatcher via
_dispatcher_sse. No backend is named in this file.
"""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine.chat_state import active_streams
from services.xo_manifest import resolve_agent_name

log = logging.getLogger(__name__)

# Tracks recently-started streams so a fast reconnect (e.g. navigation-caused
# double-mount) gets a graceful done event rather than "Stream not found".
# Maps stream_id -> {session_id, started_at}
_recently_started: dict[str, dict] = {}
_RECENTLY_STARTED_TTL = 600  # seconds — must outlast SSE_HEARTBEAT_TIMEOUT (45s) + full reconnect backoff

router = APIRouter()


async def _resolve_user_id(request: Request) -> str | None:
    """Resolve the Composio user_id for an incoming chat request.

    The bearer is a gate, not a selector: this backend has exactly one Composio
    principal, fetched from xo-swarm-api. ``body.user_id`` is still never trusted.
    Chat/session storage is unchanged.

    Returns None when the request carries no valid session, or when the principal
    cannot be resolved. Chat still runs — the agent simply gets no Composio MCP
    server for that turn, which is the only safe answer.
    """
    from services.cowork_agent.connectors.composio.identity import resolve_user_from_bearer

    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        log.warning(
            "chat: no valid session bearer on this prompt — the turn runs "
            "without Composio tools. Mint a session id via "
            "GET /xo-auth/session/self and send it as 'X-XO-Session: <id>'."
        )
    return user_id


def _resolve_backend_for_session(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None (caller uses AGENT_NAME default)."""
    from services.cowork_agent.engine.sessions_io import find_session_backend
    return find_session_backend(session_id)


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

    Keepalives use an asyncio.Queue producer-task pattern — NOT
    asyncio.wait_for(__anext__) — because cancelling __anext__ on an
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
                # streaming text — emits ``hermes.tool.progress`` events with a
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
    body = await request.json()
    text = body.get("text", "").strip()
    session_id = body.get("session_id")
    agent_name = body.get("agent_name")

    if not text:
        return JSONResponse(status_code=400, content={"detail": "Empty message"})

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

    print(f"[chat] routing → agent_name={agent_name!r} agent_id={body.get('agent_id')!r} session_id={session_id!r} workspace={body.get('workspace')!r}")

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
    body = await request.json()
    stream_id = body.get("stream_id")
    if stream_id:
        active_streams.pop(stream_id, None)
    return {"ok": True}


@router.post("/api/chat/respond")
async def chat_respond(request: Request):
    return {"ok": True}
