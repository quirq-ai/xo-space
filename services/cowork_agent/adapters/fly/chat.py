"""Existing chat endpoints, with abort-aware adapter-owned SSE and no core edits."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi.responses import JSONResponse

from services.cowork_agent.adapters.fly.adapter import FlyAdapter
from services.cowork_agent.engine.chat_state import active_streams
from services.cowork_agent.registry.settings import load_agent_config
from services.errors import ServiceError


async def handle_prompt(*, body, text, session_id, agent_id, is_new_session):
    try:
        if session_id and any(row.get("backend") == "fly" and row.get("session_id") == session_id for row in active_streams.values()):
            raise ServiceError("session_busy", "Finish or cancel this session's current turn first.", 409)
        adapter = FlyAdapter(load_agent_config("fly"))
        prepared = await adapter.prepare(text, our_session_id=session_id, agent_id=agent_id,
                                         model=body.get("model"), is_new_session=is_new_session)
        sid = prepared["session"]["id"]
        # Recheck after asynchronous artifact validation; concurrent prompts must not reserve the same session.
        if any(row.get("backend") == "fly" and row.get("session_id") == sid for row in active_streams.values()):
            raise ServiceError("session_busy", "This session already has a pending turn.", 409)
        stream_id = str(uuid.uuid4())
        active_streams[stream_id] = {"backend": "fly", "session_id": sid, "prepared": prepared,
                                     "adapter": adapter, "is_new_session": is_new_session, "started": False,
                                     "expires_at": time.monotonic() + 300}
        # Expire a prompt never attached to an SSE consumer; it must not block the session forever.
        def expire():
            current = active_streams.get(stream_id)
            if current and not current["started"]:
                active_streams.pop(stream_id, None)
        asyncio.get_running_loop().call_later(300, expire)
        return {"stream_id": stream_id, "session_id": sid}
    except ServiceError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})


def get_sse_generator(stream_id: str, stream_info: dict):
    async def generate():
        sid = stream_info["session_id"]
        number = 0
        def sse(name: str, payload: dict):
            nonlocal number
            number += 1
            return f"id: {number}\nevent: {name}\ndata: {json.dumps(payload)}\n\n"
        if stream_info["started"]:
            # A duplicate EventSource attachment must never execute the turn twice.
            done = stream_info.get("finished")
            if done:
                await done.wait()
            yield sse("done", {"session_id": sid})
            return
        stream_info["started"] = True
        stream_info["finished"] = asyncio.Event()
        cancel_event = asyncio.Event()
        queue = asyncio.Queue()
        sentinel = object()

        async def produce():
            try:
                async for event in stream_info["adapter"].stream(stream_info["prepared"]["question"], prepared=stream_info["prepared"], cancel_event=cancel_event):
                    await queue.put(event)
            except asyncio.CancelledError:
                pass  # adapter persisted cancellation while unwinding
            except Exception as exc:
                await queue.put({"type": "error", "error": str(exc) if isinstance(exc, ServiceError) else "The local fly runtime failed."})
            finally:
                await queue.put(sentinel)

        producer = asyncio.create_task(produce())
        last_heartbeat = time.monotonic()
        try:
            if stream_info["is_new_session"]:
                yield sse("session-created", {"session_id": sid})
            while True:
                if active_streams.get(stream_id) is not stream_info:
                    cancel_event.set()
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    yield sse("done", {"session_id": sid, "finish_reason": "cancelled"})
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_heartbeat >= 20:
                        yield "event: heartbeat\ndata: {}\n\n"
                        last_heartbeat = time.monotonic()
                    continue
                if event is sentinel or event.get("done"):
                    yield sse("done", {"session_id": sid, "finish_reason": "stop"})
                    return
                if event.get("type") == "token":
                    yield sse("text-delta", {"text": event["token"]})
                elif event.get("type") == "model-loading":
                    yield sse("model-loading", {"label": event["label"]})
                elif event.get("type") == "error":
                    yield sse("agent-error", {"error_message": event["error"]})
        finally:
            cancel_event.set()
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            if active_streams.get(stream_id) is stream_info:
                active_streams.pop(stream_id, None)
            stream_info["finished"].set()
    return generate()
