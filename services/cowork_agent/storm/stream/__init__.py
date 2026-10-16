"""Storm's events and streams (design ch 09): the bus, the turn presenter
and the SSE writers. Routes: ``routers/cowork_agent/storm/stream.py``.

The server attaches the bus to the engine it boots (``attach``) and closes
it before the engine stops (``detach``). A stream (``stream``):

1. subscribes before anything else;
2. with a resume point (``Last-Event-ID`` or ``after_seq``), replays the
   stored events after it, page by page, until the bus has handed on
   exactly what the store held at the last read; a point the store can't
   serve (older than retention, or one it never reached) is a ``reset``;
3. goes live from exactly that seq, with no await in between (a live frame
   at or before it is dropped, so there is no gap and no duplicate), and
   sends the running turns' snapshot, then live frames, with a
   ``heartbeat`` in every 15 s of silence.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import TYPE_CHECKING, Any, AsyncIterator

from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import EngineUnavailable
from services.cowork_agent.storm.stream.bus import HEARTBEAT_S, QUEUE_LIMIT, Bus, Subscriber
from services.cowork_agent.storm.stream.frames import Filter, Frame, control
from services.cowork_agent.storm.stream.presenter import BLOCK_TYPES, TurnPresenter, present
from services.cowork_agent.storm.stream.replay import PAGE_LIMIT, RETENTION_S, Replay, read_page, stale_point
from services.cowork_agent.storm.stream.source import Source

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine

_bus: Bus | None = None


async def attach(engine: "Engine") -> Bus:
    """Start the bus for ``engine``: it hears every commit from now on."""
    global _bus
    bus = Bus(Source(engine))
    await bus.start()
    _bus = bus
    return bus


def detach() -> None:
    """End every stream (the server is stopping)."""
    global _bus
    bus, _bus = _bus, None
    if bus is not None:
        bus.close()


def end_streams_on_exit_signal(loop: asyncio.AbstractEventLoop) -> None:
    """When the server is told to stop, end every stream at once.

    uvicorn waits for every open response to finish before it runs the
    app's shutdown, and a stream never finishes on its own: left open, it
    would hold the server up until it is killed, and the engine would never
    release its CLI processes. So the exit signals first close the bus
    (clients reconnect with ``Last-Event-ID``), then go on to the handler
    in place, uvicorn's, which restores its own when it stops serving."""
    if threading.current_thread() is not threading.main_thread():
        return                              # signals reach the main thread only
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue                        # nothing serving to hand on to

        def handler(signum: int, frame: Any, previous: Any = previous) -> None:
            loop.call_soon_threadsafe(detach)
            previous(signum, frame)
        signal.signal(sig, handler)


def current() -> Bus:
    """The bus of the running engine; raises ``EngineUnavailable`` (503)
    like the engine's routes when this server runs no engine."""
    engine = storm_engine.current()
    if _bus is None or _bus.source.engine is not engine:
        raise EngineUnavailable("stream_not_running", "Storm's event stream isn't running in this server (it "
                                                      "starts with the engine); retry shortly.")
    return _bus


async def stream(bus: Bus, filter: Filter, after: int | None) -> AsyncIterator[Frame]:
    """One client's frames, from ``after`` (None: from now) on, until it goes
    away, falls too far behind (``reset``) or the server stops."""
    subscriber = bus.subscribe(filter)
    try:
        if after is not None:
            stale = await stale_point(bus.source.store, after, now=bus.clock.time())
            if stale is not None:
                reason, head = stale
                yield control("reset", {"from_seq": head, "reason": reason})
                return
            replay, read_from = Replay(bus.source.store, filter), after
            while True:
                page = await read_page(bus.source.store, filter, after=read_from)
                for frame in await replay.frames(page.events):
                    yield frame
                if page.events:
                    read_from = page.events[-1].seq
                if len(page.events) == PAGE_LIMIT:
                    continue
                # Everything up to the store's head is sent; go live once the
                # bus has handed on exactly that much (no await from the check
                # to ``go_live``, so nothing slips between).
                await bus.caught_up(page.head)
                if bus.closed:
                    return
                if bus.processed == page.head:
                    break
        subscriber.go_live(bus.processed)
        async for frame in subscriber.frames():
            yield frame
    finally:
        bus.unsubscribe(subscriber)


async def events_page(bus: Bus, filter: Filter, *, after: int, before: int | None, limit: int) -> dict[str, Any]:
    """``GET /api/chat/events``: the state log as the stream sends it, paged
    by seq. With ``before`` and no ``after``, the page is the newest events
    before it (paging back); otherwise the oldest after ``after``."""
    newest = before is not None and after == 0
    page = await read_page(bus.source.store, filter, after=after, before=before, limit=limit, newest=newest)
    frames = await Replay(bus.source.store, filter).frames(page.events)
    full = len(page.events) == limit
    return {"events": [f.wire() for f in frames],
            "next_after_seq": page.events[-1].seq if full and not newest else None,
            "next_before_seq": page.events[0].seq if full and newest else None}


async def require_session(bus: Bus, session_id: str) -> None:
    await bus.source.require_session(session_id)


__all__ = [
    "BLOCK_TYPES", "EngineUnavailable", "HEARTBEAT_S", "QUEUE_LIMIT", "RETENTION_S", "Bus", "Filter", "Frame",
    "Subscriber", "TurnPresenter", "attach", "current", "detach", "end_streams_on_exit_signal", "events_page",
    "present", "require_session", "stream",
]
