"""The bus (design ch 09 §4): in-process, one per engine. It hears every
commit and every live-only event from the engine (``ports.Listener``), runs
the running turns' presenters, and hands frames to its subscribers.

* Each subscriber is a bounded queue (``QUEUE_LIMIT``) with a filter. It
  keeps every stored frame it wants from the moment it subscribes, so a
  reconnect replays from the store while new commits wait in its queue;
  going live drops what the replay already sent, by place.
* A slow subscriber loses live-only frames first: they are dropped, and the
  subscriber is sent a fresh snapshot once it has caught up (live text
  arrives as appends, so a dropped one is never left as a gap). If a stored
  frame can't be queued, its stream ends with ``reset {from_seq}``: the place
  of the last frame it got, from where the client resumes from the store.
* Live updates are coalesced: one flush ``COALESCE_S`` after the first
  change; a file's growing size goes out at most every
  ``SIZE_INTERVAL_S``. Before a turn's stored event is handed on, that
  turn's pending updates are, so a client never sees an update after the
  block finished.
* No polling: a subscriber's writer awaits its queue, with a
  ``heartbeat`` after ``HEARTBEAT_S`` of silence.
* A session's sub-agent aggregate (``runs {session, running, waiting,
  tokens}``, at most once a second, ch 14 §2.1) is a live-only frame.

Everything here runs on the event loop: the engine calls the listener there,
and the timers are the engine's clock's.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, AsyncIterator, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine.ports import Clock, RunsAggregate, Timer
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream.frames import Filter, Frame, Point, control
from services.cowork_agent.storm.stream.presenter import TURN_EVENTS, Streams
from services.cowork_agent.storm.stream.source import Source
from services.cowork_agent.storm.stream.turns import Running, Turns
from services.errors import ServiceError

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
    """One stream's queue. Until ``go_live`` it keeps only stored frames
    (the snapshot will carry what is live)."""

    def __init__(self, bus: "Bus", filter: Filter) -> None:
        self.filter = filter
        self.sent = Point(0)                # the place of the last frame this client has
        self._bus = bus
        self._queue: deque[Frame] = deque()
        self._live = False
        self._stale = False                 # dropped live frames: owes the client a fresh snapshot
        self._reset = False
        self._closed = False
        self._beat = False
        self._signal = asyncio.Event()

    def go_live(self, upto: Point, held: Mapping[str, list[str]]) -> None:
        """The client has everything up to ``upto``: drop what it has, keep
        the stored frames after it, then the snapshot of the running turns
        (their state after those frames; ``held``: their sessions' held
        review items), then live frames."""
        self._queue = deque(f for f in self._queue if not upto.covers(f))
        self.sent = upto
        if self.filter.wants_blocks:
            self._queue.append(self._bus.snapshot(self.filter, held))
        self._live = True
        self._signal.set()

    def close(self) -> None:
        self._closed = True
        self._signal.set()

    @property
    def backlog(self) -> int:
        """Frames queued and not yet sent (never more than ``QUEUE_LIMIT``)."""
        return len(self._queue)

    def offer(self, frame: Frame) -> None:
        if self._reset or self._closed or not self.filter.matches(frame):
            return
        if frame.seq is None and (self._stale or not self._live):
            return                          # the coming snapshot carries it
        if len(self._queue) >= QUEUE_LIMIT:
            stored = deque(f for f in self._queue if f.seq is not None)
            if len(stored) < len(self._queue):
                self._queue, self._stale = stored, True
            if frame.seq is None:
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
                yield control("reset", {"from_seq": self.sent.id, "reason": "slow"})
                return
            if self._queue:
                frame = self._queue.popleft()
                if frame.seq is not None:
                    self.sent = Point.after(frame)
                self._beat = False
                yield frame
                continue
            if self._stale:
                # Live frames stay dropped until the snapshot is built: it
                # carries what they said.
                held = await self._bus.held(self.filter) if self.filter.wants_blocks else {}
                self._stale = False
                if self.filter.wants_blocks:
                    yield self._bus.snapshot(self.filter, held)
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
    """The engine's ``ports.Listener``: passed to ``boot(listeners=…)`` so it
    hears everything from the first admission, then bound to the engine boot
    returns. ``clock`` is the engine's."""

    def __init__(self, clock: Clock) -> None:
        self.source = Source()
        self.clock = clock
        self.turns = Turns()
        self.processed = 0                  # the last seq handed on
        self.closed = False
        self._subscribers: set[Subscriber] = set()
        self._advanced = asyncio.Event()
        self._flush_timer: Timer | None = None
        self._flush_at: float | None = None
        self._tasks: set[asyncio.Task] = set()

    async def bind(self, engine: Any) -> None:
        """Bind to the engine boot returned, and learn where the store is
        (recovery wrote before the first admission), so a reconnect knows when
        the bus has caught up with what it replays."""
        self.source.bind(engine)
        self.processed = max(self.processed, await self.source.store.head())

    def close(self) -> None:
        self.closed = True
        self.source.unlisten(self)
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        for task in self._tasks:
            task.cancel()
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

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

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
            running = self.turns.running.get(event.turn_id) if event.turn_id else None
            if running is not None and event.type in TURN_EVENTS:
                self._flush_turn(running)
            self._publish(self.turns.apply(event))          # never raises: it logs a row it can't read
            if event.type == "turn.started" and (started := self.turns.running.get(event.turn_id)) is not None:
                self._learn_streams(started)
            if running is not None and running.presenter.dirty:
                self._schedule(COALESCE_S)
            self.processed = max(self.processed, event.seq)
        self._advanced.set()
        self._advanced = asyncio.Event()

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        running = self.turns.running.get(event.turn_id) if event.turn_id and not self.closed else None
        if running is None or event.session != running.session_id:
            return
        if running.epoch is not None and epoch < running.epoch:
            return                          # from a stale process the engine hasn't caught yet
        try:
            self._publish(running.frames(running.presenter.ephemeral(event)))
        except Exception:  # noqa: BLE001 - a live update it can't present is skipped, never fatal
            log.exception("storm stream: presenting %s for turn %s failed", type(event).__name__, event.turn_id)
        if running.presenter.dirty:
            self._schedule(COALESCE_S)

    def runs(self, aggregate: RunsAggregate) -> None:
        """``ports.RunsListener``: the session's sub-agents working now."""
        if self.closed:
            return
        self._publish([Frame("runs", {"session": aggregate.session_id, "running": aggregate.running,
                                      "waiting": aggregate.waiting, "tokens": aggregate.tokens},
                             chat_id=aggregate.chat_id, session_id=aggregate.session_id,
                             thread_ts=aggregate.thread_ts)])

    # ── snapshots ────────────────────────────────────────────────────────

    async def held(self, filter: Filter) -> dict[str, list[str]]:
        """The review items each running turn's session this filter sees is
        held on, for a snapshot."""
        sessions = {r.session_id for r in self.turns.running.values() if r.session_id
                    and filter.wants_ids(r.chat_id, r.session_id)}
        held: dict[str, list[str]] = {}
        for session in sorted(sessions):
            try:
                held[session] = await self.source.held_items(session)
            except ServiceError as exc:     # gone meanwhile: the snapshot shows nothing held
                log.info("storm stream: no held items for %s: %s", session, exc.message)
        return held

    def snapshot(self, filter: Filter, held: Mapping[str, list[str]]) -> Frame:
        """The running turns this filter sees, as their clients were last
        sent them (ch 09 §5)."""
        turns: list[dict[str, Any]] = []
        for running in self.turns.running.values():
            if filter.wants_ids(running.chat_id, running.session_id):
                turns.append({"turn_id": running.presenter.turn_id, "session_id": running.session_id,
                              "chat_id": running.chat_id, "thread_ts": running.thread_ts,
                              "seq": running.presenter.last_seq, "held": held.get(running.session_id, []),
                              "blocks": running.presenter.open_blocks()})
        return control("snapshot", {"seq": self.processed, "turns": turns})

    def _learn_streams(self, running: Running) -> None:
        """What the turn's runtime streams live, from its feature list; until
        it is known the live view promises nothing (only live hints depend on
        it, never a block's content)."""
        async def learn() -> None:
            try:
                running.presenter.streams = Streams.from_features(await self.source.features(running.session_id))
            except ServiceError as exc:
                log.info("storm stream: no feature list for %s: %s", running.session_id, exc.message)
        if running.session_id is not None:
            task = asyncio.ensure_future(learn())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

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

    def _flush_turn(self, running: Running) -> float | None:
        try:
            outs, due = running.presenter.flush(self.clock.monotonic(), interval=SIZE_INTERVAL_S)
        except Exception:  # noqa: BLE001 - live updates it can't build are skipped, never fatal
            log.exception("storm stream: updating turn %s failed", running.presenter.turn_id)
            return None
        self._publish(running.frames(outs))
        return due

    def _publish(self, frames: Sequence[Frame]) -> None:
        for subscriber in list(self._subscribers):
            for frame in frames:
                subscriber.offer(frame)
