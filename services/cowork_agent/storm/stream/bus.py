"""The bus (design ch 09 §4): in-process, one per engine. It hears every
commit and every live-only event from the engine (``ports.Listener``), runs
the running turns' presenters, and hands frames to its subscribers.

* Each subscriber is a bounded queue (``QUEUE_LIMIT``) with a filter.
* A slow subscriber loses live-only frames first: they are dropped, and the
  subscriber is sent a fresh snapshot once it has caught up (live text
  arrives as appends, so a dropped one is never left as a gap). If a frame
  with a seq can't be queued, its stream ends with ``reset {from_seq}`` and
  the client resumes from the store.
* Live updates are coalesced: one flush ``COALESCE_S`` after the first
  change; a file's growing size goes out at most every
  ``SIZE_INTERVAL_S``. Before a turn's stored event is handed on, that
  turn's pending updates are, so a client never sees an update after the
  block finished.
* No polling: a subscriber's writer awaits its queue, with a
  ``heartbeat`` after ``HEARTBEAT_S`` of silence.

Everything here runs on the event loop: the engine calls the listener there,
and the timers are the engine's clock's.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, AsyncIterator, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine.ports import Timer
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream.frames import Filter, Frame, control
from services.cowork_agent.storm.stream.presenter import TURN_EVENTS
from services.cowork_agent.storm.stream.source import Source
from services.cowork_agent.storm.stream.turns import Running, Turns

log = logging.getLogger(__name__)

#: A subscriber's queue (ch 09 §4).
QUEUE_LIMIT = 1000
#: Live updates are coalesced over this long (ch 09 §3: 50–100 ms).
COALESCE_S = 0.075
#: A file's size is sent at most this often (ch 09 §3: 4 a second).
SIZE_INTERVAL_S = 0.25
#: Silence after which a subscriber gets a heartbeat (ch 09 §3).
HEARTBEAT_S = 15.0


class Subscriber:
    """One stream's queue. Created idle: it queues nothing until
    ``go_live``, so a reconnect can replay from the store first."""

    def __init__(self, bus: "Bus", filter: Filter) -> None:
        self.filter = filter
        self.dropped = 0                    # live-only frames dropped (slow client)
        self.sent_seq = 0                   # the last stored event this client has
        self._bus = bus
        self._queue: deque[Frame] = deque()
        self._live = False
        self._after = 0                     # live from here: what came before was replayed
        self._stale = False                 # dropped live frames: owes the client a fresh snapshot
        self._reset = False
        self._closed = False
        self._beat = False
        self._signal = asyncio.Event()

    def go_live(self, after: int) -> None:
        """From now on, queue every matching frame after ``after``; the first
        thing sent is the snapshot of the running turns."""
        self._live, self._after, self.sent_seq = True, after, after
        if self.filter.wants_blocks:
            self._queue.append(self._bus.snapshot(self.filter))
        self._signal.set()

    def close(self) -> None:
        self._closed = True
        self._signal.set()

    @property
    def backlog(self) -> int:
        """Frames queued and not yet sent (never more than ``QUEUE_LIMIT``)."""
        return len(self._queue)

    def offer(self, frame: Frame) -> None:
        if not self._live or self._reset or self._closed:
            return
        if frame.seq is not None and frame.seq <= self._after:
            return
        if not self.filter.matches(frame):
            return
        if frame.seq is None and self._stale:
            self.dropped += 1               # the coming snapshot carries it
            return
        if len(self._queue) >= QUEUE_LIMIT:
            stored = deque(f for f in self._queue if f.seq is not None)
            if len(stored) < len(self._queue):
                self.dropped += len(self._queue) - len(stored)
                self._queue, self._stale = stored, True
            if frame.seq is None:
                self.dropped += 1
                self._stale = True
                return
            if len(self._queue) >= QUEUE_LIMIT:
                self._queue.clear()
                self._reset = True
                self._signal.set()
                return
        self._queue.append(frame)
        self._signal.set()

    async def frames(self) -> AsyncIterator[Frame]:
        while True:
            if self._reset:
                yield control("reset", {"from_seq": self.sent_seq, "reason": "slow"})
                return
            if self._queue:
                frame = self._queue.popleft()
                if frame.seq is not None:
                    self.sent_seq = frame.seq
                self._beat = False
                yield frame
                continue
            if self._stale:
                self._stale = False
                if self.filter.wants_blocks:
                    yield self._bus.snapshot(self.filter)
                continue
            if self._closed:
                return
            if self._beat:
                self._beat = False
                yield control("heartbeat", {})
                continue
            self._signal.clear()
            timer = self._bus.clock.call_later(HEARTBEAT_S, self._heartbeat)
            try:
                await self._signal.wait()
            finally:
                timer.cancel()

    def _heartbeat(self) -> None:
        self._beat = True
        self._signal.set()


class Bus:
    """The engine's ``ports.Listener``."""

    def __init__(self, source: Source) -> None:
        self.source = source
        self.clock = source.clock
        self.turns = Turns(source.streams)
        self.processed = 0                  # the last seq handed on
        self.closed = False
        self._subscribers: set[Subscriber] = set()
        self._advanced = asyncio.Event()
        self._flush_timer: Timer | None = None
        self._flush_at: float | None = None

    async def start(self) -> None:
        """Listen, then learn where the store is, so a reconnect knows when
        the bus has caught up with what it replays."""
        self.source.listen(self)
        head = await self.source.store.read(lambda conn: conn.execute(
            "SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])
        self.processed = max(self.processed, head)

    def close(self) -> None:
        self.closed = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        for subscriber in list(self._subscribers):
            subscriber.close()
        self._subscribers.clear()
        self._advanced.set()

    def subscribe(self, filter: Filter) -> Subscriber:
        subscriber = Subscriber(self, filter)
        if self.closed:
            subscriber.close()
        else:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    async def caught_up(self, seq: int) -> None:
        """Wait until every event up to ``seq`` has been handed on."""
        while self.processed < seq and not self.closed:
            advanced = self._advanced
            await advanced.wait()

    # ── ports.Listener ───────────────────────────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        if self.closed:
            return
        for event in events:
            try:
                running = self.turns.running.get(event.turn_id) if event.turn_id else None
                if running is not None and event.type in TURN_EVENTS:
                    self._flush_turn(running, force=True)
                self._publish(self.turns.apply(event))
                if running is not None and running.presenter.dirty:
                    self._schedule(COALESCE_S)
            except Exception:  # noqa: BLE001 - one event it can't present must not stall every stream
                log.exception("storm stream: presenting %s (seq %s) failed", event.type, event.seq)
            self.processed = max(self.processed, event.seq)
        self._advanced.set()
        self._advanced = asyncio.Event()

    def ephemeral(self, event: c.Event) -> None:
        running = self.turns.running.get(event.turn_id) if event.turn_id and not self.closed else None
        if running is None or event.session != running.session_id:
            return
        try:
            self._publish(running.frames(running.presenter.ephemeral(event)))
        except Exception:  # noqa: BLE001 - a live update it can't present is skipped, never fatal
            log.exception("storm stream: presenting %s for turn %s failed", type(event).__name__, event.turn_id)
        if running.presenter.dirty:
            self._schedule(COALESCE_S)

    # ── snapshots ────────────────────────────────────────────────────────

    def snapshot(self, filter: Filter) -> Frame:
        """The running turns this filter sees, as their clients were last
        sent them (ch 09 §5)."""
        turns: list[dict[str, Any]] = []
        for running in self.turns.running.values():
            if filter.wants_ids(running.chat_id, running.session_id):
                turns.append({"turn_id": running.presenter.turn_id, "session_id": running.session_id,
                              "chat_id": running.chat_id, "thread_ts": running.thread_ts,
                              "seq": running.presenter.last_seq, "held": self.source.held(running.session_id),
                              "blocks": running.presenter.open_blocks()})
        return control("snapshot", {"seq": self.processed, "turns": turns})

    # ── coalescing ───────────────────────────────────────────────────────

    def _schedule(self, delay: float) -> None:
        due = self.clock.monotonic() + delay
        if self._flush_at is not None and self._flush_at <= due:
            return
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_at, self._flush_timer = due, self.clock.call_later(delay, self._flush)

    def _flush(self) -> None:
        self._flush_timer = self._flush_at = None
        if self.closed:
            return
        due = [d for running in list(self.turns.running.values())
               if (d := self._flush_turn(running)) is not None]
        if due:
            self._schedule(max(0.0, min(due) - self.clock.monotonic()))

    def _flush_turn(self, running: Running, *, force: bool = False) -> float | None:
        outs, due = running.presenter.flush(self.clock.monotonic(), interval=SIZE_INTERVAL_S, force=force)
        self._publish(running.frames(outs))
        return due

    def _publish(self, frames: Sequence[Frame]) -> None:
        for subscriber in list(self._subscribers):
            for frame in frames:
                subscriber.offer(frame)
