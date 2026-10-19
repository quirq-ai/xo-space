"""Storm's events and streams (design ch 09): the bus, the turn presenter
and the SSE writers. Routes: ``routers/cowork_agent/storm/stream.py``.

The server makes the bus (``listener``), passes it to the engine's
``boot(listeners=…)`` so it hears everything from the first admission, binds
it to the engine boot returns (``attach``) and closes it before the engine
stops (``detach``). A stream (``stream``), as ch 09 §4 describes:

1. subscribes first: from then on its queue keeps every stored frame its
   filter wants (bounded; overflowing it is a ``reset``);
2. with a resume point (``Last-Event-ID``, or ``after_seq``), replays the
   stored frames after it, page by page, once; a point the store can't
   serve (older than retention, or one it never reached) is a ``reset``;
3. goes live: the queue drops the frames the replay covered (by place, so
   there is no gap and no duplicate), then sends the running turns'
   snapshot, then live frames, with a ``heartbeat`` in every 15 s of
   silence.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import EngineUnavailable
from services.cowork_agent.storm.engine.ports import Clock, LoopClock
from services.cowork_agent.storm.stream.bus import HEARTBEAT_S, QUEUE_LIMIT, Bus, Subscriber
from services.cowork_agent.storm.stream.frames import Filter, Frame, Point, control
from services.cowork_agent.storm.stream.presenter import BLOCK_TYPES, TurnPresenter, present
from services.cowork_agent.storm.stream.replay import PAGE_LIMIT, RETENTION_S, Replay, read_page, stale_point

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine

_bus: Bus | None = None


def listener(clock: Clock | None = None) -> Bus:
    """A bus for ``boot(listeners=…)``; ``clock`` must be the engine's
    (the real clock by default, as the engine's is)."""
    return Bus(clock or LoopClock())


async def attach(bus: Bus, engine: "Engine") -> Bus:
    """Bind ``bus`` (which ``boot`` registered) to ``engine`` and serve
    streams from it (an earlier bus is closed first)."""
    global _bus
    detach()
    await bus.bind(engine)
    _bus = bus
    return bus


def detach() -> None:
    """End every stream (the server is stopping)."""
    global _bus
    bus, _bus = _bus, None
    if bus is not None:
        bus.close()


def end_streams_on_exit_signal(loop: asyncio.AbstractEventLoop) -> Callable[[], None]:
    """When the server is told to stop, end every stream at once; returns
    how to take that back (the server's shutdown does).

    uvicorn waits for every open response to finish before it runs the
    app's shutdown, and a stream never finishes on its own: left open, it
    would hold the server up until it is killed, and the engine would never
    release its CLI processes. So the exit signals first close the bus
    (clients reconnect with ``Last-Event-ID``), then go on to the handler
    in place (uvicorn's). Once ``loop`` is closed they only go on."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None                 # signals reach the main thread only
    installed: dict[int, tuple[Any, Any]] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue                        # nothing serving to hand on to

        def handler(signum: int, frame: Any, previous: Any = previous) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(detach)
            previous(signum, frame)
        signal.signal(sig, handler)
        installed[sig] = (handler, previous)

    def restore() -> None:
        for sig, (handler, previous) in installed.items():
            if signal.getsignal(sig) is handler:      # nobody has replaced it since
                signal.signal(sig, previous)
    return restore


def current() -> Bus:
    """The bus of the running engine; raises ``EngineUnavailable`` (503)
    like the engine's routes when this server runs no engine."""
    engine = storm_engine.current()
    if _bus is None or _bus.source.engine is not engine:
        raise EngineUnavailable("stream_not_running", "Storm's event stream isn't running in this server (it "
                                                      "starts with the engine); retry shortly.")
    return _bus


async def stream(bus: Bus, filter: Filter, after: Point | None) -> AsyncIterator[Frame]:
    """One client's frames, after ``after`` (None: from now), until it goes
    away, falls too far behind (``reset``) or the server stops."""
    subscriber = bus.subscribe(filter)
    try:
        upto = Point(bus.processed)
        if after is not None:
            stale = await stale_point(bus.source.store, after.seq, now=bus.clock.time())
            if stale is not None:
                reason, head = stale
                yield control("reset", {"from_seq": Point(head).id, "reason": reason})
                return
            upto = after
            async for item in _catch_up(bus, filter, after):
                if isinstance(item, Point):
                    upto = item
                else:
                    yield item
            # The snapshot must show the state after what was replayed.
            await bus.caught_up(upto.seq)
        held = await bus.held(filter) if filter.wants_blocks else {}
        if bus.closed:
            return
        subscriber.go_live(upto, held)
        async for frame in subscriber.frames():
            yield frame
    finally:
        bus.unsubscribe(subscriber)


async def _catch_up(bus: Bus, filter: Filter, after: Point) -> AsyncIterator[Frame | Point]:
    """The stored frames after ``after``, then the place they reach (the
    last event read). Its replay state goes when it ends."""
    replay, limit = Replay(bus.source.store, filter), PAGE_LIMIT
    read_from, upto = (after.seq if after.part is None else after.seq - 1), after
    while True:
        page = await read_page(bus.source.store, filter, after=read_from, limit=limit)
        for frame in await replay.frames(page, after=after):
            yield frame
        if page:                            # its last seq is never before ``after``
            read_from = page[-1].seq
            upto = Point(read_from)
        if len(page) < limit:
            break
    yield upto


async def events_page(bus: Bus, filter: Filter, *, after: int, before: int | None, limit: int) -> dict[str, Any]:
    """``GET /api/chat/events``: the state log as the stream sends it, paged
    by seq. With ``before`` and no ``after``, the page is the newest events
    before it (paging back); otherwise the oldest after ``after``."""
    newest = before is not None and after == 0
    page = await read_page(bus.source.store, filter, after=after, before=before, limit=limit, newest=newest)
    frames = await Replay(bus.source.store, filter).frames(page)
    full = len(page) == limit
    return {"events": [f.wire() for f in frames],
            "next_after_seq": page[-1].seq if full and not newest else None,
            "next_before_seq": page[0].seq if full and newest else None}


async def require_session(bus: Bus, session_id: str) -> None:
    await bus.source.require_session(session_id)


__all__ = [
    "BLOCK_TYPES", "EngineUnavailable", "HEARTBEAT_S", "QUEUE_LIMIT", "RETENTION_S", "Bus", "Filter", "Frame",
    "Point", "Subscriber", "TurnPresenter", "attach", "current", "detach", "end_streams_on_exit_signal",
    "events_page", "listener", "present", "require_session", "stream",
]
