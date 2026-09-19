"""Streams as server-sent events: ``GET /api/<module>/stream/<name>``.

A module's ``stream.py`` exposes ``STREAMS = {"events": fn}`` where ``fn``
is an async generator factory ``fn(since=None, types=None)`` yielding event
lines (dicts). :func:`mount_stream` turns each into one SSE route: ``id`` is
the line's ``ts``, ``event`` its ``type``, ``data`` the line as JSON. A
client that reconnects sends ``Last-Event-ID`` and resumes after it. The
route is gated like the module's api: off answers 404 ``module_disabled``,
and an open stream ends when the switch flips.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from services import modules as registry

logger = logging.getLogger("xo_space.streams")

HEARTBEAT_S = 15.0


def _sse(line: dict) -> str:
    ts = line.get("ts") or ""
    kind = line.get("type") or "message"
    return f"id: {ts}\nevent: {kind}\ndata: {json.dumps(line, ensure_ascii=False)}\n\n"


async def _body(module: str, kind: str, gen: AsyncIterator[dict], request: Request) -> AsyncIterator[str]:
    yield ": open\n\n"
    try:
        while True:
            try:
                line = await asyncio.wait_for(gen.__anext__(), timeout=HEARTBEAT_S)
            except asyncio.TimeoutError:
                if await request.is_disconnected() or not registry.enabled(module, kind):
                    return
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return
            if not registry.enabled(module, kind):
                return
            yield _sse(line)
    finally:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass


def mount_stream(app: FastAPI, module: Any, name: str, factory: Callable[..., AsyncIterator[dict]]) -> None:
    path = f"/api/{module.name}/stream/{name}"

    async def stream(request: Request,
                     since: Optional[str] = Query(None),
                     types: Optional[str] = Query(None)) -> StreamingResponse:
        last = request.headers.get("last-event-id") or since
        allow = [t.strip() for t in types.split(",") if t.strip()] if types else None
        gen = factory(since=last, types=allow)
        return StreamingResponse(
            _body(module.name, "stream", gen, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    stream.__name__ = f"stream_{module.name}_{name}"
    app.add_api_route(path, stream, methods=["GET"], tags=[module.name],
                      dependencies=[Depends(registry.gate(module.name, "stream"))])
