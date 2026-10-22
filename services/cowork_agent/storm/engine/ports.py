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
* ``Clock``: wall time, a monotonic clock and timers, so the idle release and
  the pools' reset times run in tests without real sleeps.
* ``Writes``: what a collaborator's code gets inside an engine transaction:
  the transaction, its time, and ``wake`` to queue an input for any session
  (the engine admits it after the commit).
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
from services.cowork_agent.storm.store import KEPT, StoredEvent, Tx
from services.timestamps import TS_FORMAT

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

    def execute(self, sql: str, params: Mapping[str, Any] | tuple = ()):
        return self.tx.execute(sql, params)

    def append(self, type: str, data: Mapping[str, Any] | None = None, **ids: Any) -> int:
        return self.tx.append(type, data, **ids)

    def wake(self, session_id: str, wake: Wake) -> bool:
        """Queue ``wake`` for ``session_id`` unless a wake with its id exists
        (idempotent, ch 07 §4.1). Returns whether it was inserted."""
        meta = {"origin": wake.origin, "context": wake.context, "delivery": wake.delivery,
                **({KEPT: wake.kept} if wake.kept is not None and wake.kept != wake.text else {})}
        inserted = self.tx.execute(
            "INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, json(?), 'queued', ?) ON CONFLICT (id) DO NOTHING",
            (wake.id, session_id, wake.priority, wake.reason, wake.text, json.dumps(meta), self.now)).rowcount == 1
        if inserted:
            self.woken.add(session_id)
        return inserted


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
