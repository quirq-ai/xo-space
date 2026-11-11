"""The space advisory: one live, versioned snapshot of the Space's facts.

One writer, many readers. Everything that changes a fact runs on the event
loop and ends in ``_publish``, which builds a new immutable ``Snapshot``
and, when it says something new, raises the version, wakes ``wait_newer``
callers and tells the ``on_change`` listeners which accounts changed. A
fact that goes stale, or a window whose reset passes, is a change too: the
sampler's tick publishes it, so a reader learns of it without polling.

Where the facts come from:

* **pushed** by the engine as they happen: each rate-limit event of a live
  turn (``record_usage``), each turn's numbers (``record_turn``), and the
  process each session runs in (``track_process``, ``forget_session``).
  These calls do no I/O and never raise: a broken advisory must never stop
  or fail a turn.
* **read** by the advisory itself, where nothing pushes:
  * the container's cgroup every ``SAMPLE_S`` seconds, and the live
    sessions' process trees every ``SESSIONS_S``;
  * each account's usage from its vendor, through every runtime that offers
    a model-free read (feature ``usage_read``): every ``READ_CALM_S``, every
    ``READ_NEAR_S`` while a window is near its limit or paid usage is in
    use, and once more just after a known reset. A runtime without the read
    leaves its usage to what its turns report, and unknown once that ages.

It gives no verdict: it never says go or wait. The engine, the agents and
the person decide what to do with the facts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import replace
from typing import Any, Callable, Mapping

from services import background
from services.advisory.facts import Reading, iso_at
from services.advisory.machine import Sample, cgroup_dir, machine, read_sample
from services.advisory.model import Account, Snapshot, account_key, frozen
from services.advisory.sessions import Measured, Root, SessionState, measure
from services.advisory.usage import UsageBook, round_reset
from services.cowork_agent.adapters.contract import RateLimit, RuntimeFailure, TokenUsage, UsageReader

log = logging.getLogger(__name__)

SAMPLE_S = 2.0
SESSIONS_S = 15.0
READ_CALM_S = 300.0
READ_NEAR_S = 60.0
#: The fewest seconds between two reads of one runtime, whoever asks.
READ_MIN_S = 60.0
#: A read just after a window's reset, once the vendor has moved on.
AFTER_RESET_S = 5.0
READ_TIMEOUT_S = 30.0
#: A window at or past this share used is near its limit (reads come faster).
NEAR_UTILIZATION = 0.8

Listener = Callable[[Snapshot, frozenset[str]], None]
Readers = Callable[[], Mapping[str, UsageReader]]


def _never_raises(method: Callable[..., None]) -> Callable[..., None]:
    """For the calls the engine makes from a turn's path: a failure here is
    logged, and the caller goes on."""
    @functools.wraps(method)
    def guarded(self: "Advisory", *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - the advisory must never fail its caller
            log.exception("advisory: %s failed; the caller goes on", method.__name__)
    return guarded


class Advisory:
    def __init__(self, *, readers: Readers | None = None, clock: Callable[[], float] = time.time,
                 sample: Callable[[float], Sample] | None = None,
                 measure_sessions: Callable[[Mapping[str, Root]], Mapping[str, Measured]] = measure) -> None:
        self._readers = readers or _no_readers
        self._clock = clock
        self._sample = sample
        self._measure = measure_sessions
        self._book = UsageBook()
        self._sessions: dict[str, SessionState] = {}
        self._machine: tuple[Sample | None, Sample | None] = (None, None)   # (latest, the one before)
        self._snapshot = Snapshot(version=0, as_of=iso_at(clock()))
        self._listeners: list[Listener] = []
        self._newer = asyncio.Event()
        self._read_keys: dict[str, str] = {}       # agent -> the account key its last read was for
        self._next_read: dict[str, float] = {}     # agent -> when to read it next
        self._last_read: dict[str, float] = {}
        self._read_failures: dict[str, str] = {}   # agent -> the failure last logged
        self._read_now = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ── what readers call ──────────────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        """The current snapshot, whole, at one version."""
        return self._snapshot

    def on_change(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(snapshot, changed_accounts)`` on the event loop after
        each new version. Returns the call that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    async def wait_newer(self, version: int, timeout_s: float) -> Snapshot:
        """The first snapshot newer than ``version``, or the current one after
        ``timeout_s``."""
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while self._snapshot.version <= version:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._newer.wait(), remaining)
            except TimeoutError:
                break
        return self._snapshot

    def request_read(self, agent: str) -> None:
        """Read ``agent``'s usage soon (someone is looking at it), unless it
        was read in the last ``READ_MIN_S``."""
        now = self._clock()
        if now - self._last_read.get(agent, float("-inf")) >= READ_MIN_S:
            self._next_read[agent] = now
            self._read_now.set()

    # ── what the engine pushes (no I/O, never raises) ─────────────────────

    @_never_raises
    def record_usage(self, pool_id: str, event: RateLimit) -> None:
        """One window's state, as a live turn's call saw it."""
        self._book.record_event(pool_id, event, self._clock())
        self._publish()

    @_never_raises
    def record_turn(self, session_id: str, pool_id: str | None, usage: TokenUsage | None) -> None:
        """A turn's numbers, at its end."""
        if usage is None:
            return
        state = self._sessions.setdefault(session_id, SessionState())
        state.account = pool_id or state.account
        state.record_turn(usage, self._clock())
        self._publish()

    @_never_raises
    def track_process(self, session_id: str, pool_id: str | None, pid: int, start_ticks: int | None = None) -> None:
        """The process ``session_id`` runs in now (its tree is measured)."""
        state = self._sessions.setdefault(session_id, SessionState())
        state.account = pool_id or state.account
        if state.root is None or state.root.pid != pid:
            state.root, state.memory, state.processes = Root(pid, start_ticks), None, None
        self._publish()

    @_never_raises
    def forget_session(self, session_id: str) -> None:
        """The session's process is gone: it isn't live any more."""
        if self._sessions.pop(session_id, None) is not None:
            self._publish()

    # ── its own loops ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start sampling and reading (on the running loop); idempotent."""
        if self._tasks:
            return
        for name, loop in (("advisory sampler", self._sample_loop), ("advisory usage reads", self._read_loop)):
            task = asyncio.get_running_loop().create_task(loop(), name=name)
            background.register(name, task)
            self._tasks.append(task)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sample_loop(self) -> None:
        folder = None if self._sample is not None else await asyncio.to_thread(cgroup_dir)
        sample = self._sample or (lambda at: read_sample(at, folder))
        sessions_at = float("-inf")
        while True:
            background.tick_started("advisory sampler")
            try:
                now = self._clock()
                roots = {sid: s.root for sid, s in self._sessions.items() if s.root is not None} \
                    if now - sessions_at >= SESSIONS_S else None
                current, measured = await asyncio.to_thread(self._read_host, sample, now, roots)
                self._machine = (current, self._machine[0])
                if measured is not None:
                    sessions_at = now
                    self._apply_sessions(measured, now)
                self._publish()
            except Exception as exc:  # noqa: BLE001 - one bad sample must not end sampling
                background.tick_failed("advisory sampler", exc)
                log.warning("advisory: a sample failed", exc_info=True)
            else:
                background.tick_succeeded("advisory sampler")
            await asyncio.sleep(SAMPLE_S)

    def _read_host(self, sample: Callable[[float], Sample], now: float,
                   roots: Mapping[str, Root] | None) -> tuple[Sample, Mapping[str, Measured] | None]:
        return sample(now), (self._measure(roots) if roots else None)

    def _apply_sessions(self, measured: Mapping[str, Measured], at: float) -> None:
        for session_id, found in measured.items():
            state = self._sessions.get(session_id)
            if state is None:
                continue                # forgotten while it was measured
            state.memory = Reading(found.pss, "measured", at) if found.pss is not None else None
            state.processes = Reading(found.processes, "measured", at)

    async def _read_loop(self) -> None:
        while True:
            self._read_now.clear()
            now = self._clock()
            try:
                readers = dict(self._readers())
            except Exception:  # noqa: BLE001 - a broken adapter must not end the loop
                log.exception("advisory: the runtimes that read usage couldn't be listed")
                readers = {}
            due = [agent for agent in readers if self._next_read.get(agent, float("-inf")) <= now]
            if due:
                await asyncio.gather(*(self._read(agent, readers[agent]) for agent in due))
            wake = min((self._next_read.get(a, now) for a in readers), default=now + READ_CALM_S)
            try:
                await asyncio.wait_for(self._read_now.wait(), max(wake - self._clock(), 1.0))
            except TimeoutError:
                pass

    async def _read(self, agent: str, reader: UsageReader) -> None:
        started = self._clock()
        self._last_read[agent] = started
        try:
            snapshot = await asyncio.wait_for(reader.read_usage(), READ_TIMEOUT_S)
        except (RuntimeFailure, TimeoutError) as exc:
            self._read_failed(agent, exc)
            self._next_read[agent] = started + READ_CALM_S
            return
        except Exception as exc:  # noqa: BLE001 - one runtime's bug must not stop the others' reads
            log.exception("advisory: reading %s's usage failed", agent)
            self._read_failed(agent, exc)
            self._next_read[agent] = started + READ_CALM_S
            return
        if self._read_failures.pop(agent, None) is not None:
            log.info("advisory: %s's usage reads again", agent)
        at = self._clock()
        if snapshot.account is None:
            self._next_read[agent] = at + READ_CALM_S       # nothing to key it by: no login
            return
        key = account_key(agent, snapshot.account)
        self._read_keys[agent] = key
        try:
            self._book.record_read(key, snapshot, at)
            self._publish()
        finally:
            self._next_read[agent] = self._read_due(self._snapshot.accounts.get(key), at)

    def _read_failed(self, agent: str, exc: BaseException) -> None:
        what = f"{exc.code}: {exc.message}" if isinstance(exc, RuntimeFailure) else type(exc).__name__
        if self._read_failures.get(agent) != what:
            self._read_failures[agent] = what
            log.info("advisory: %s's usage can't be read now (%s); what its turns report stands", agent, what)

    @staticmethod
    def _read_due(account: Account | None, now: float) -> float:
        """When to read an account next: soon while it is near a limit or on
        paid usage; just after its next reset; else at the calm pace."""
        if account is None:
            return now + READ_CALM_S
        near = account.paid.in_use.value is True or any(
            (w.utilization.known and w.utilization.value >= NEAR_UTILIZATION)
            or (w.status.known and w.status.value != "ok") for w in account.windows.values())
        due = now + (READ_NEAR_S if near else READ_CALM_S)
        resets = [r for w in account.windows.values() if (r := round_reset(w.resets_at.value)) is not None]
        upcoming = [r + AFTER_RESET_S for r in resets if r + AFTER_RESET_S > now]
        return min([due, *upcoming])

    # ── publishing ─────────────────────────────────────────────────────────

    def _publish(self) -> None:
        now = self._clock()
        old = self._snapshot
        current, previous = self._machine
        new = Snapshot(version=old.version, as_of=iso_at(now), accounts=frozen(self._book.accounts(now)),
                       machine=machine(current, previous, now),
                       sessions=frozen({sid: s.publish(now) for sid, s in self._sessions.items()}))
        if new.same_facts(old):
            return
        changed = frozenset(k for k in {*old.accounts, *new.accounts} if old.accounts.get(k) != new.accounts.get(k))
        self._snapshot = replace(new, version=old.version + 1)
        self._newer.set()
        self._newer = asyncio.Event()
        for listener in list(self._listeners):
            try:
                listener(self._snapshot, changed)
            except Exception:  # noqa: BLE001 - one listener can't stop the others hearing
                log.exception("advisory: a change listener failed")


def _no_readers() -> Mapping[str, UsageReader]:
    return {}
