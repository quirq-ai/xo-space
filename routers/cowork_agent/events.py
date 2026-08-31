"""
Aggregated live-events SSE feed.

GET /api/events streams ``event: snapshot`` frames carrying the combined
section payload (``models``, ``data``, ``skills``, ...) so the frontend can
subscribe once instead of polling the per-source status endpoints. Those
endpoints (``/providers/status``, ``/api/connectors/*/status``, ...) remain
the detail surface; this feed is display-oriented and push-only.

Lightweight by design: one shared poll loop serves every subscriber, it only
runs while at least one client is connected, and a frame is pushed only when
the snapshot actually changed. Comment-line heartbeats keep idle connections
alive through proxies.
"""

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services.cowork_agent.events_stream import EventsBroadcaster, default_sections

router = APIRouter(tags=["events"])

POLL_INTERVAL_S = 60.0
HEARTBEAT_S = 15.0

broadcaster = EventsBroadcaster(sections=default_sections(), interval=POLL_INTERVAL_S)


def _sse_frame(snapshot: dict[str, Any]) -> str:
    return f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"


@router.get("/api/events")
async def events() -> StreamingResponse:
    """Stream aggregated snapshots over SSE; pushes only on change."""

    async def stream() -> AsyncIterator[str]:
        queue = broadcaster.subscribe()
        try:
            last = await broadcaster.current_snapshot()
            yield _sse_frame(last)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item == last:
                    continue  # dedupe the initial-snapshot race
                last = item
                yield _sse_frame(item)
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
