"""The session engine: runs every storm session end to end (design ch 07).

It speaks only the runtime contract and never names an agent: per-runtime
differences reach it through each runtime's feature list.

Public API (what the surface packages call):

* ``create_session(spec, agent=, role=, preset=, …)`` → the session id;
* ``deliver(session_id, wake, …)`` queues an input (idempotent by the wake's
  id) and the engine admits it;
* ``stop(session_id, by=)`` stops the running turn and its sub-agents;
  ``stop_run(session_id, native_run_id, by=)`` stops one sub-agent;
* ``update(session_id, model=, effort=, permission_mode=)``;
* ``pause(session_id, reason=)`` / ``resume(session_id)``: no new turns;
* ``subscribe(listener)`` / ``unsubscribe(listener)`` for the stream
  (``ports.Listener``; ``boot(listeners=…)`` registers before admission);
  ``boot(preparers=…)`` for what a process needs on disk (``ports.Preparer``);
  ``boot(checks=…)`` for what looks at a session's folder before each turn
  (``ports.TurnCheck``);
* ``deliver_decision(…)`` / ``deliver_usage_decision(…)`` for the review queue;
* ``status()``, ``list_sessions(…)``, ``get_session(…)``, ``runtime(…)``,
  ``live_progress(…)`` for the routes.

One owner per change: a session's process events are recorded by its pump
(``session.LiveSession``); operations on a session (attach, send, stop,
update, release) hold its lock; everything else here runs on the event loop
and writes through the store, one transaction per change, the store's seq
being the order. No polling: admission runs when something changes (a wake, a
turn's end, a pool or memory change, a timer), and the memory guard's
5-second reading is the one periodic read. The daily retention prune
(``store.retention``, ch 08 §6) runs here too, so it starts and stops with
the engine and only ever prunes the store this engine holds the lock of.

Bounds on retry loops (not caps on work, §0.17): a session whose turn failed
on a retryable error or whose CLI died, or whose process could not start,
waits 1, 2, 4 … 60 s (jittered) before the next try, the delay growing until
a turn completes; a non-retryable failure to start waits for the person's
next message; a wake is handed back at most ``MAX_REDELIVERIES`` times.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import secrets
import statistics
import sys
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import redact
from services.cowork_agent.storm.engine import admission, records
from services.cowork_agent.storm.engine.admission import Candidate, Pool
from services.cowork_agent.storm.engine.catalog import Catalog
from services.cowork_agent.storm.engine.memory import READ_INTERVAL_S, MemoryGuard, peak_rss_bytes
from services.cowork_agent.storm.engine.model import (
    PRIORITY_RANK,
    ROLES,
    STATUSES,
    AlreadyDecided,
    Ask,
    CallRef,
    Decision,
    Delivery,
    SessionNotFound,
    SessionRefused,
    StopResult,
    Unsupported,
    Wake,
    WouldStopRuns,
)
from services.cowork_agent.storm.engine.places import project_of, write_index_row
from services.cowork_agent.storm.engine.ports import (
    Clock,
    Listener,
    LoopClock,
    NoReviewDesk,
    Preparer,
    ReviewDesk,
    RunsAggregate,
    RunsListener,
    Timer,
    TurnCheck,
    Writes,
    iso,
)
from services.cowork_agent.storm.engine.session import (
    GRANT_TTL_S,
    STOPS_RUNS,
    LiveSession,
    Meta,
    Outcome,
    native,
    report_rows,
)
from services.cowork_agent.storm.engine.specs import spec_from_json, spec_to_json, token_digest, wants_session_token
from services.cowork_agent.storm.runs import records as run_records
from services.cowork_agent.storm.runs.live import Progress
from services.cowork_agent.storm.store import (
    RESUME_NOTE,
    EngineLock,
    RecoveryReport,
    Store,
    StoredEvent,
    hand_back,
    last_step,
    scrub,
)
from services.cowork_agent.storm.store import retention
from services.periodic import run_forever
from services.timestamps import parse_ts
from utils.commands import reap_orphan, spawn_stream

log = logging.getLogger(__name__)

IDLE_RELEASE_S = 10 * 60
#: Retry backoff: the first delay, the longest, and the jitter around each.
RETRY_FIRST_S = 1.0
RETRY_MAX_S = 60.0
RETRY_JITTER = 0.2
#: How many recent latencies ``status()`` summarises.
LATENCY_SAMPLES = 200
MEMORY_WAIT = "waiting for memory"
#: ``supersede``'s ``by`` when the person's new message moved the session on.
PERSON_MESSAGE = "person_message"
#: How often the engine tries to settle a session whose store write failed
#: (closed and held until then, so its taken wakes go back before any new turn).
STORE_RETRY_S = 10.0
STORE_FAILED = "because the store could not record it"
#: Open parked review items, per session: the session waits for the
#: decision (ch 07 §2, §6), except for a new message from the person.
_DECISION_HOLDS = ("SELECT session_id, group_concat(id, ' '), min(expires_at) FROM review_items "
                   "WHERE state = 'open' AND parked = 1 AND session_id IS NOT NULL "
                   "AND (expires_at IS NULL OR expires_at > ?) GROUP BY session_id")
#: A person's new message (not one handed back): they are here.
_NEW_PERSON_MESSAGE = ("id LIKE 'msg:%' AND json_extract(meta, '$.origin') = 'person' "
                       "AND coalesce(json_extract(meta, '$.handed_back'), 0) = 0")
#: How long shutdown waits for every session's process to close, then for the
#: engine's other tasks (index rows, reaping) to finish.
#: Storm's whole shutdown, held calls parked first. restart.sh sends SIGKILL
#: 10 s after SIGTERM, and server.py stops telemetry and its own tasks after
#: storm, so storm keeps well inside that; what a slow close leaves running,
#: the next boot's recovery stops.
SHUTDOWN_BUDGET_S = 6.0
#: A pool's account when the runtime reports none (logged out, or no way to tell).
DEFAULT_ACCOUNT = "default"
#: The hex digits of a new id (``cse_…``, ``grant_…``).
ID_HEX = 16
#: A comms token's random bytes (ch 12 §2.2).
TOKEN_BYTES = 32
#: A session's run aggregate goes on the stream at most this often (ch 14 §2.1).
RUNS_INTERVAL_S = 1.0
#: Consecutive failed starts (a process that dies before it takes its wake,
#: or a retryable failure to open) before the session waits for the person,
#: like a start that fails for good. A bound on a retry loop, not a cap.
MAX_FAILED_STARTS = 5

RuntimeLoader = Callable[[str], "c.Runtime | None"]


def pool_id(agent: str, account: str | None) -> str:
    """A pool is one agent's account (ch 07 §5.1)."""
    return f"{agent}:{account or DEFAULT_ACCOUNT}"


def pool_agent(pool: str) -> str:
    """The agent a pool id belongs to (``pool_id``'s first part)."""
    return pool.split(":", 1)[0]


@dataclass
class _Backoff:
    not_before: float                    # monotonic; inf: until the person's next message
    delay: float
    why: str
    failed_starts: int = 0


class Engine:
    def __init__(self, store: Store, lock: EngineLock, *, runtimes: RuntimeLoader, providers: Sequence[str],
                 desk: ReviewDesk | None = None, clock: Clock | None = None, memory: MemoryGuard | None = None,
                 xo_ports: frozenset[int] = frozenset(), listeners: Iterable[Listener] = (),
                 preparers: Iterable[Preparer] = (), checks: Iterable[TurnCheck] = (),
                 spawner=spawn_stream) -> None:
        self.store = store
        self.lock = lock
        self.desk: ReviewDesk = desk or NoReviewDesk()
        self.clock: Clock = clock or LoopClock()
        self.memory = memory or MemoryGuard()
        self.xo_ports = xo_ports
        self.project_config_paths: tuple[str, ...] = ()
        self.main_checkout_config_paths: tuple[str, ...] = ()
        self._runtimes = runtimes
        self._providers = tuple(providers)
        self._spawner = spawner
        self._listeners: list[Listener] = list(listeners)
        self._preparers: tuple[Preparer, ...] = tuple(preparers)
        self._checks: tuple[TurnCheck, ...] = tuple(checks)
        self._live: dict[str, LiveSession] = {}
        self._metas: dict[str, Meta] = {}
        self._pools: dict[str, Pool] = {}
        self._deciding: set[str] = set()                     # review items whose decision is being written
        self._paused: dict[str, str] = {}                    # session → why it takes no new turns
        self._admitting: set[str] = set()
        self._starting = 0
        self._next_spawn = 0.0
        self._backoff: dict[str, _Backoff] = {}
        self._survivors: set[str] = set()
        self._queued: dict[str, str] = {}                    # cold session → why it waits
        # Items whose call is gone but the review queue couldn't park: their
        # sessions are held as if parked (fail closed) until the item is
        # decided, withdrawn or expires, or the person writes.
        self._unparked: dict[str, set[str]] = {}
        self._store_failed: set[str] = set()                 # closed on a failed write; held until settled
        self._timers: dict[str, Timer] = {}
        self._wakeup = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []
        self._wake_to_start: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self._first_token: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self._closing = False

    # ── life ─────────────────────────────────────────────────────────────

    async def start(self, report: RecoveryReport) -> None:
        """After boot recovery: load the pools and pauses, park the calls the
        restart left held (through the review queue), hold back sessions
        whose process survived recovery (a second process would fight it),
        and admit what recovery requeued."""
        self.store.on_commit(self._committed)
        self.project_config_paths = self._every_runtimes("project_config_paths")
        self.main_checkout_config_paths = self._every_runtimes("main_checkout_config_paths")
        # What storm stores is redacted of the CLIs' credentials, and so are
        # the logs of storm and of its runtimes (ch 16 §3).
        redact.watch_env(self._every_runtimes("auth_env"))
        redact.redact_logs(runtime_package(rt) for rt in self._available_runtimes())

        def load(conn) -> tuple[list, list, list]:
            return (conn.execute("SELECT id, state, resets_at, paid_continue_until FROM pools").fetchall(),
                    conn.execute("SELECT DISTINCT session_id FROM procs WHERE session_id IS NOT NULL").fetchall(),
                    conn.execute("SELECT id, paused FROM sessions WHERE paused IS NOT NULL").fetchall())
        pools, survivors, paused = await self.store.read(load)
        for pid, state, resets_at, paid_until in pools:
            self._pools[pid] = Pool(pid, state, resets_at, paid_until)
            if state in ("closed", "paid") and resets_at:
                self._reopen_at(pid, resets_at)
        self._paused = dict(paused)
        self._survivors = {r[0] for r in survivors}
        await self._park_restarted(report.calls_parked)
        if report.sessions_fenced:
            self._spawn(self._reconcile_restarted(report.sessions_fenced), "storm-runs-restart")
        for session_id in sorted(self._survivors & set(report.sessions_to_rekick)):
            log.warning("storm: not re-admitting %s: a process recovery could not stop may still run for it",
                        session_id)
            await self._say_held_back(session_id)
        self._loops = [asyncio.create_task(self._admission_loop(), name="storm-admission"),
                       asyncio.create_task(run_forever("storm memory guard", self._memory_tick,
                                                       interval_s=lambda: READ_INTERVAL_S, logger=log),
                                           name="storm-memory-guard"),
                       asyncio.create_task(run_forever("storm retention prune", self._prune,
                                                       interval_s=lambda: retention.PRUNE_INTERVAL_S,
                                                       startup_delay_s=retention.PRUNE_FIRST_S, logger=log),
                                           name="storm-retention")]
        self.kick()

    def _available_runtimes(self) -> list[c.Runtime]:
        return [rt for name in self._providers if (rt := self._runtimes(name)) is not None]

    def _every_runtimes(self, feature: str) -> tuple[str, ...]:
        """The union of a values-only feature over every runtime this
        server has: a thread's checkout is watched for every CLI's config,
        and every CLI's credentials are redacted, whichever CLI runs it
        (ch 16 §2.1, §3)."""
        return tuple(sorted({p for rt in self._available_runtimes()
                             for p in rt.features().features.get(feature, c.FeatureSupport()).values}))

    def background_loops(self) -> list[asyncio.Task]:
        return list(self._loops)

    async def shutdown(self) -> None:
        """Stop admitting, release every process (a turn it ends is handed
        back, a held call is parked), and let go of the store and the lock."""
        self._closing = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_BUDGET_S

        def left() -> float:
            return max(0.0, deadline - loop.time())

        async def within(step: str, work: Awaitable[Any]) -> None:
            try:
                await asyncio.wait_for(work, left())
            except asyncio.TimeoutError:
                log.error("storm: shutdown's %s ran past its %s s budget; the next boot's recovery settles what "
                          "is left", step, SHUTDOWN_BUDGET_S)
        for task in self._loops:
            task.cancel()
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        admissions = [t for t in self._tasks if t.get_name().startswith("storm-admit")]
        for task in admissions:
            task.cancel()
        await within("admission", asyncio.gather(*self._loops, *admissions, return_exceptions=True))
        lives = list(self._live.values())
        # Held calls first: parked, their decision comes back as a grant and a wake however the close goes.
        await within("parking", asyncio.gather(*(live.park_held() for live in lives), return_exceptions=True))
        await within("closing", asyncio.gather(*(live.close("shutdown") for live in lives), return_exceptions=True))
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=left())
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.store.close()
        self.lock.release()

    # ── the public API ───────────────────────────────────────────────────

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def features(self, session_id: str) -> c.FeatureList:
        """The feature list of the runtime the session runs on."""
        return (await self.session_runtime(session_id)).features()

    async def session_runtime(self, session_id: str) -> c.Runtime:
        """The runtime the session runs on (``SessionRefused`` no_runtime when
        this server has none for its agent)."""
        return self._runtime_or_refuse((await self.meta(session_id)).agent)

    def live_progress(self, session_id: str) -> dict[str, Progress]:
        """What each of the session's working sub-agents last reported, by
        its native id (empty when no process runs it)."""
        live = self._live.get(session_id)
        return dict(live.live_runs.progress) if live is not None else {}

    def runtime(self, agent: str) -> c.Runtime | None:
        """``agent``'s storm runtime in this server, or None when it has none."""
        return self._runtimes(agent)

    async def held_items(self, session_id: str) -> list[str]:
        """The open review items the session's process holds a call for."""
        await self.meta(session_id)
        live = self._live.get(session_id)
        return live.held_items() if live is not None else []

    def unsubscribe(self, listener: Listener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    async def create_session(self, spec: c.SessionSpec, *, agent: str, role: str, preset: str,
                             chat_id: str | None = None, thread_ts: str | None = None,
                             requires: Iterable[str] = (), allow_bypass: bool = False,
                             also: Callable[[Writes, str], None] | None = None) -> str:
        """A new cold session on ``agent`` for life (ch 10 §3.1). ``spec``
        comes ready from the caller's preset; ``requires`` lists features that
        must be enforced by the CLI or the host (ch 10 §9; ch 01 §6). ``also``
        runs in the same transaction, given the new id."""
        if role not in ROLES:
            raise SessionRefused("bad_role", f"A session's role is one of {', '.join(sorted(ROLES))}, not {role!r}.")
        runtime = self._runtimes(agent)
        if runtime is None:
            raise SessionRefused("no_runtime", f"{agent} has no storm runtime, so storm can't run its sessions.")
        if spec.permissions.mode == "bypass" and not allow_bypass:
            raise SessionRefused("spec_refused", "Only a preset that declares it may run a session in bypass mode.")
        requires = tuple(requires)
        try:
            report = _admit(runtime, spec, requires, agent)
        except c.SpecRefused as exc:
            raise SessionRefused("spec_refused", exc.message) from exc
        session_id = f"cse_{uuid.uuid4().hex[:ID_HEX]}"
        spec_json, report_json = spec_to_json(spec), json.dumps(report_rows(report))
        pool = await self._current_pool(agent)

        def create(w: Writes) -> None:
            seq = w.append("session", {"status": "cold", "reason": "created", "agent": agent, "role": role,
                                       "preset": preset}, session_id=session_id, chat_id=chat_id,
                           thread_ts=thread_ts, epoch=0)
            w.execute("INSERT INTO sessions (id, seq, chat_id, thread_ts, role, preset, agent, pool, spec, report, "
                      "requires, status, epoch, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, json(?), "
                      "json(?), json(?), 'cold', 0, ?, ?)",
                      (session_id, seq, chat_id, thread_ts, role, preset, agent, pool, spec_json, report_json,
                       json.dumps(requires), w.now, w.now))
            if also is not None:
                also(w, session_id)
        await self.write(create)
        return session_id

    async def deliver(self, session_id: str, wake: Wake | Callable[[Writes], Wake], *,
                      confirm_stop_runs: int | None = None, also: Callable[[Writes], None] | None = None) -> bool:
        """Queue ``wake`` for the session; ``also`` writes the caller's own
        rows in the same transaction (a message and its event, a hop).
        ``wake`` may be built in that transaction, after ``also``, from what
        it wrote (an envelope that quotes the stored message); such a wake
        waits for the next turn. Returns False when a wake with this id was
        already delivered."""
        meta = await self.meta(session_id)
        if isinstance(wake, Wake) and wake.delivery == "now":
            self.refuses_now(session_id, confirm_stop_runs)
            wake = replace(wake, confirmed_runs=confirm_stop_runs)     # the interrupt checks it again (§0.68)

        def queue(w: Writes) -> tuple[bool, str | None]:
            retired, pool_id = w.execute("SELECT retired_at IS NOT NULL, pool FROM sessions WHERE id = ?",
                                         (meta.id,)).fetchone()
            if retired:
                raise SessionRefused("session_retired", f"Session {meta.id} was retired; deliver to its successor.",
                                     status=409)
            if also is not None:
                also(w)
            return w.wake(meta.id, wake if isinstance(wake, Wake) else wake(w)), pool_id
        inserted, pool_id = await self.write(queue)
        if inserted:
            self._clear_hold(session_id)    # a new message retries a session that waited for one
            if session_id in self._survivors:
                self._spawn(self._recheck_survivor(session_id), f"storm-survivor:{session_id}")
            pool = self._pools.get(pool_id) if pool_id else None
            if pool is not None and pool.state == "paid":
                await self._ask_usage(pool, meta.agent)     # waiting on paid usage: the person is asked again
        return inserted

    def refuses_now(self, session_id: str, confirm_stop_runs: int | None) -> None:
        """``WouldStopRuns`` when a "send now" would stop the session's live
        background runs (its runtime's interrupt stops them) and the request
        hasn't confirmed their count; ``deliver`` checks it for a ``now``
        wake, and a caller may ask first, before it changes anything."""
        live = self._live.get(session_id)
        if live is None or live.facts.turn is None:
            return
        count = _runs_an_interrupt_stops(live)
        if count and confirm_stop_runs != count:
            raise WouldStopRuns(count)

    async def stop(self, session_id: str, *, by: str) -> StopResult:
        """Stop the running turn and the live sub-agents: each sub-agent
        where the runtime stops them, then the turn; after the grace the
        process group (ch 07 §7). With only sub-agents running, they are
        stopped. Records ``turn.stopped {by, last_step}`` for a turn; the
        session stays resumable. The 3-second Undo is the UI's (sedc 09 #1)."""
        await self.meta(session_id)
        live = self._live.get(session_id)
        if live is None:
            return StopResult(False)
        async with live.lock:
            turn_id, acted = await live.stop(by=by)
        return StopResult(acted, turn_id)

    @contextlib.asynccontextmanager
    async def between_inputs(self, session_id: str) -> AsyncIterator[bool]:
        """Hold the session's operations (no input is sent to it, no Stop or
        update runs) and say whether it is between inputs: no process, an
        idle one (the runtime said its CLI is done with its input, so never
        between an input's turns), or one whose end is recorded. What retires
        a session runs inside (ch 11 §6.2: never mid-turn); one starting is
        ``starting`` in the store, and a start that finds it retired doesn't
        happen."""
        live = self._live.get(session_id)
        if live is None:
            yield True
            return
        async with live.lock:
            yield live.idle or live.status() == "cold"

    def retired(self, session_id: str) -> None:
        """The session was retired (its successor carries its conversation,
        ch 11 §6.2): its process is released at once if idle, else when its
        turn ends. Nothing new is admitted for it."""
        live = self._live.get(session_id)
        if live is not None:
            live.recycle = True
            if live.idle:
                self.became_idle(live)

    async def stop_run(self, session_id: str, native_run_id: str, *, by: str) -> bool:
        """Stop one sub-agent (ch 14 §4.3), at once: the 3-second Undo is the
        UI's (sedc 09 #1). Its ``stop_requested_at`` is recorded; only the
        run's own end event confirms the stop. ``Unsupported`` (501,
        ``runs.stop``) where the runtime can't stop one. False when the run
        isn't working in a live process."""
        meta = await self.meta(session_id)
        if not native(self._runtime_or_refuse(meta.agent).features().features, "runs.stop"):
            raise Unsupported("runs.stop", f"{meta.agent} can't stop one sub-agent; stop the session to stop them all.")
        live = self._live.get(session_id)
        if live is None:
            return False
        async with live.lock:
            return await live.stop_run(native_run_id, by=by)

    async def stop_runs(self, session_id: str, native_run_ids: Sequence[str], *, by: str) -> list[str]:
        """Stop several sub-agents of a session together, its root agent
        running on (ch 14 §4.3, "stop all"); as ``stop_run`` for each, in
        one hold of the session. ``Unsupported`` (501, ``runs.stop``) where
        the runtime can't stop one, whether or not any is working. Returns
        the native ids a stop was sent to."""
        meta = await self.meta(session_id)
        if not native(self._runtime_or_refuse(meta.agent).features().features, "runs.stop"):
            raise Unsupported("runs.stop", f"{meta.agent} can't stop one sub-agent; stop the session to stop them all.")
        live = self._live.get(session_id)
        if live is None or not native_run_ids:
            return []
        async with live.lock:
            return await live.stop_runs(native_run_ids, by=by)

    async def pause(self, session_id: str, *, reason: str, also: Callable[[Writes], None] | None = None) -> None:
        """No new turns for the session until ``resume`` (the loop breaker,
        ch 15 §9). A running turn goes on; ``stop`` it to end it. Its wakes
        stay queued."""
        await self._set_paused(session_id, reason, also)

    async def resume(self, session_id: str, *, also: Callable[[Writes], None] | None = None) -> None:
        await self._set_paused(session_id, None, also)
        self.kick()

    async def update(self, session_id: str, *, model: str | None = None, effort: c.Effort | None = None,
                     permission_mode: c.PermissionMode | None = None) -> dict[str, Any]:
        """Change the session's model, effort or permission mode where its
        runtime's feature list allows (ch 17 §2.1). A change applies from the
        next turn; effort, which restarts the process on some runtimes, waits
        until no sub-agent is running. The changed spec must pass what a new
        one does (the runtime's ``check()`` and the preset's ``requires``,
        ch 01 §6), live or cold, or nothing changes (``spec_refused``). What
        the runtime refused is reported (``update_refused``); what it took is
        kept. The catalog's default model means no model override, and a
        model that takes no effort drops the session's effort; a live
        process can't clear an override, so it is released between turns
        and the next turn starts (resumed) without it."""
        meta = await self.meta(session_id)
        runtime = self._runtime_or_refuse(meta.agent)
        features = runtime.features().features
        for key, value in (("update.model", model), ("update.effort", effort),
                           ("update.permission_mode", permission_mode)):
            if value is not None and not native(features, key):
                raise Unsupported(key, f"{meta.agent} can't change {key.split('.')[1].replace('_', ' ')} "
                                       "on a session.")
        row = await self.store.read(lambda conn: conn.execute("SELECT spec, requires FROM sessions WHERE id = ?",
                                                              (session_id,)).fetchone())
        spec = spec_from_json(row[0])
        catalog = await self._validate(runtime, features, spec, model, effort, permission_mode)
        changes = {k: v for k, v in (("model", model), ("effort", effort), ("permission_mode", permission_mode))
                   if v is not None}
        if catalog is not None and model is not None:
            # The catalog's default model clears the model override; a model that takes no effort clears the effort.
            changes["model"], kept, _takes = catalog.running(model, effort or spec.effort)
            if kept is None and (effort or spec.effort) is not None:
                changes["effort"] = None
        if not changes:
            return await self.get_session(session_id)
        try:
            _admit(runtime, _changed(spec, changes), json.loads(row[1]), meta.agent)
        except c.SpecRefused as exc:
            raise SessionRefused("spec_refused", f"Nothing was changed: {exc.message}") from exc
        self._clear_hold(session_id)
        live = self._live.get(session_id)
        refused: list[c.SupportItem] = []
        async with (live.lock if live else contextlib.nullcontext()):
            if live is not None and live.session is not None and not live.ended:
                now = {k: v for k, v in changes.items() if k != "effort" and v is not None}
                if changes.get("effort") is not None:
                    if live.idle:
                        now["effort"] = changes["effort"]
                    else:
                        live.pending_effort = changes["effort"]
                if any(changes.get(k, "") is None and getattr(spec, k) is not None for k in ("model", "effort")):
                    # The contract has no live "clear the override": the process is released between
                    # turns, and the next one starts (resumed) on the new spec.
                    live.pending_effort = None
                    live.respawn = True
                if now:
                    try:
                        report = await live.update(**now)
                        refused = [i for i in report.items if i.support == "refused"]
                    except c.RuntimeFailure as exc:     # the process just ended: the spec applies at its next start
                        log.info("storm: session %s ended during an update (%s); it applies next time",
                                 session_id, exc.message)
            applied = {k: v for k, v in changes.items() if f"update.{k}" not in {i.feature for i in refused}}
            if applied:
                new = _changed(spec, applied)
                if live is not None:
                    live.spec = new

                def save(w: Writes) -> None:
                    status, epoch = w.execute("SELECT status, epoch FROM sessions WHERE id = ?",
                                              (session_id,)).fetchone()
                    w.execute("UPDATE sessions SET spec = json(?), updated_at = ? WHERE id = ?",
                              (spec_to_json(new), w.now, session_id))
                    w.append("session", {"status": status, "reason": "updated", **applied},
                             **meta.ids.columns(epoch=epoch))
                await self.write(save)
        if live is not None and live.respawn:
            self.became_idle(live)                  # idle now: released at once; else at the turn's end
        if refused:
            raise SessionRefused("update_refused", "; ".join(f"{i.feature}: {i.note}" for i in refused))
        return await self.get_session(session_id)

    async def deliver_decision(self, item_id: str, decision: Decision, call: CallRef,
                               record: Callable[[Writes, Delivery], int]) -> Delivery:
        """The person decided the item asked for ``call``. ``record`` writes
        the decision (and its ``review.decided``) in the engine's transaction,
        compare-and-set on the item still being open (else it raises
        ``AlreadyDecided``), and returns that event's seq. The decision
        commits first; only then is anything delivered:

        * the call is still held by the process that asked: the answer goes
          into it (``in_call``);
        * otherwise (parked, its process gone, a later epoch running the
          session): an allowed permission becomes a single-use grant plus a
          ``now`` wake, anything else a ``now`` wake carrying the answer
          (ch 07 §6, §9.2).

        A failed write delivers nothing and leaves a held call held; a second
        decision while one is being written is refused (``AlreadyDecided``)."""
        if item_id in self._deciding:
            raise AlreadyDecided(item_id)
        self._deciding.add(item_id)
        try:
            return await self._deliver_decision(item_id, decision, call, record)
        finally:
            self._deciding.discard(item_id)

    async def _deliver_decision(self, item_id: str, decision: Decision, call: CallRef,
                                record: Callable[[Writes, Delivery], int]) -> Delivery:
        live = self._live.get(call.session_id)
        held = live.held_by_item(item_id) if live is not None and live.epoch == call.epoch and not live.stale \
            else None
        if held is not None and not held.future.done():
            await live.answer(held, decision, record)
            return "in_call"
        grant = call.kind == "permission" and decision.allow
        delivery: Delivery = "grant" if grant else "wake"
        expires = iso(self.clock.time() + GRANT_TTL_S)

        def deliver(w: Writes) -> None:
            seq = record(w, delivery)
            if grant:
                w.execute("INSERT INTO grants (id, session_id, tool, input_sha256, created_at, expires_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (f"grant_{uuid.uuid4().hex[:ID_HEX]}", call.session_id,
                                                        call.tool, call.input_sha256, w.now, expires))
            # As typed for the asking session; plain text, so the scrub that
            # consumes it redacts it as it stands (§0.112).
            w.wake(call.session_id, Wake(id=f"review:{item_id}:{seq}", text=_decision_text(call, decision),
                                         priority="now", reason="review", origin="system"))
        if held is not None:            # its call died with the process: settled here, never parked again
            await live.settle(held, deliver)
        else:
            await self.write(deliver)
        return delivery

    async def deliver_usage_decision(self, pool_id: str, *, keep_going: bool,
                                     record: Callable[[Writes], None]) -> None:
        """The person chose on a pool's paid usage. Continue (``keep_going``,
        while the pool is ``paid``; a pool in any other state is left as it
        is): until the pool's next limit reset as the store knows it now; with no
        known reset, ``paid_continue_until`` stays null and the pool's next
        report on paid usage asks again. Wait: the pool stays paused and
        reopens at its reset; with no known reset, the person's next message
        (or the pool's next report) asks again."""
        def apply(w: Writes) -> Pool:
            row = w.execute("SELECT state, resets_at, paid_continue_until FROM pools WHERE id = ?",
                            (pool_id,)).fetchone()
            if row is None:
                raise SessionRefused("pool_not_found", f"There is no pool {pool_id!r}; a session's view names its "
                                                       "pool.", status=404)
            state, resets_at, paid_until = row
            # Continue lifts paid usage only: a pool closed on a hard limit meanwhile stays as it is.
            new = Pool(pool_id, "open", resets_at, resets_at) if keep_going and state == "paid" else \
                Pool(pool_id, state, resets_at, paid_until)
            record(w)
            _save_pool(w, new)
            w.append("pool", {"pool": pool_id, "state": new.state, "resets_at": new.resets_at,
                              "paid_continue_until": new.paid_continue_until, "was": state})
            return new
        new = await self.write(apply)       # read and written in one transaction
        self._pools[pool_id] = new
        if new.state == "paid" and new.resets_at:
            self._reopen_at(pool_id, new.resets_at)
        self.kick()

    # ── reads ────────────────────────────────────────────────────────────

    async def meta(self, session_id: str) -> Meta:
        metas = await self._load_metas([session_id])
        if session_id not in metas:
            raise SessionNotFound(session_id)
        return metas[session_id]

    async def _load_metas(self, session_ids: Sequence[str]) -> dict[str, Meta]:
        """What never changes for each session, read once per server life
        (one query for all that aren't known yet)."""
        missing = [s for s in session_ids if s not in self._metas]
        if missing:
            marks = ", ".join("?" * len(missing))
            rows = await self.store.read(lambda conn: conn.execute(
                "SELECT s.id, s.agent, s.role, s.preset, s.chat_id, s.thread_ts, ch.project_pid, "
                "json_extract(s.spec, '$.cwd') FROM sessions s LEFT JOIN chats ch ON ch.id = s.chat_id "
                f"WHERE s.id IN ({marks})", missing).fetchall())
            projects = await asyncio.to_thread(lambda: [project_of(r[7]).name for r in rows])
            for (*fields, _cwd), project in zip(rows, projects):
                self._metas[fields[0]] = Meta(*fields, project=fields[4] or project)
        return {s: self._metas[s] for s in session_ids if s in self._metas}

    def _read_holds(self) -> Callable[[Any], dict[str, tuple[list[str], str | None]]]:
        """A store read: each session's open items its process no longer
        holds a call for (parked, or whose park failed), with the earliest
        expiry. Unparked items that were decided, withdrawn or expired are
        let go."""
        now = iso(self.clock.time())
        unparked = {s: set(items) for s, items in self._unparked.items()}

        def read(conn) -> dict[str, tuple[list[str], str | None]]:
            holds = {s: (items.split(), expires) for s, items, expires in conn.execute(_DECISION_HOLDS, (now,))}
            for session_id, items in unparked.items():
                marks = ", ".join("?" * len(items))
                for item, expires in conn.execute(
                        f"SELECT id, expires_at FROM review_items WHERE id IN ({marks}) AND state = 'open' "
                        "AND (expires_at IS NULL OR expires_at > ?)", (*items, now)):
                    have, soonest = holds.get(session_id, ([], None))
                    holds[session_id] = (sorted({*have, item}), min(filter(None, (soonest, expires)), default=None))
            return holds
        return read

    def _forget_unparked(self, holds: Mapping[str, tuple[list[str], str | None]]) -> None:
        for session_id in list(self._unparked):
            self._unparked[session_id] &= set(holds.get(session_id, ([], None))[0])
            if not self._unparked[session_id]:
                del self._unparked[session_id]

    async def _holds(self, session_ids: Iterable[str]) -> dict[str, list[str]]:
        """The items each of these sessions waits on a decision for."""
        ids = set(session_ids)
        holds = await self.store.read(self._read_holds())
        return {s: items for s, (items, _expires) in holds.items() if s in ids}

    def park_item(self, w: Writes, session_id: str, item_id: str, reason: str) -> None:
        """Park an item whose call is gone (inside a transaction). If the
        review queue fails, only its part is undone and the session is held
        as if the item were parked (fail closed): no open item whose call is
        gone is ever left without a hold."""
        w.execute("SAVEPOINT park")
        try:
            self.desk.park(w, item_id, reason)
        except Exception as exc:  # noqa: BLE001 - the hold below stands in for the park
            w.execute("ROLLBACK TO park")
            log.error("storm: the review queue could not park %s (%s); holding session %s until it is decided",
                      item_id, getattr(exc, "message", exc), session_id)
            self._unparked.setdefault(session_id, set()).add(item_id)
        finally:
            w.execute("RELEASE park")

    async def list_sessions(self, *, chat: str | None = None, thread: str | None = None, role: str | None = None,
                            state: str | None = None, after_seq: int = 0, before_seq: int | None = None,
                            limit: int = 100) -> list[dict[str, Any]]:
        """Paged by seq, oldest first: the ``limit`` after ``after_seq``, or,
        with ``before_seq`` alone, the ``limit`` right before it."""
        if state is not None and state not in STATUSES:
            raise SessionRefused("bad_filter", f"state is one of {', '.join(STATUSES)}, not {state!r}.")
        if role is not None and role not in ROLES:
            raise SessionRefused("bad_filter", f"role is one of {', '.join(sorted(ROLES))}, not {role!r}.")
        backwards = before_seq is not None and after_seq == 0
        rows = await self.store.read(lambda conn: conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE (:chat IS NULL OR chat_id = :chat) "
            "AND (:thread IS NULL OR thread_ts = :thread) AND (:role IS NULL OR role = :role) "
            "AND (:state IS NULL OR status = :state) AND seq > :after AND (:before IS NULL OR seq < :before) "
            f"ORDER BY seq {'DESC' if backwards else 'ASC'} LIMIT :limit",
            {"chat": chat, "thread": thread, "role": role, "state": state, "after": after_seq,
             "before": before_seq, "limit": limit}).fetchall())
        holds = await self._holds(r[0] for r in rows)
        return [self._view(r, holds.get(r[0])) for r in (reversed(rows) if backwards else rows)]

    async def get_sessions(self, session_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The views of these sessions, by id (unknown ids are left out)."""
        ids = list(dict.fromkeys(session_ids))
        if not ids:
            return {}
        marks = ", ".join("?" * len(ids))
        found = await self.store.read(lambda conn: conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id IN ({marks})", ids).fetchall())
        holds = await self._holds(ids)
        return {row[0]: self._view(row, holds.get(row[0])) for row in found}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = await self.store.read(lambda conn: conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?", (session_id,)).fetchone())
        if row is None:
            raise SessionNotFound(session_id)
        return self._view(row, (await self._holds([session_id])).get(session_id))

    async def status(self) -> dict[str, Any]:
        """The engine at a glance (ch 07 §11; ch 17 §2.7)."""
        def read(conn) -> dict[str, Any]:
            return {
                "by_state": dict(conn.execute("SELECT status, count(*) FROM sessions WHERE retired_at IS NULL "
                                              "GROUP BY status").fetchall()),
                "released": conn.execute("SELECT count(*) FROM sessions WHERE retired_at IS NULL AND "
                                         "status = 'cold' AND native_id IS NOT NULL").fetchone()[0],
                "queue": conn.execute("SELECT s.pool, w.priority, count(*) FROM wakes w JOIN sessions s ON "
                                      "s.id = w.session_id WHERE w.state = 'queued' GROUP BY s.pool, w.priority"
                                      ).fetchall(),
                "review_open": conn.execute("SELECT count(*), coalesce(sum(parked), 0) FROM review_items "
                                            "WHERE state = 'open'").fetchone(),
            }
        data = await self.store.read(read)
        holds = await self.store.read(self._read_holds())
        by_priority = {p: 0 for p in PRIORITY_RANK}
        by_pool: dict[str, int] = {}
        for pool, priority, count in data["queue"]:
            by_priority[priority] += count
            by_pool[pool] = by_pool.get(pool, 0) + count
        reading = self.memory.last
        lives = list(self._live.values())
        return {
            "sessions": {"by_state": data["by_state"], "live": len(lives),
                         "warm": sum(1 for live in lives if live.idle), "released": data["released"],
                         "parked": data["by_state"].get("parked", 0), "held_back": sorted(self._survivors),
                         "paused": dict(self._paused), "store_failed": sorted(self._store_failed),
                         "backing_off": {s: {"why": b.why, "failed_starts": b.failed_starts,
                                             "until_next_message": b.not_before == math.inf}
                                         for s, b in self._backoff.items()},
                         "waiting_for_decision": {s: {"items": items, "why": _decision_wait(items)}
                                                  for s, (items, _expires) in holds.items()}},
            "queue": {"by_priority": by_priority, "by_pool": by_pool, "waiting": dict(self._queued)},
            "pools": [{"id": p.id, "state": p.state, "resets_at": p.resets_at,
                       "paid_continue_until": p.paid_continue_until} for p in sorted(self._pools.values(),
                                                                                    key=lambda p: p.id)],
            "memory": {"reading": reading.as_dict() if reading else None,
                       "expected_process_bytes": self.memory.expectations()},
            "review": {"open": data["review_open"][0], "parked": data["review_open"][1]},
            "latency_ms": {"wake_to_start": _summary(self._wake_to_start),
                           "start_to_first_token": _summary(self._first_token)},
        }

    # ── what a LiveSession calls ─────────────────────────────────────────

    async def write(self, fn: Callable[[Writes], Any]) -> Any:
        """One transaction; the sessions it queued wakes for are admitted
        after the commit."""
        now = iso(self.clock.time())

        def run(tx) -> tuple[Any, set[str]]:
            w = Writes(tx, now)
            return fn(w), w.woken
        out, woken = await self.store.write(run)
        if woken:
            self.kick()
        return out

    async def spawn(self, argv, *, cwd: str, env: Mapping[str, str], label: str):
        return await self._spawner(argv, cwd=cwd, env=env, label=label)

    def reap_later(self, handle, key: tuple[int, int]) -> None:
        async def reap() -> None:
            await handle.wait()
            await self.write(lambda w: w.execute("DELETE FROM procs WHERE pid = ? AND start_ticks = ?", key))
        self._spawn(reap(), f"storm-reap:{key[0]}")

    def ephemeral(self, event: c.Event, epoch: int) -> None:
        for listener in self._listeners:
            try:
                listener.ephemeral(event, epoch)
            except Exception:  # noqa: BLE001 - a listener can't stop the record
                log.exception("storm: a listener failed on %s", type(event).__name__)

    def first_token(self, turn) -> None:
        self._first_token.append((turn.first_token - turn.started) * 1000)

    def index_row(self, live: LiveSession, totals: dict[str, int] | None = None) -> None:
        """Today's session-index row for this session (observation unchanged,
        ch 07 §11), written off the loop."""
        native_id = live.native_id
        if not native_id:
            return

        async def write() -> None:
            usage = totals if totals is not None else await self.store.read(
                lambda conn: records.usage_totals(conn, live.meta.id))
            ok = await asyncio.to_thread(write_index_row, session_id=live.meta.id, agent=live.meta.agent,
                                         cwd=live.spec.cwd, native_id=native_id, usage=usage,
                                         updated_ms=int(self.clock.time() * 1000))
            if not ok:
                log.warning("storm: no session-index row for %s (its folder %s has no project home)",
                            live.meta.id, live.spec.cwd)
        self._spawn(write(), f"storm-index:{live.meta.id}")

    def became_idle(self, live: LiveSession) -> None:
        """Idle now (ch 07 §2): recycle, release a process whose call was
        parked for memory or whose update cleared an override, or apply a
        pending effort, at once; otherwise release the process after
        ``IDLE_RELEASE_S``."""
        self._cancel_timer(live.meta.id)
        if not live.idle or self._closing:
            return
        if live.recycle or live.release_after_turn or live.respawn or live.pending_effort is not None:
            self._spawn(self._at_idle(live), f"storm-idle:{live.meta.id}")
        else:
            self._timers[live.meta.id] = self.clock.call_later(
                IDLE_RELEASE_S, lambda: self._spawn(self._release(live, "idle"), f"storm-release:{live.meta.id}"))

    def became_busy(self, live: LiveSession) -> None:
        self._cancel_timer(live.meta.id)

    def runs_changed(self, live: LiveSession) -> None:
        """The session's sub-agents changed: its aggregate goes out at most
        once every ``RUNS_INTERVAL_S``, the first change at once and the
        ones after it together when the interval is up (a timer the change
        arms, never a poll)."""
        key = f"runs:{live.meta.id}:{live.epoch}"
        if key in self._timers:
            return                          # already due: it reads the counts when it fires
        wait = 0.0 if live.runs_sent_at is None else live.runs_sent_at + RUNS_INTERVAL_S - self.clock.monotonic()
        if wait <= 0:
            self._send_runs(live)
        else:
            self._timer(key, wait, lambda: self._send_runs(live))

    def _send_runs(self, live: LiveSession) -> None:
        live.runs_sent_at = self.clock.monotonic()
        running, waiting, tokens = live.runs_counts()
        aggregate = RunsAggregate(live.meta.id, live.meta.chat_id, live.meta.thread_ts, running, waiting, tokens)
        for listener in self._listeners:
            if not isinstance(listener, RunsListener):
                continue
            try:
                listener.runs(aggregate)
            except Exception:  # noqa: BLE001 - a listener can't stop the record
                log.exception("storm: a listener failed on %s's run aggregate", live.meta.id)

    def reconcile_runs(self, live: LiveSession) -> None:
        """Reconcile the session's runs with its CLI's files, off the pump."""
        self._spawn(live.reconcile_runs(), f"storm-runs:{live.meta.id}")

    async def turn_ended(self, live: LiveSession, outcome: Outcome, retryable: c.Error | None) -> None:
        """A turn's wakes are settled: a completed turn ends any backoff; a
        retried one starts or grows it, and an overload counts toward its
        pool's warn (ch 07 §5.1)."""
        pool = self._pools.setdefault(live.pool, Pool(live.pool))
        if outcome == "completed":
            self._backoff.pop(live.meta.id, None)
            if pool.overloaded and pool.state == "warn":
                await self._set_pool(pool, "open", None, {"status": "recovered"}, agent=live.meta.agent)
        elif outcome == "retry":
            live.backed_off = True
            if live.took_input:
                self._retry_later(live.meta.id, retryable.code if retryable else "the CLI stopped")
            else:                           # it died before it took the message: a failed start
                await self._start_failed(live.meta.id, "its process ended before it took the message")
            if retryable is not None and pool.state == "open":
                await self._set_pool(pool, "warn", None, {"status": retryable.code}, agent=live.meta.agent,
                                     overloaded=True)
        self.became_idle(live)
        self.kick()

    def session_closed(self, live: LiveSession, *, unexpected: bool) -> None:
        """Its process is gone. One that ended on its own (a crash, even right
        after it started) backs the session off before the next start."""
        if self._live.get(live.meta.id) is live:
            del self._live[live.meta.id]
        self._cancel_timer(live.meta.id)
        if unexpected and not self._closing and not live.backed_off:
            if live.took_input:
                self._retry_later(live.meta.id, "its process ended")
            else:
                self._spawn(self._start_failed(live.meta.id, "its process ended before it took the message"),
                            f"storm-start-failed:{live.meta.id}")
        self.kick()

    def fenced(self, live: LiveSession) -> None:
        """Its epoch moved on: the process is stale. Nothing it says is kept;
        it is closed."""
        self._abandon(live, "fenced")

    def unusable(self, live: LiveSession) -> None:
        """The store failed recording it: nothing it does can be kept, so it
        is closed, and the session is held (not admitted) until a write
        settles it as boot recovery would: its running turn interrupted, its
        taken wakes handed back, on a new epoch. So those wakes never stay
        behind a newer turn."""
        self._abandon(live, "store_failed")
        self._store_failed.add(live.meta.id)
        self.run_later(f"store-retry:{live.meta.id}", STORE_RETRY_S,
                       lambda: self._settle_store_failed(live.meta.id))

    async def _settle_store_failed(self, session_id: str) -> None:
        if session_id not in self._store_failed or self._closing:
            return
        try:
            meta = await self.meta(session_id)

            def settle(w: Writes) -> None:
                epoch = w.execute("UPDATE sessions SET status = 'cold', epoch = epoch + 1, updated_at = ? "
                                  "WHERE id = ? RETURNING epoch", (w.now, session_id)).fetchone()[0]
                requeued: list[str] = []
                for (turn_id,) in w.execute("SELECT id FROM turns WHERE session_id = ? AND status = 'running'",
                                            (session_id,)).fetchall():
                    note = RESUME_NOTE.format(step=last_step(w.tx.conn, turn_id), why=STORE_FAILED)
                    w.execute("UPDATE turns SET status = 'interrupted', ended_at = ?, stop_reason = 'store_failed' "
                              "WHERE id = ?", (w.now, turn_id))
                    back, _ = hand_back(w.tx, turn_id, note, now=w.now, counts=False)   # not the message's fault
                    w.append("turn.finished", {"status": "interrupted", "reason": "store_failed",
                                               "wakes_requeued": back}, **meta.ids.columns(turn_id=turn_id,
                                                                                          epoch=epoch))
                    requeued += back
                for (item,) in w.execute("SELECT id FROM review_items WHERE session_id = ? AND state = 'open' "
                                         "AND parked = 0 AND native IS NOT NULL", (session_id,)).fetchall():
                    self.park_item(w, session_id, item, "process_ended")      # its call died with the process
                lost = run_records.lose(w, now=w.now, session_id=session_id).get(session_id, [])
                w.append("session", {"status": "cold", "reason": "store_failed", "wakes_requeued": requeued,
                                     "runs_lost": lost}, **meta.ids.columns(epoch=epoch))
            await self.write(settle)
        except Exception as exc:  # noqa: BLE001 - the store still fails: stay held, try again later
            log.warning("storm: session %s is still held: the store can't settle it yet (%s)", session_id,
                        getattr(exc, "message", exc))
            self.run_later(f"store-retry:{session_id}", STORE_RETRY_S,
                           lambda: self._settle_store_failed(session_id))
            return
        self._store_failed.discard(session_id)
        self.kick()

    def _abandon(self, live: LiveSession, reason: str) -> None:
        live.close_reason = live.close_reason or reason
        if self._live.get(live.meta.id) is live:
            del self._live[live.meta.id]
        if live.session is not None:
            self._spawn(live.session.close(), f"storm-{reason}:{live.meta.id}")

    async def rate_limit(self, live: LiveSession, event: c.RateLimit) -> None:
        pool = self._pools.setdefault(live.pool, Pool(live.pool))
        state, resets_at = admission.pool_state(event, pool, iso(self.clock.time()))
        await self._set_pool(pool, state, resets_at, {"status": event.status, "window": event.window,
                                                      "utilization": event.utilization, "overage": event.overage},
                             agent=live.meta.agent)

    async def credentials_changed(self, live: LiveSession) -> None:
        """A runtime saw a new login (the person reconnected, ch 07 §8). The
        account is read at every start anyway, so new processes start in the
        new account's pool; the processes that started before ``live`` did (and
        ``live``) hold the old login and are recycled at their next idle
        boundary. One started since has the new login and is left alone."""
        agent = live.meta.agent
        now_pool = await self._current_pool(agent)
        targets = [other for other in self._live.values()
                   if other.meta.agent == agent and other.attached_at <= live.attached_at and not other.recycle]
        ids = [other.meta.id for other in targets]

        def mark(w: Writes) -> None:
            w.tx.conn.executemany("UPDATE sessions SET recycle_pending = 1, updated_at = ? WHERE id = ?",
                                  [(w.now, s) for s in ids])
            w.append("pool", {"pool": live.pool, "recycle": ids, "reason": "credentials_changed", "now": now_pool})
        if ids:
            await self.write(mark)
        for other in targets:
            other.recycle = True
            if other.idle:
                self.became_idle(other)

    # ── admission ────────────────────────────────────────────────────────

    def kick(self) -> None:
        self._wakeup.set()

    async def _admission_loop(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            try:
                await self._admission_pass()
            except Exception:  # noqa: BLE001 - one failed pass must not stop admission
                log.exception("storm: an admission pass failed")

    async def _admission_pass(self) -> None:
        now_s = self.clock.time()

        read_holds = self._read_holds()

        def read(conn) -> tuple[list, dict, set[str]]:
            return (conn.execute(
                "SELECT w.session_id, w.priority, min(w.created_at), "
                "max(coalesce(json_extract(w.meta, '$.delivery') = 'now', 0)), "
                "max(coalesce(json_extract(w.meta, '$.delivery') = 'add', 0)) "
                "FROM wakes w JOIN sessions s ON s.id = w.session_id WHERE w.state = 'queued' "
                "AND s.retired_at IS NULL GROUP BY w.session_id, w.priority").fetchall(),
                read_holds(conn),
                # The person's own new message runs a session that waits on a decision: they are here.
                {r[0] for r in conn.execute(
                    f"SELECT DISTINCT session_id FROM wakes WHERE state = 'queued' AND {_NEW_PERSON_MESSAGE}")})
        rows, holds, person_waiting = await self.store.read(read)
        self._forget_unparked(holds)
        held_for_decision: set[str] = set()
        for session_id, (_items, expires) in holds.items():
            if session_id in person_waiting:
                continue
            held_for_decision.add(session_id)
            if (until := parse_ts(expires)) is not None:      # its expiry lets the session go
                self._timer(f"decision:{session_id}", max(0.0, until.timestamp() - now_s), self.kick)
        ready: dict[str, dict[str, Any]] = {}
        for session_id, priority, oldest, has_now, has_add in rows:
            entry = ready.setdefault(session_id, {"rank": admission.LOWEST_RANK, "oldest": oldest, "now": False,
                                                  "add": False, "later": None})
            entry["rank"] = min(entry["rank"], admission.effective_rank(priority, oldest, now_s))
            entry["oldest"] = min(entry["oldest"], oldest)
            entry["now"] |= bool(has_now)
            entry["add"] |= bool(has_add)
            if priority == "later":
                entry["later"] = oldest
        wanted = [s for s in ready if s not in self._survivors and s not in self._admitting
                  and s not in self._paused and s not in held_for_decision and s not in self._store_failed
                  and self._ready(s)]
        metas = await self._load_metas(wanted)
        current: dict[str, str] = {}            # agent → its current pool, read once a pass

        async def current_pool(agent: str) -> str:
            if agent not in current:
                current[agent] = await self._current_pool(agent)
            return current[agent]
        candidates = [(Candidate(s, ready[s]["rank"], ready[s]["oldest"], metas[s].project), ready[s], metas[s])
                      for s in wanted]
        by_id = {cand.session_id: (entry, meta) for cand, entry, meta in candidates}
        reading = None
        relieved = False
        for cand in admission.fair_order(cand for cand, _, _ in candidates):
            entry, meta = by_id[cand.session_id]
            live = self._live.get(cand.session_id)
            pid = live.pool if live is not None else await current_pool(meta.agent)
            pool = self._pools.setdefault(pid, Pool(pid))
            if not pool.admits(cand.rank) and pool.state == "warn" and (born := parse_ts(entry["later"])):
                # Held back as `later`: look again when it ages into `next`.
                self._timer(f"aging:{cand.session_id}", admission.AGING_S - (now_s - born.timestamp()), self.kick)
            if live is not None:
                self._admit_live(live, entry, cand, pool)
                continue
            if not pool.admits(cand.rank):
                await self._wait(cand.session_id, f"pool {pool.state}" + (f" until {pool.resets_at}"
                                                                         if pool.resets_at else ""))
                continue
            # A new process: the memory guard (one fresh reading a pass), then pacing.
            reading = reading or await self.memory.read()
            short = self.memory.tripped(reading, meta.agent)
            if short is not None:
                log.info("storm: %s waits for memory (%s)", cand.session_id, short)
                await self._wait(cand.session_id, MEMORY_WAIT)
                if not relieved:
                    relieved = True
                    await self._relieve_memory()
                continue
            if self._starting >= admission.MAX_STARTING:
                continue
            wait = self._next_spawn - self.clock.monotonic()
            if wait > 0:
                self._timer("spawn", wait, self.kick)
                continue
            self._next_spawn = self.clock.monotonic() + admission.SPAWN_INTERVAL_S
            self._starting += 1
            self._admit(cand.session_id, starting=True)

    def _admit_live(self, live: LiveSession, entry: Mapping[str, Any], cand: Candidate, pool: Pool) -> None:
        """A wake for a session with a process. While a turn runs, ``now``
        interrupts it and ``add`` joins it where the runtime steers (ch 07
        §4.2); everything else waits for the input's end. Between an input's
        turns (a worker's end has woken the CLI's own, no sub-agent left)
        there is no turn to join or interrupt yet: all of it waits, ``add``
        and ``now`` for that turn. With no turn but a live sub-agent, a wake
        starts a turn of its own, as with no input open."""
        if live.facts.starting or live.facts.stopping or live.ended or live.between_turns:
            return                  # its end (or the CLI's next turn), when it comes, admits again
        if live.facts.turn is not None:
            if entry["now"] and live.interrupted_turn != live.facts.turn.id and live.meta.id not in self._admitting:
                self._admitting.add(live.meta.id)
                task = self._spawn(self._interrupt_for_now(live), f"storm-admit-now:{live.meta.id}")
                task.add_done_callback(lambda _t: self._admitted(live.meta.id, False))
            elif entry["add"] and _steers(live):
                self._admit(live.meta.id)
            return
        if pool.admits(cand.rank):
            self._admit(live.meta.id)

    def _admit(self, session_id: str, *, starting: bool = False) -> None:
        self._admitting.add(session_id)
        self._queued.pop(session_id, None)
        task = self._spawn(self._run_admission(session_id), f"storm-admit:{session_id}")
        task.add_done_callback(lambda _t: self._admitted(session_id, starting))

    def _admitted(self, session_id: str, starting: bool) -> None:
        self._admitting.discard(session_id)
        if starting:
            self._starting -= 1
        self.kick()

    async def _run_admission(self, session_id: str) -> None:
        live = self._live.get(session_id)
        attached = live is None             # a start runs the checks before its process opens
        if attached:
            live = await self._attach(session_id)
            if live is None:
                return
        async with live.lock:
            if live.ended or live.between_turns:
                return              # the CLI's next turn or the input's end admits again
            steering = live.facts.turn is not None
            wakes, untold = await self.store.read(lambda conn: (conn.execute(
                "SELECT id, envelope, meta, created_at, reason FROM wakes WHERE session_id = ? AND state = 'queued' "
                "ORDER BY rowid", (session_id,)).fetchall(), conn.execute(
                "SELECT runs_lost_untold FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]))
            if steering:
                wakes = [w for w in wakes if json.loads(w[2]).get("delivery") == "add"]
            if not wakes:
                return
            if any(_new_person_message(w) for w in wakes) and not await self._person_moved_on(live):
                return
            if not steering and not attached:
                await self._before_turn(live.meta, live.spec)
            # The pump doesn't take the lock: a worker's end (the gap) or the
            # CLI's own turn may have come during the awaits above. Nothing is
            # sent on a stale look (no `add` filter, no check before a turn);
            # that change admits again (`_admitted` kicks a new pass).
            if live.ended or live.between_turns or (live.facts.turn is not None) != steering:
                return
            try:
                await live.send(_input(wakes, untold), [w[0] for w in wakes], told_lost=untold)
            except c.RuntimeFailure as exc:
                await self._send_failed(live, exc, [w[0] for w in wakes])
                return
            except records.StaleEpoch as exc:
                log.warning("storm: session %s moved to a new epoch while sending (%s); closing the stale process",
                            session_id, exc)
                live.stale = True
                self.fenced(live)
                return
        now = self.clock.time()
        for _wid, _text, _meta, created, _reason in wakes:
            if (stamp := parse_ts(created)) is not None:
                self._wake_to_start.append(max(0.0, now - stamp.timestamp()) * 1000)

    async def _person_moved_on(self, live: LiveSession) -> bool:
        """The person wrote to a session that waits on a decision: they have
        moved on, so its items whose call is gone are withdrawn as
        superseded, before their message reaches the agent (one action, one
        item: if the agent still needs it, it asks again). False if that
        failed: nothing is sent, the wakes wait (fail closed)."""
        session_id = live.meta.id
        unparked = sorted(self._unparked.get(session_id, ()))

        def withdraw(w: Writes) -> None:
            if w.execute("SELECT 1 FROM review_items WHERE session_id = ? AND state = 'open' AND parked = 1 LIMIT 1",
                         (session_id,)).fetchone():
                self.desk.supersede(w, session_id, by=PERSON_MESSAGE)
            for item in unparked:
                if w.execute("SELECT 1 FROM review_items WHERE id = ? AND state = 'open'", (item,)).fetchone():
                    self.desk.withdraw(w, item, "superseded")
        try:
            await live.write(withdraw)
        except Exception as exc:  # noqa: BLE001 - a decision could still replay the action: don't run
            log.error("storm: could not withdraw the items session %s waits on (%s); its message waits",
                      session_id, getattr(exc, "message", exc))
            return False
        if self._unparked.get(session_id) is not None:
            self._unparked[session_id] -= set(unparked)
            if not self._unparked[session_id]:
                del self._unparked[session_id]
        return True

    async def _attach(self, session_id: str) -> LiveSession | None:
        """Start a process for a cold session in its agent's current account's
        pool: a new epoch (everything an older process might still say is
        fenced off), the preparers (a thread's worktree), then the runtime
        opens it, resuming the CLI's own session. A retryable failure backs
        off; one that is not waits for the person's next message, and so does
        a stored spec that no longer passes ``check()`` and the preset's
        ``requires`` (ch 01 §6): it is never opened."""
        meta = await self.meta(session_id)
        row = await self.store.read(lambda conn: conn.execute(
            "SELECT spec, requires, native_id, epoch FROM sessions WHERE id = ?", (session_id,)).fetchone())
        spec_json, requires_json, native_id, epoch = row
        spec = spec_from_json(spec_json)
        runtime = self._runtimes(meta.agent)
        pool = await self._current_pool(meta.agent)
        # The comms token is bound to the epoch this attach starts (ch 07 §9.2).
        token = secrets.token_urlsafe(TOKEN_BYTES) if wants_session_token(spec) else None

        def bump(w: Writes) -> int | None:
            if w.execute("SELECT retired_at FROM sessions WHERE id = ?", (session_id,)).fetchone()[0] is not None:
                return None             # retired meanwhile: its successor has its wakes
            records.fence(w, session_id, epoch)
            new = w.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ? RETURNING epoch",
                            (session_id,)).fetchone()[0]
            if token is not None:
                # The previous epoch's token stays known (its process gets 409 epoch_stale); older ones go.
                w.execute("DELETE FROM comms_tokens WHERE session_id = ? AND epoch < ?", (session_id, new - 1))
                w.execute("INSERT INTO comms_tokens (sha256, session_id, epoch, created_at) VALUES (?, ?, ?, ?)",
                          (token_digest(token), session_id, new, w.now))
            records.set_status(w, meta.ids, "starting", new)
            return new
        epoch = await self.write(bump)
        if epoch is None:
            return None
        live = LiveSession(self, meta, runtime, spec, epoch, native_id, pool, session_token=token) if runtime \
            else None
        try:
            if live is None:
                raise c.RuntimeFailure("unsupported", f"{meta.agent} has no storm runtime in this server")
            _admit(runtime, live.spec, json.loads(requires_json), meta.agent)     # SpecRefused: not retryable
            for preparer in self._preparers:
                await preparer.prepare(self, meta, live.spec)
            await self._before_turn(meta, live.spec)
            self._live[session_id] = live
            async with live.lock:           # an update or Stop meanwhile waits for the process
                await live.attach()
        except asyncio.CancelledError:
            self._live.pop(session_id, None)        # the runtime discards what it started
            raise
        except Exception as exc:  # noqa: BLE001 - any failure to open leaves the session cold, retried or held
            self._live.pop(session_id, None)
            message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            retryable = getattr(exc, "retryable", True) if isinstance(exc, c.RuntimeFailure) else True
            log.warning("storm: session %s could not open (%s): %s", session_id,
                        "retrying" if retryable else "waiting for the next message", message)

            def failed(w: Writes) -> None:
                records.fence(w, session_id, epoch)
                records.set_status(w, meta.ids, "cold", epoch, {"reason": "open_failed", "error": message,
                                                                "retryable": retryable})
            with contextlib.suppress(Exception):
                await self.write(failed)
            if retryable:
                await self._start_failed(session_id, message)
            else:
                self._hold_for_person(session_id, message)
            return None
        return live

    async def _before_turn(self, meta: Meta, spec: c.SessionSpec) -> None:
        """The checks before a turn starts. They only report (ch 16 §11.2),
        so one that fails is logged and the turn starts anyway."""
        for check in self._checks:
            try:
                await check.before_turn(self, meta, spec)
            except Exception:  # noqa: BLE001 - a report that failed never holds a turn back
                log.exception("storm: a check before %s's turn failed; the turn starts", meta.id)

    async def _send_failed(self, live: LiveSession, exc: c.RuntimeFailure, wake_ids: list[str]) -> None:
        """``send`` refused: the wakes were never handed to a turn, so they
        stay queued, unless they can never be sent (dropped, with the
        reason). A limit closes the pool; any other failure that may pass
        backs the session off (ch 07 §5.1), and one that won't waits for the
        person's next message, saying why: never an immediate retry."""
        log.warning("storm: sending to session %s failed: %s", live.meta.id, exc.message)
        if exc.code == "rate_limited":
            pool = self._pools.setdefault(live.pool, Pool(live.pool))
            resets = iso(exc.retry_at.timestamp()) if exc.retry_at else None
            await self._set_pool(pool, "closed", resets, {"status": "limited"}, agent=live.meta.agent)
        elif exc.code in ("unsupported", "spec_refused"):
            def drop(w: Writes) -> None:
                w.tx.conn.executemany("UPDATE wakes SET state = 'dropped', done_at = ? WHERE id = ? AND "
                                      "state = 'queued'", [(w.now, wid) for wid in wake_ids])
                scrub(w.tx, wake_ids)
                w.append("session", {"status": live.status(), "reason": "input_refused", "error": exc.message,
                                     "wakes_dropped": wake_ids}, **live.meta.ids.columns(epoch=live.epoch))
            await live.write(drop)
        elif exc.code == "process_died":
            live.dying = True           # not again: the pump records the end, then it starts afresh
        elif exc.retryable:
            self._retry_later(live.meta.id, exc.message)
        else:
            self._hold_for_person(live.meta.id, exc.message)
            await live.write(lambda w: w.append("session", {"status": live.status(), "reason": "send_failed",
                                                            "error": exc.message},
                                                **live.meta.ids.columns(epoch=live.epoch)))

    async def _interrupt_for_now(self, live: LiveSession) -> None:
        """"Send now": interrupt the running turn; its end admits the wakes.
        The check at delivery can pass with no turn running yet, so the
        background runs the interrupt would stop are checked again here, as it
        acts (§0.68): if no queued ``now`` confirmed that count, nothing is
        stopped and those wakes are added to the turn instead (ch 07 §4.2)."""
        async with live.lock:
            turn = live.facts.turn
            if turn is None or live.interrupted_turn == turn.id:
                return
            urgent = await self.store.read(lambda conn: conn.execute(
                "SELECT id, json_extract(meta, '$.confirmed_runs') FROM wakes WHERE session_id = ? "
                "AND state = 'queued' AND json_extract(meta, '$.delivery') = 'now'", (live.meta.id,)).fetchall())
            if not urgent or live.facts.turn is not turn:
                return                          # taken, or the turn ended meanwhile: its end admits the wakes
            count = _runs_an_interrupt_stops(live)
            if count and all(confirmed != count for _id, confirmed in urgent):
                log.info("storm: a send-now for session %s would stop %d unconfirmed background run(s); "
                         "added to the turn instead", live.meta.id, count)
                await self.write(lambda w: w.tx.conn.executemany(
                    "UPDATE wakes SET meta = json_set(meta, '$.delivery', 'add') WHERE id = ? AND state = 'queued'",
                    [(wake_id,) for wake_id, _confirmed in urgent]))
                return
            live.interrupted_turn = turn.id      # once per turn: the engine's view of it may lag
            await live.stop(by=None)

    # ── backoff and holds ────────────────────────────────────────────────

    def _ready(self, session_id: str) -> bool:
        backoff = self._backoff.get(session_id)
        return backoff is None or backoff.not_before <= self.clock.monotonic()

    def _retry_later(self, session_id: str, why: str, *, failed_starts: int | None = None) -> None:
        last = self._backoff.get(session_id)
        delay = min(RETRY_MAX_S, last.delay * 2 if last and last.delay else RETRY_FIRST_S)
        jittered = delay * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)
        starts = failed_starts if failed_starts is not None else (last.failed_starts if last else 0)
        self._backoff[session_id] = _Backoff(self.clock.monotonic() + jittered, delay, why, starts)
        self._timer(f"retry:{session_id}", jittered, self.kick)

    async def _start_failed(self, session_id: str, why: str) -> None:
        """A start failed (retryably). After ``MAX_FAILED_STARTS`` in a row the
        session waits for the person, saying why; it tries again on their next
        message."""
        last = self._backoff.get(session_id)
        count = (last.failed_starts if last else 0) + 1
        if count < MAX_FAILED_STARTS:
            self._retry_later(session_id, why, failed_starts=count)
            return
        message = (f"Couldn't start this session's CLI {count} times in a row ({why}). Fix that, then send a "
                   "message to try again.")
        self._hold_for_person(session_id, message, failed_starts=count)
        meta = await self.meta(session_id)

        def say(w: Writes) -> None:
            status, epoch = w.execute("SELECT status, epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()
            w.append("session", {"status": status, "reason": "start_failed", "error": message},
                     **meta.ids.columns(epoch=epoch))
        await self.write(say)

    def _hold_for_person(self, session_id: str, why: str, *, failed_starts: int = 0) -> None:
        """No retry until the person sends again (or changes the session)."""
        self._backoff[session_id] = _Backoff(math.inf, 0.0, why, failed_starts)
        self._cancel_timer(f"retry:{session_id}")

    def _clear_hold(self, session_id: str) -> None:
        backoff = self._backoff.get(session_id)
        if backoff is not None and backoff.not_before == math.inf:
            del self._backoff[session_id]

    async def _set_paused(self, session_id: str, reason: str | None, also: Callable[[Writes], None] | None) -> None:
        meta = await self.meta(session_id)

        def save(w: Writes) -> None:
            status, epoch = w.execute("SELECT status, epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()
            w.execute("UPDATE sessions SET paused = ?, updated_at = ? WHERE id = ?", (reason, w.now, session_id))
            w.append("session", {"status": status, "reason": "paused" if reason else "resumed",
                                 **({"why": reason} if reason else {})}, **meta.ids.columns(epoch=epoch))
            if also is not None:
                also(w)
        # The in-memory pause changes with the write that records it (as a
        # session's facts do), so no admission pass slips between the two.
        before = self._paused.get(session_id)
        if reason is None:
            self._paused.pop(session_id, None)
        else:
            self._paused[session_id] = reason
        try:
            await self.write(save)
        except BaseException:
            if before is None:
                self._paused.pop(session_id, None)
            else:
                self._paused[session_id] = before
            raise

    async def _wait(self, session_id: str, why: str) -> None:
        """A cold session with a ready wake that a gate holds back waits as
        ``queued``, saying why (ch 07 §2)."""
        if self._queued.get(session_id) == why:
            return
        meta = await self.meta(session_id)
        await self.write(lambda w: records.set_status(w, meta.ids, "queued", _epoch_of(w, session_id),
                                                      {"reason": why}))
        self._queued[session_id] = why

    # ── the memory guard ─────────────────────────────────────────────────

    async def _relieve_memory(self) -> None:
        """The guard tripped: release the least recently used idle process;
        failing that, park the longest-held approval, whose process is then
        released at its turn's end (ch 07 §5.2, §6). Nothing more is parked
        while such a release is pending."""
        if any(live.release_after_turn for live in self._live.values()):
            return
        idle = sorted((live for live in self._live.values() if live.idle), key=lambda live: live.last_active)
        if idle:
            self._spawn(self._release(idle[0], "memory"), f"storm-release:{idle[0].meta.id}")
            return
        holders = [live for live in self._live.values() if any(
            h.kind == "permission" and h.item_id and not h.future.done() for h in live.facts.held.values())]
        holders.sort(key=lambda live: min(h.since for h in live.facts.held.values()))
        for live in holders:
            if await live.park_oldest():
                return

    async def _memory_tick(self) -> None:
        reading = await self.memory.read()
        lives = [live for live in self._live.values() if live.pid]
        peaks = await asyncio.to_thread(lambda: [peak_rss_bytes(live.pid) for live in lives])
        for live, peak in zip(lives, peaks):
            if peak:
                self.memory.learn(live.meta.agent, peak)
        if reading is not None and MEMORY_WAIT in self._queued.values():
            self.kick()             # the pass reads again, admits or relieves

    async def _release(self, live: LiveSession, reason: str) -> None:
        async with live.lock:
            if live.idle and self._live.get(live.meta.id) is live:
                await live.close(reason)

    async def _at_idle(self, live: LiveSession) -> None:
        async with live.lock:
            if not live.idle or self._live.get(live.meta.id) is not live:
                return
            if live.recycle or live.release_after_turn or live.respawn:
                await live.close("recycle" if live.recycle else "memory" if live.release_after_turn else "updated")
                return
            effort, live.pending_effort = live.pending_effort, None
            await live.update(effort=effort)
        self.became_idle(live)

    # ── retention (ch 08 §6) ─────────────────────────────────────────────

    async def _prune(self) -> None:
        await retention.prune(self.store, self.lock, now=self.clock.time())

    # ── pools ────────────────────────────────────────────────────────────

    async def _current_pool(self, agent: str) -> str:
        """The pool a process of ``agent`` started now runs in: its account is
        read each time (``describe()`` keeps its probes, so this is cheap), so a
        reconnect while nothing runs moves new work to the new account."""
        return pool_id(agent, await self._read_account(agent))

    async def _read_account(self, agent: str) -> str | None:
        runtime = self._runtimes(agent)
        try:
            info = await runtime.describe() if runtime is not None else None
        except c.RuntimeFailure as exc:
            log.info("storm: %s's account is unknown (%s)", agent, exc.message)
            return None
        return info.account if info is not None and info.logged_in else None

    async def _set_pool(self, pool: Pool, state: admission.PoolState, resets_at: str | None,
                        snapshot: Mapping[str, Any], *, agent: str, overloaded: bool = False) -> None:
        """Commit a pool's new state, then use it. Moving onto paid usage
        opens a ``usage`` ask in the same transaction (a failing review queue
        leaves the pool paused, unasked)."""
        if (state, resets_at) == (pool.state, pool.resets_at):
            if state == "paid":
                await self._ask_usage(pool, agent)          # still paid: the person has an item to answer
            return
        new = replace(pool, state=state, resets_at=resets_at, overloaded=overloaded and state == "warn")
        ask = _usage_ask(pool.id, agent) if state == "paid" else None

        def save(w: Writes) -> None:
            _save_pool(w, new, snapshot)
            w.append("pool", {"pool": pool.id, "state": state, "resets_at": resets_at, "was": pool.state})
            if ask is not None:
                _try(w, "usage", lambda: self.desk.open(w, ask))
        await self.write(save)
        self._pools[pool.id] = new
        if state in ("closed", "paid") and resets_at:
            self._reopen_at(pool.id, resets_at)
        if state in ("open", "warn"):
            self.kick()

    async def _ask_usage(self, pool: Pool, agent: str) -> None:
        """Ask the person about a paid pool; the review queue keeps one open
        item per pool, so asking again changes nothing while one is open."""
        await self.write(lambda w: _try(w, "usage", lambda: self.desk.open(w, _usage_ask(pool.id, agent))))

    def _reopen_at(self, pool_id: str, resets_at: str) -> None:
        """A closed or paid pool opens again at its limit's reset."""
        when = parse_ts(resets_at)
        if when is None:
            return

        async def reopen() -> None:
            pool = self._pools[pool_id]
            if pool.state in ("closed", "paid") and pool.resets_at == resets_at:
                await self._set_pool(replace(pool, paid_continue_until=None), "open", None, {"status": "reset"},
                                     agent=pool_agent(pool_id))
        delay = when.timestamp() - self.clock.time()
        if delay <= 0:                  # the window reset already
            self._spawn(reopen(), f"storm-pool:{pool_id}")
            return
        self._timer(f"pool:{pool_id}", delay, lambda: self._spawn(reopen(), f"storm-pool:{pool_id}"))

    # ── restarts and survivors ───────────────────────────────────────────

    async def _park_restarted(self, items: Sequence[str]) -> None:
        """The calls a restart left held are parked through the review queue
        (the decision then comes back as a grant and a wake)."""
        if not items:
            return

        def park(w: Writes) -> None:
            for item in items:
                row = w.execute("SELECT session_id FROM review_items WHERE id = ?", (item,)).fetchone()
                self.park_item(w, row[0] if row else "", item, "restart")
        await self.write(park)

    async def _say_held_back(self, session_id: str, pid: int | None = None) -> None:
        meta = await self.meta(session_id)
        why = (f"a process from before the restart (pid {pid}) may still run for this session; stop it, then send "
               "again" if pid else "a process from before the restart may still run for this session")
        def say(w: Writes) -> None:
            status, epoch = w.execute("SELECT status, epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()
            w.append("session", {"status": status, "reason": "held_back", "error": why},
                     **meta.ids.columns(epoch=epoch))
        await self.write(say)

    async def _reconcile_restarted(self, session_ids: Sequence[str]) -> None:
        """Boot's run reconcile (ch 14 §2.2), after recovery made every
        working run ``lost``: runs only the CLI's files show are stored too,
        as ``lost``, since their process is gone and nothing says how they
        ended. Only for the process recovery fenced: a session re-admitted
        meanwhile (a new epoch) has a live process whose runs are its own.
        One session at a time; the runtime reads the files off the loop."""
        marks = ", ".join("?" * len(session_ids))
        rows = await self.store.read(lambda conn: conn.execute(
            f"SELECT id, native_id, epoch FROM sessions WHERE id IN ({marks}) AND native_id IS NOT NULL ORDER BY id",
            tuple(session_ids)).fetchall())
        for session_id, native_id, epoch in rows:
            await self._store_lost_runs(await self.meta(session_id), native_id, epoch)

    async def _store_lost_runs(self, meta: Meta, native_id: str, recovered_epoch: int) -> None:
        runtime = self._runtimes(meta.agent)
        if runtime is None or meta.id in self._live:
            return
        try:
            infos = await runtime.list_runs(native_id)
        except c.RuntimeFailure as exc:
            log.info("storm: session %s's runs weren't reconciled after the restart (%s)", meta.id, exc.message)
            return

        def found(w: Writes) -> None:
            if _epoch_of(w, meta.id) == recovered_epoch:
                run_records.found(w, meta.ids, infos, epoch=recovered_epoch, native_id=native_id,
                                  state_of=lambda _run: "lost")
        await self.write(found)

    async def _recheck_survivor(self, session_id: str) -> None:
        """A message for a held-back session: is the old process gone now?"""
        rows = await self.store.read(lambda conn: conn.execute(
            "SELECT pid, start_ticks FROM procs WHERE session_id = ?", (session_id,)).fetchall())
        left = [(pid, ticks) for pid, ticks in rows if not await reap_orphan(pid, ticks)]
        if left:
            await self._say_held_back(session_id, left[0][0])
            return
        await self.write(lambda w: w.execute("DELETE FROM procs WHERE session_id = ?", (session_id,)))
        self._survivors.discard(session_id)
        self.kick()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _committed(self, events: list[StoredEvent]) -> None:
        for listener in self._listeners:
            try:
                listener.committed(events)
            except Exception:  # noqa: BLE001 - a listener can't undo a commit
                log.exception("storm: a listener failed after seq %s", events[-1].seq)

    def _spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm: task %s failed", task.get_name(), exc_info=task.exception())

    def _timer(self, key: str, delay: float, callback: Callable[[], None]) -> None:
        self._cancel_timer(key)

        def fire() -> None:
            self._timers.pop(key, None)
            callback()
        self._timers[key] = self.clock.call_later(delay, fire)

    def run_later(self, key: str, delay: float, work: Callable[[], Awaitable[Any]]) -> None:
        """Run ``work()`` as an engine task after ``delay`` seconds of the
        engine's clock (a timer shutdown cancels)."""
        self._timer(key, delay, lambda: self._spawn(work(), f"storm-{key}"))

    def _cancel_timer(self, key: str) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _runtime_or_refuse(self, agent: str) -> c.Runtime:
        runtime = self._runtimes(agent)
        if runtime is None:
            raise SessionRefused("no_runtime", f"{agent} has no storm runtime in this server.")
        return runtime

    async def _validate(self, runtime: c.Runtime, features: Mapping[str, c.FeatureSupport], spec: c.SessionSpec,
                        model: str | None, effort: str | None, mode: str | None) -> Catalog | None:
        """Refuse a change the runtime can't take; the catalog read for a
        model or effort, else None."""
        catalog = None
        if model is not None or effort is not None:
            try:
                catalog = Catalog(runtime.name, tuple(await runtime.models()),
                                  features.get("effort", c.FeatureSupport()).values)
            except c.RuntimeFailure as exc:
                raise SessionRefused("runtime_unavailable", exc.message, status=503) from exc
            if model is not None and catalog.model(model) is None:
                raise SessionRefused("unsupported_model", f"{model} isn't one of {runtime.name}'s models: "
                                                          f"{', '.join(sorted(m.id for m in catalog.models))}.")
            efforts = catalog.efforts(model or spec.model)
            if effort is not None and effort not in efforts:
                raise SessionRefused("unsupported_effort", f"Effort {effort} isn't offered here: "
                                                           f"{', '.join(efforts) or 'none'}.")
        if mode is not None:
            modes = features.get("permissions.mode", c.FeatureSupport()).values
            if mode not in modes:
                raise SessionRefused("unsupported_permission_mode", f"{mode} isn't a mode of {runtime.name}: "
                                                                    f"{', '.join(modes)}.")
            if mode == "bypass" and spec.permissions.mode != "bypass":
                raise SessionRefused("mode_refused", "Only a session created in bypass mode may use it.")
        return catalog

    def _view(self, row: tuple, decision_items: list[str] | None = None) -> dict[str, Any]:
        (session_id, seq, chat_id, thread_ts, role, preset, agent, pool, spec, native_id, status, epoch, recycle,
         paused, retired_at, created_at, updated_at) = row
        spec = json.loads(spec)
        live = self._live.get(session_id)
        turn = live.facts.turn if live else None
        return {
            "id": session_id, "seq": seq, "chat_id": chat_id, "thread_ts": thread_ts, "role": role,
            "preset": preset, "agent": agent, "pool": pool, "model": spec.get("model"), "effort": spec.get("effort"),
            "permission_mode": (spec.get("permissions") or {}).get("mode"), "cwd": spec.get("cwd"),
            "native_id": native_id, "state": status, "epoch": epoch, "recycle_pending": bool(recycle),
            "paused": paused, "retired_at": retired_at, "created_at": created_at, "updated_at": updated_at,
            "current_turn": {"id": turn.id, "origin": turn.origin} if turn else None,
            "live_runs": len(live.facts.runs) if live else 0,
            "held": len(live.facts.held) if live else 0,
            "held_items": live.held_items() if live else [],
            # Parked items it waits on: its queued work runs with the decision (a new message runs at once).
            "waiting_for_decision": {"items": decision_items, "why": _decision_wait(decision_items)}
            if decision_items else None,
        }


_SESSION_COLUMNS = ("id, seq, chat_id, thread_ts, role, preset, agent, pool, spec, native_id, status, epoch, "
                    "recycle_pending, paused, retired_at, created_at, updated_at")


def _try(w: Writes, name: str, fn: Callable[[], Any]) -> None:
    """Run a collaborator's part of a transaction under a savepoint: if it
    fails, only its own writes are undone, and the rest of the change
    commits."""
    w.execute(f"SAVEPOINT {name}")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - its failure is logged; the engine's own change stands
        w.execute(f"ROLLBACK TO {name}")
        log.warning("storm: the review queue could not %s: %s", name, getattr(exc, "message", exc))
    finally:
        w.execute(f"RELEASE {name}")


def _admit(runtime: c.Runtime, spec: c.SessionSpec, requires: Iterable[str], agent: str) -> c.SupportReport:
    """ch 01 §6's safety rule for a spec about to be stored or opened: the
    runtime's ``check()``, then every feature the preset ``requires`` must be
    enforced by the CLI or the host, never the prompt or nothing. Raises
    ``SpecRefused``."""
    report = runtime.check(spec)
    weak = [f for f in requires
            if (item := report.weakest(f)) is None or item.enforced_by in ("prompt", "none")
            or item.support in ("ignored", "refused")]
    if weak:
        raise c.SpecRefused(f"{agent} can't enforce {', '.join(weak)} for this session, so it would rest on the "
                            "prompt alone; choose another agent.", report)
    return report


def _changed(spec: c.SessionSpec, changes: Mapping[str, Any]) -> c.SessionSpec:
    """``spec`` with an update's model, effort and permission mode."""
    return replace(spec, model=changes.get("model", spec.model), effort=changes.get("effort", spec.effort),
                   permissions=replace(spec.permissions, mode=changes.get("permission_mode", spec.permissions.mode)))


def _runs_an_interrupt_stops(live: LiveSession) -> int:
    """The background runs interrupting the turn would stop as well: none
    where the runtime's interrupt spares them."""
    interrupt = live.features().get("interrupt")
    return live.background_runs() if interrupt is not None and interrupt.behaviour == STOPS_RUNS else 0


def _steers(live: LiveSession) -> bool:
    entry = live.features().get("send_while_running")
    return entry is not None and entry.behaviour == "steered"


def _input(wakes: list[tuple], runs_lost: int = 0) -> c.UserInput:
    """Several queued wakes go into one turn, in order, each its own marked
    block (ch 07 §4.1). The turn is the person's when any of its wakes is.
    Sub-agents lost since the agent last heard (``runs_lost``) are said
    first (ch 14 §5.3)."""
    metas = [json.loads(w[2]) for w in wakes]
    origin = "person" if any(m.get("origin") == "person" for m in metas) else \
        "system" if all(m.get("origin") == "system" for m in metas) else "wake"
    contexts = [m["context"] for m in metas if m.get("context")]
    if len(wakes) == 1:
        text = wakes[0][1] or ""
    else:
        text = "\n\n".join(f"[{n} of {len(wakes)}: {reason}]\n{envelope or ''}"
                           for n, (_id, envelope, _meta, _created, reason) in enumerate(wakes, 1))
    if runs_lost:
        text = f"{run_records.untold_note(runs_lost)}\n\n{text}"
    return c.UserInput(text=text, origin=origin, context="\n\n".join(contexts) or None)


def _new_person_message(wake: tuple) -> bool:
    meta = json.loads(wake[2])
    return wake[0].startswith("msg:") and meta.get("origin") == "person" and not meta.get("handed_back")


def _decision_wait(items: list[str]) -> str:
    return f"waiting for your decision on {', '.join(items)}"


def runtime_package(runtime: c.Runtime) -> str:
    """The package a runtime's class lives in: every module of it logs under
    this name, so its logs are redacted like storm's."""
    module = type(runtime).__module__
    return getattr(sys.modules.get(module), "__package__", None) or module


def _decision_text(call: CallRef, decision: Decision) -> str:
    if call.kind == "question":
        answers = "; ".join(f"{q}: {a}" for q, a in (decision.answers or {}).items())
        return f"The person answered your question ({call.summary}): {answers or 'no answer'}."
    if decision.allow:
        return f"The person allowed {call.summary}. Run it again."
    note = f" Their note: {decision.message}" if decision.message else ""
    return f"The person denied {call.summary}.{note}"


def _usage_ask(pool_id: str, agent: str) -> Ask:
    return Ask(kind="usage", pool=pool_id, title=f"Your {agent} subscription has moved onto paid extra usage. "
                                                 "Continue until the next limit reset, or wait?")


def _save_pool(w: Writes, pool: Pool, snapshot: Mapping[str, Any] | None = None) -> None:
    w.execute("INSERT INTO pools (id, state, resets_at, snapshot, paid_continue_until, updated_at) "
              "VALUES (?, ?, ?, json(?), ?, ?) ON CONFLICT (id) DO UPDATE SET state = excluded.state, "
              "resets_at = excluded.resets_at, snapshot = coalesce(excluded.snapshot, pools.snapshot), "
              "paid_continue_until = excluded.paid_continue_until, updated_at = excluded.updated_at",
              (pool.id, pool.state, pool.resets_at, json.dumps(snapshot) if snapshot else None,
               pool.paid_continue_until, w.now))


def _epoch_of(w: Writes, session_id: str) -> int:
    return w.execute("SELECT epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]


def _summary(samples: deque[float]) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "p50": None, "p95": None}
    ordered = sorted(samples)
    return {"count": len(ordered), "p50": round(statistics.median(ordered)),
            "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))])}
