"""The engine's seams: the small interfaces other storm packages implement,
so they plug in without editing the engine.

* ``Listener`` (the stream, PR 8): every committed transaction's events, in
  seq order, and every ephemeral runtime event (deltas, bytes, progress) as it
  arrives, with the epoch of the process it came from (a listener drops one
  older than the session's latest ``session`` event's epoch). The two reach a
  listener in the order the engine handled them, per session. Register at
  boot (``boot(listeners=…)``) to hear everything from the first admission. The presenter builds blocks and the running turn's snapshot from
  exactly these; the engine keeps no presentation state.
* ``RunsListener``: a listener that also wants each session's sub-agent
  aggregate (``running``, ``waiting``, ``tokens``), at most once a second
  per session while its runs change (ch 14 §2.1). Never stored.
* ``ReviewDesk`` (the review queue, PR 9): open an item for an ask, park it
  (memory, a dead process, a restart), withdraw it. Each call runs inside the
  engine's own transaction (through ``Writes``), so an item opens in the same
  commit as the session's ``waiting_on_person``. Decisions come back through
  ``Engine.deliver_decision``, whose ``record`` is compare-and-set on the item
  being open (``AlreadyDecided`` otherwise).
* ``Preparer`` (the worktrees, PR 13): readies what a session's process
  needs on disk (a thread's worktree) right before the process starts, on
  every start. Register at boot (``boot(preparers=…)``).
* ``TurnCheck`` (the integrity check, PR 17): looks at a session's folder
  before every turn it starts. Report only: the turn starts whatever it
  found. Register at boot (``boot(checks=…)``).
* ``TurnNote``: a short note the agent reads at the top of a new turn's
  input, about what changed in its folder since its last turn without it
  (the person's edits in Files). Register at boot (``boot(notes=…)``).
* ``Clock``: wall time, a monotonic clock and timers, so the idle release and
  the pools' reset times run in tests without real sleeps.
* ``Writes``: what a collaborator's code gets inside an engine transaction:
  the transaction, its time, and ``wake`` to queue an input for any session
  (the engine admits it after the commit). ``wake`` is the one gate every
  wake writer goes through: it follows a retired session to its live
  successor and keeps the founder's Stop (w2-w3 design §3.1).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from services.cowork_agent.adapters.contract import Event, SessionSpec
from services.cowork_agent.storm.engine.model import Ask, CallRef, ReviewUnavailable, Wake
from services.cowork_agent.storm.records import intents
from services.cowork_agent.storm.records.intents import Intent
from services.cowork_agent.storm.store import KEPT, StoredEvent, Tx, scrub
from services.timestamps import TS_FORMAT, parse_ts

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine.engine import Engine
    from services.cowork_agent.storm.engine.session import Meta


class Timer(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def time(self) -> float: ...                       # wall clock, epoch seconds
    def monotonic(self) -> float: ...
    def call_later(self, delay: float, callback: Callable[[], None]) -> Timer: ...


class LoopClock:
    """The real clock: the running event loop's timers."""

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def call_later(self, delay: float, callback: Callable[[], None]) -> Timer:
        return asyncio.get_running_loop().call_later(max(0.0, delay), callback)


def iso(seconds: float) -> str:
    """A wall time as the store writes it: UTC, ending in Z."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(TS_FORMAT)


class Listener(Protocol):
    """Implemented by the stream (PR 8). Both calls run on the event loop and
    must not block: a slow consumer is the listener's to handle (bounded
    queues, ch 09 §4)."""

    def committed(self, events: Sequence[StoredEvent]) -> None: ...
    def ephemeral(self, event: Event, epoch: int) -> None: ...   # epoch: the process it came from


class Preparer(Protocol):
    """Implemented by the worktrees (PR 13). Called each time a cold session
    gets a process, after its epoch moved and before the runtime opens it,
    with the spec it opens with. It may write through ``engine.write``. A
    ``RuntimeFailure`` with ``retryable=False`` leaves the session cold and
    held for the person's next message, saying why; anything else it raises
    is retried with the engine's backoff."""

    async def prepare(self, engine: "Engine", meta: "Meta", spec: SessionSpec) -> None: ...


class TurnCheck(Protocol):
    """Implemented by the integrity check (PR 17). Called before every turn
    a session starts: on a cold start right after the preparers, before the
    runtime opens the process (a CLI loads its project config as it starts),
    and before each later turn the warm process takes. Report only (ch 16
    §11.2): the turn starts when it returns, and anything it raises is
    logged, never a reason not to start. It bounds its own wait."""

    async def before_turn(self, engine: "Engine", meta: "Meta", spec: SessionSpec) -> None: ...


class TurnNote(Protocol):
    """``note`` is called as a new turn's input is put together (never for
    input added to a running turn), with when the session's previous turn
    started (``since``, to the second; the session's creation for its
    first), and returns a note for the agent, already escaped for the
    envelope, or None. It is bounded by the engine (``NOTE_WAIT_S``); a note
    that fails or is late is logged and left out, never a reason not to
    start. ``delivered`` is called once that input reached the agent, so a
    provider that keeps its own bound moves it only then: input that was
    never sent is noted again with the next turn."""

    async def note(self, engine: "Engine", meta: "Meta", spec: SessionSpec, since: str) -> str | None: ...

    def delivered(self, meta: "Meta") -> None: ...

@dataclass(frozen=True)
class RunsAggregate:
    """A session's sub-agents working now: how many run, how many wait on
    the person, and the tokens they have used so far."""

    session_id: str
    chat_id: str | None
    thread_ts: str | None
    running: int
    waiting: int
    tokens: int


@runtime_checkable
class RunsListener(Protocol):
    """A ``Listener`` that also takes the per-session run aggregate. Called
    on the event loop; must not block."""

    def runs(self, aggregate: RunsAggregate) -> None: ...


class Writes:
    """One engine transaction, as seen by a collaborator's code."""

    def __init__(self, tx: Tx, now: str) -> None:
        self.tx = tx
        self.now = now
        self.woken: set[str] = set()
        #: The founder's Stop that refused the last wake, or None. A caller
        #: whose message must not be lost silently (a tool's hand-off, a
        #: delivery) looks right after its ``wake`` and refuses its whole
        #: change with it.
        self.refused: Intent | None = None

    def execute(self, sql: str, params: Mapping[str, Any] | tuple = ()):
        return self.tx.execute(sql, params)

    def append(self, type: str, data: Mapping[str, Any] | None = None, **ids: Any) -> int:
        return self.tx.append(type, data, **ids)

    def wake(self, session_id: str, wake: Wake) -> bool:
        """Queue ``wake`` for ``session_id`` unless a wake with its id exists
        (idempotent, ch 07 §4.1). Returns whether it was inserted.

        Every wake writer goes through here (w2-w3 design §3.1), so one rule
        holds for each, read from the store in this transaction:

        * a retired session's wake goes to the live session that succeeded
          it (``sessions.predecessor``), with a ``wake.moved`` event: an
          answer to a replaced coordinator reaches the live one. A line of
          successors that ends retired refuses it (``no_live_session``);
        * in a scope the founder stopped it is refused, with a
          ``wake.refused`` event, and ``refused`` says why until the next
          wake. The founder's own message is the exception: it is held,
          never refused;
        * in a paused scope it is inserted, and admission holds it."""
        target = self._target(session_id, wake.id, person=_founders_own(wake.id, wake.origin))
        if target is None:
            return False
        meta = {"origin": wake.origin, "context": wake.context, "delivery": wake.delivery,
                **({KEPT: wake.kept} if wake.kept is not None and wake.kept != wake.text else {}),
                **({"confirmed_runs": wake.confirmed_runs} if wake.confirmed_runs is not None else {}),
                **({"images": list(wake.images)} if wake.images else {})}
        inserted = self.tx.execute(
            "INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, json(?), 'queued', ?) ON CONFLICT (id) DO NOTHING",
            (wake.id, target, wake.priority, wake.reason, wake.text, json.dumps(meta), self.now)).rowcount == 1
        if inserted:
            self.woken.add(target)
            if target != session_id:
                self.append("wake.moved", {"id": wake.id, "from": session_id, "to": target}, session_id=target)
        return inserted

    def move_queued(self, old: str) -> list[str]:
        """Move the queued wakes of the retired session ``old`` to its live
        successor, by ``wake``'s rules, each with a ``wake.moved`` event:
        after what is queued there already, in the order they were queued,
        their meta, redeliveries and age kept. One the founder's Stop
        refuses is dropped. With no live successor nothing moves, and
        nothing is said. Returns the ids moved."""
        moved: list[str] = []
        if live_successor(self.tx.conn, old) in (None, old):
            return moved
        for wake_id, priority, reason, envelope, meta, redelivery, created_at in self.execute(
                "SELECT id, priority, reason, envelope, meta, redelivery, created_at FROM wakes "
                "WHERE session_id = ? AND state = 'queued' ORDER BY rowid", (old,)).fetchall():
            target = self._target(old, wake_id, person=_founders_own(wake_id, json.loads(meta).get("origin")))
            if target is None:                  # a successor is there: the founder's Stop refused it
                self.execute("UPDATE wakes SET state = 'dropped', done_at = ? WHERE id = ?", (self.now, wake_id))
                scrub(self.tx, [wake_id])
                continue
            self.execute("DELETE FROM wakes WHERE id = ?", (wake_id,))
            self.execute("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, redelivery, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                         (wake_id, target, priority, reason, envelope, meta, redelivery, created_at))
            self.append("wake.moved", {"id": wake_id, "from": old, "to": target}, session_id=target)
            self.woken.add(target)
            moved.append(wake_id)
        return moved

    def _target(self, session_id: str, wake_id: str, *, person: bool) -> str | None:
        """The session a wake for ``session_id`` goes to, or None when it is
        refused (said with a ``wake.refused`` event)."""
        self.refused = None
        target = live_successor(self.tx.conn, session_id)
        if target is None:
            self.append("wake.refused", {"id": wake_id, "reason": "no_live_session", "scope": None},
                        session_id=session_id)
            return None
        hold = intents.session_run(self.tx.conn, target, parse_ts(self.now).timestamp())
        if hold is not None and hold.value == "stopped" and not person:
            self.refused = hold
            self.append("wake.refused", {"id": wake_id, "reason": "stopped",
                                         "scope": {"scope": hold.scope, "scope_id": hold.scope_id}},
                        session_id=target)
            return None
        return target


def live_successor(conn, session_id: str) -> str | None:
    """``session_id`` while it isn't retired, else the live session that
    succeeded it (``sessions.predecessor``, followed to its end); None when
    the line ends in a retired session. An id the store doesn't have is
    returned as it is: the insert then fails as it always has."""
    seen: set[str] = set()
    current: str | None = session_id
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT retired_at IS NULL FROM sessions WHERE id = ?", (current,)).fetchone()
        if row is None or row[0]:
            return current
        after = conn.execute("SELECT id FROM sessions WHERE predecessor = ? ORDER BY seq DESC LIMIT 1",
                             (current,)).fetchone()
        current = after[0] if after else None
    return None


def _founders_own(wake_id: str, origin: str | None) -> bool:
    """The founder's own message (``msg:<id>``, from the person): held in a
    stopped scope, never refused."""
    return wake_id.startswith("msg:") and origin == "person"


class ReviewDesk(Protocol):
    """Implemented by the review queue (PR 9). Every method runs on the
    store's thread, inside the engine's transaction: plain SQL through ``w``,
    no awaiting. Raising rolls the whole change back (the engine then denies
    the call: fail closed)."""

    def open(self, w: Writes, ask: Ask) -> str: ...                       # → the item id
    def park(self, w: Writes, item_id: str, reason: str) -> None: ...     # the call is gone; the item stays open
    def withdraw(self, w: Writes, item_id: str, reason: str) -> None: ...  # the CLI took the question back

    def supersede(self, w: Writes, session_id: str, *, by: str, call: CallRef | None = None) -> list[str]:
        """Withdraw the session's *open, parked* items with ``review.withdrawn
        {id, reason: "superseded", by}``, and return their ids. With
        ``call`` (``by`` is the item just opened for it, right after ``open``
        in the same transaction): only the items whose call has the same
        ``kind`` and ``input_sha256``. Without (``by`` is
        ``"person_message"``): all of them, because the person wrote to the
        session and moved on. One action, one item: a superseded item can't
        be decided, so no grant for an action that may have run is minted."""
        ...


class NoReviewDesk:
    """Until a review queue is plugged in nothing can reach the person, so an
    ask is denied (fail closed)."""

    def open(self, w: Writes, ask: Ask) -> str:
        raise ReviewUnavailable()

    def park(self, w: Writes, item_id: str, reason: str) -> None:
        raise ReviewUnavailable()

    def withdraw(self, w: Writes, item_id: str, reason: str) -> None:
        raise ReviewUnavailable()

    def supersede(self, w: Writes, session_id: str, *, by: str, call: CallRef | None = None) -> list[str]:
        raise ReviewUnavailable()
