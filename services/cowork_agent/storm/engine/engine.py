"""The session engine: runs every storm session end to end (design ch 07).

It speaks only the runtime contract and never names an agent: per-runtime
differences reach it through each runtime's feature list.

Public API (what the surface packages call):

* ``create_session(spec, agent=, role=, preset=, …)`` → the session id;
* ``deliver(session_id, wake, …)`` queues an input (idempotent by the wake's
  id) and the engine admits it;
* ``stop(session_id, by=)`` stops the running turn and its sub-agents;
* ``update(session_id, model=, effort=, permission_mode=)``;
* ``pause(session_id, reason=)`` / ``resume(session_id)``: no new turns;
* ``subscribe(listener)`` / ``unsubscribe(listener)`` for the stream
  (``ports.Listener``; ``boot(listeners=…)`` registers before admission);
* ``deliver_decision(…)`` / ``deliver_usage_decision(…)`` for the review queue;
* ``status()``, ``list_sessions(…)``, ``get_session(…)`` for the routes.

One owner per change: a session's process events are recorded by its pump
(``session.LiveSession``); operations on a session (attach, send, stop,
update, release) hold its lock; everything else here runs on the event loop
and writes through the store, one transaction per change, the store's seq
being the order. No polling: admission runs when something changes (a wake, a
turn's end, a pool or memory change, a timer), and the memory guard's
5-second reading is the one periodic read.

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
import statistics
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import admission, records
from services.cowork_agent.storm.engine.admission import Candidate, Pool
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
    ReviewDesk,
    Timer,
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
from services.cowork_agent.storm.engine.specs import spec_from_json, spec_to_json
from services.cowork_agent.storm.store import EngineLock, RecoveryReport, Store, StoredEvent
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
#: Open parked review items, per session: the session waits for the
#: decision (ch 07 §2, §6), except for a new message from the person.
_DECISION_HOLDS = ("SELECT session_id, group_concat(id, ' '), min(expires_at) FROM review_items "
                   "WHERE state = 'open' AND parked = 1 AND session_id IS NOT NULL "
                   "AND (expires_at IS NULL OR expires_at > ?) GROUP BY session_id")
#: How long shutdown waits for every session's process to close, then for the
#: engine's other tasks (index rows, reaping) to finish.
SHUTDOWN_TIMEOUT_S = 30.0
SHUTDOWN_TASKS_S = 5.0
#: A pool's account when the runtime reports none (logged out, or no way to tell).
DEFAULT_ACCOUNT = "default"
#: The hex digits of a new id (``cse_…``, ``grant_…``).
ID_HEX = 16
#: Consecutive failed starts (a process that dies before it takes its wake,
#: or a retryable failure to open) before the session waits for the person,
#: like a start that fails for good. A bound on a retry loop, not a cap.
MAX_FAILED_STARTS = 5

RuntimeLoader = Callable[[str], "c.Runtime | None"]


def pool_id(agent: str, account: str | None) -> str:
    """A pool is one agent's account (ch 07 §5.1)."""
    return f"{agent}:{account or DEFAULT_ACCOUNT}"


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
                 spawner=spawn_stream) -> None:
        self.store = store
        self.lock = lock
        self.desk: ReviewDesk = desk or NoReviewDesk()
        self.clock: Clock = clock or LoopClock()
        self.memory = memory or MemoryGuard()
        self.xo_ports = xo_ports
        self.project_config_paths: tuple[str, ...] = ()
        self._runtimes = runtimes
        self._providers = tuple(providers)
        self._spawner = spawner
        self._listeners: list[Listener] = list(listeners)
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
        self.project_config_paths = tuple(sorted({
            p for name in self._providers if (rt := self._runtimes(name)) is not None
            for p in rt.features().features.get("project_config_paths", c.FeatureSupport()).values}))

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
        for session_id in sorted(self._survivors & set(report.sessions_to_rekick)):
            log.warning("storm: not re-admitting %s: a process recovery could not stop may still run for it",
                        session_id)
            await self._say_held_back(session_id)
        self._loops = [asyncio.create_task(self._admission_loop(), name="storm-admission"),
                       asyncio.create_task(run_forever("storm memory guard", self._memory_tick,
                                                       interval_s=lambda: READ_INTERVAL_S, logger=log),
                                           name="storm-memory-guard")]
        self.kick()

    def background_loops(self) -> list[asyncio.Task]:
        return list(self._loops)

    async def shutdown(self) -> None:
        """Stop admitting, release every process (a turn it ends is handed
        back, a held call is parked), and let go of the store and the lock."""
        self._closing = True
        for task in self._loops:
            task.cancel()
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        admissions = [t for t in self._tasks if t.get_name().startswith("storm-admit")]
        for task in admissions:
            task.cancel()
        await asyncio.gather(*self._loops, *admissions, return_exceptions=True)
        closing = asyncio.gather(*(live.close("shutdown") for live in list(self._live.values())),
                                 return_exceptions=True)
        try:
            await asyncio.wait_for(closing, SHUTDOWN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("storm: sessions did not close within %s s; the next boot's recovery stops what is left",
                      SHUTDOWN_TIMEOUT_S)
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=SHUTDOWN_TASKS_S)
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
        return self._runtime_or_refuse((await self.meta(session_id)).agent).features()

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
        try:
            report = runtime.check(spec)
        except c.SpecRefused as exc:
            raise SessionRefused("spec_refused", exc.message) from exc
        weak = [f for f in requires
                if (item := report.weakest(f)) is None or item.enforced_by in ("prompt", "none")
                or item.support in ("ignored", "refused")]
        if weak:
            raise SessionRefused("spec_refused", f"{agent} can't enforce {', '.join(weak)} for this session, "
                                                 "so it would rest on the prompt alone; choose another agent.")
        session_id = f"cse_{uuid.uuid4().hex[:ID_HEX]}"
        spec_json, report_json = spec_to_json(spec), json.dumps(report_rows(report))
        pool = await self._current_pool(agent)

        def create(w: Writes) -> None:
            seq = w.append("session", {"status": "cold", "reason": "created", "agent": agent, "role": role,
                                       "preset": preset}, session_id=session_id, chat_id=chat_id,
                           thread_ts=thread_ts, epoch=0)
            w.execute("INSERT INTO sessions (id, seq, chat_id, thread_ts, role, preset, agent, pool, spec, report, "
                      "status, epoch, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, json(?), json(?), "
                      "'cold', 0, ?, ?)", (session_id, seq, chat_id, thread_ts, role, preset, agent, pool,
                                           spec_json, report_json, w.now, w.now))
            if also is not None:
                also(w, session_id)
        await self.write(create)
        return session_id

    async def deliver(self, session_id: str, wake: Wake, *, confirm_stop_runs: int | None = None,
                      also: Callable[[Writes], None] | None = None) -> bool:
        """Queue ``wake`` for the session; ``also`` writes the caller's own
        rows in the same transaction (a message and its event, a hop).
        Returns False when a wake with this id was already delivered."""
        meta = await self.meta(session_id)
        live = self._live.get(session_id)
        if wake.delivery == "now" and live is not None and live.facts.turn is not None and live.facts.runs:
            interrupt = live.features().get("interrupt")
            if interrupt is not None and interrupt.behaviour == STOPS_RUNS and confirm_stop_runs != len(live.facts.runs):
                raise WouldStopRuns(len(live.facts.runs))

        def queue(w: Writes) -> bool:
            if w.execute("SELECT retired_at IS NOT NULL FROM sessions WHERE id = ?", (meta.id,)).fetchone()[0]:
                raise SessionRefused("session_retired", f"Session {meta.id} was retired; deliver to its successor.",
                                     status=409)
            if also is not None:
                also(w)
            return w.wake(meta.id, wake)
        inserted = await self.write(queue)
        if inserted:
            self._clear_hold(session_id)    # a new message retries a session that waited for one
            if session_id in self._survivors:
                self._spawn(self._recheck_survivor(session_id), f"storm-survivor:{session_id}")
        return inserted

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
        until no sub-agent is running. What the runtime refused is reported
        (``update_refused``); what it took is kept."""
        meta = await self.meta(session_id)
        runtime = self._runtime_or_refuse(meta.agent)
        features = runtime.features().features
        for key, value in (("update.model", model), ("update.effort", effort),
                           ("update.permission_mode", permission_mode)):
            if value is not None and not native(features, key):
                raise Unsupported(key, f"{meta.agent} can't change {key.split('.')[1].replace('_', ' ')} "
                                       "on a session.")
        row = await self.store.read(lambda conn: conn.execute("SELECT spec FROM sessions WHERE id = ?",
                                                              (session_id,)).fetchone())
        spec = spec_from_json(row[0])
        await self._validate(runtime, features, spec, model, effort, permission_mode)
        changes = {k: v for k, v in (("model", model), ("effort", effort), ("permission_mode", permission_mode))
                   if v is not None}
        if not changes:
            return await self.get_session(session_id)
        self._clear_hold(session_id)
        live = self._live.get(session_id)
        refused: list[c.SupportItem] = []
        async with (live.lock if live else contextlib.nullcontext()):
            if live is not None and live.session is not None and not live.ended:
                now = {k: v for k, v in changes.items() if k != "effort"}
                if "effort" in changes:
                    if live.idle:
                        now["effort"] = changes["effort"]
                    else:
                        live.pending_effort = changes["effort"]
                if now:
                    try:
                        report = await live.update(**now)
                        refused = [i for i in report.items if i.support == "refused"]
                    except c.RuntimeFailure as exc:     # the process just ended: the spec applies at its next start
                        log.info("storm: session %s ended during an update (%s); it applies next time",
                                 session_id, exc.message)
            applied = {k: v for k, v in changes.items() if f"update.{k}" not in {i.feature for i in refused}}
            if applied:
                new = replace(spec, model=applied.get("model", spec.model), effort=applied.get("effort", spec.effort),
                              permissions=replace(spec.permissions,
                                                  mode=applied.get("permission_mode", spec.permissions.mode)))
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
            w.wake(call.session_id, Wake(id=f"review:{item_id}:{seq}", text=_decision_text(call, decision),
                                         priority="now", reason="review", origin="system"))
        if held is not None:            # its call died with the process: settled here, never parked again
            await live.settle(held, deliver)
        else:
            await self.write(deliver)
        return delivery

    async def deliver_usage_decision(self, pool_id: str, *, continue_until: str | None,
                                     record: Callable[[Writes], None]) -> None:
        """The person chose on a pool's paid usage: continue until
        ``continue_until`` (the next limit reset), or wait (None): the pool
        reopens at its reset either way."""
        def apply(w: Writes) -> Pool:
            row = w.execute("SELECT state, resets_at, paid_continue_until FROM pools WHERE id = ?",
                            (pool_id,)).fetchone()
            if row is None:
                raise SessionRefused("pool_not_found", f"There is no pool {pool_id!r}; a session's view names its "
                                                       "pool.", status=404)
            state, resets_at, paid_until = row
            new = Pool(pool_id, "open" if continue_until else state, resets_at, continue_until or paid_until)
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

    async def _holds(self, session_ids: Iterable[str]) -> dict[str, list[str]]:
        """The open parked items each of these sessions waits on."""
        ids = set(session_ids)
        rows = await self.store.read(lambda conn: conn.execute(_DECISION_HOLDS, (iso(self.clock.time()),)).fetchall())
        return {s: items.split() for s, items, _expires in rows if s in ids}

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
                "holds": conn.execute(_DECISION_HOLDS, (iso(self.clock.time()),)).fetchall(),
            }
        data = await self.store.read(read)
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
                         "paused": dict(self._paused),
                         "backing_off": {s: {"why": b.why, "failed_starts": b.failed_starts,
                                             "until_next_message": b.not_before == math.inf}
                                         for s, b in self._backoff.items()},
                         "waiting_for_decision": {s: {"items": items.split(), "why": _decision_wait(items.split())}
                                                  for s, items, _expires in data["holds"]}},
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
        parked for memory, or apply a pending effort, at once; otherwise
        release the process after ``IDLE_RELEASE_S``."""
        self._cancel_timer(live.meta.id)
        if not live.idle or self._closing:
            return
        if live.recycle or live.release_after_turn or live.pending_effort is not None:
            self._spawn(self._at_idle(live), f"storm-idle:{live.meta.id}")
        else:
            self._timers[live.meta.id] = self.clock.call_later(
                IDLE_RELEASE_S, lambda: self._spawn(self._release(live, "idle"), f"storm-release:{live.meta.id}"))

    def became_busy(self, live: LiveSession) -> None:
        self._cancel_timer(live.meta.id)

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
        is closed and its turn's wakes stay taken for boot recovery."""
        self._abandon(live, "store_failed")

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

        def read(conn) -> tuple[list, list, set[str]]:
            return (conn.execute(
                "SELECT w.session_id, w.priority, min(w.created_at), "
                "max(coalesce(json_extract(w.meta, '$.delivery') = 'now', 0)), "
                "max(coalesce(json_extract(w.meta, '$.delivery') = 'add', 0)) "
                "FROM wakes w JOIN sessions s ON s.id = w.session_id WHERE w.state = 'queued' "
                "AND s.retired_at IS NULL GROUP BY w.session_id, w.priority").fetchall(),
                conn.execute(_DECISION_HOLDS, (iso(now_s),)).fetchall(),
                # The person's own new message runs a session that waits on a decision: they are here.
                {r[0] for r in conn.execute(
                    "SELECT DISTINCT session_id FROM wakes WHERE state = 'queued' AND id LIKE 'msg:%' "
                    "AND json_extract(meta, '$.origin') = 'person' "
                    "AND coalesce(json_extract(meta, '$.handed_back'), 0) = 0")})
        rows, holds, person_waiting = await self.store.read(read)
        held_for_decision: set[str] = set()
        for session_id, _items, expires in holds:
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
                  and s not in self._paused and s not in held_for_decision and self._ready(s)]
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
        if live.facts.starting or live.facts.stopping or live.ended:
            return                  # its end, when recorded, admits again
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
        if live is None:
            live = await self._attach(session_id)
            if live is None:
                return
        async with live.lock:
            if live.ended:
                return
            steering = live.facts.turn is not None
            wakes = await self.store.read(lambda conn: conn.execute(
                "SELECT id, envelope, meta, created_at, reason FROM wakes WHERE session_id = ? AND state = 'queued' "
                "ORDER BY rowid", (session_id,)).fetchall())
            if steering:
                wakes = [w for w in wakes if json.loads(w[2]).get("delivery") == "add"]
            if not wakes:
                return
            try:
                await live.send(_input(wakes), [w[0] for w in wakes])
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

    async def _attach(self, session_id: str) -> LiveSession | None:
        """Start a process for a cold session in its agent's current account's
        pool: a new epoch (everything an older process might still say is
        fenced off), then the runtime opens it, resuming the CLI's own
        session. A retryable failure backs off; one that is not waits for
        the person's next message."""
        meta = await self.meta(session_id)
        row = await self.store.read(lambda conn: conn.execute(
            "SELECT spec, native_id, epoch FROM sessions WHERE id = ?", (session_id,)).fetchone())
        spec_json, native_id, epoch = row
        runtime = self._runtimes(meta.agent)
        pool = await self._current_pool(meta.agent)

        def bump(w: Writes) -> int:
            records.fence(w, session_id, epoch)
            new = w.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ? RETURNING epoch",
                            (session_id,)).fetchone()[0]
            records.set_status(w, meta.ids, "starting", new)
            return new
        epoch = await self.write(bump)
        live = LiveSession(self, meta, runtime, spec_from_json(spec_json), epoch, native_id, pool) if runtime \
            else None
        try:
            if live is None:
                raise c.RuntimeFailure("unsupported", f"{meta.agent} has no storm runtime in this server")
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

    async def _send_failed(self, live: LiveSession, exc: c.RuntimeFailure, wake_ids: list[str]) -> None:
        """``send`` refused: the wakes were never handed to a turn, so they
        stay queued, unless they can never be sent (dropped, with the
        reason)."""
        log.warning("storm: sending to session %s failed: %s", live.meta.id, exc.message)
        if exc.code == "rate_limited":
            pool = self._pools.setdefault(live.pool, Pool(live.pool))
            resets = iso(exc.retry_at.timestamp()) if exc.retry_at else None
            await self._set_pool(pool, "closed", resets, {"status": "limited"}, agent=live.meta.agent)
        elif exc.code in ("unsupported", "spec_refused"):
            def drop(w: Writes) -> None:
                w.tx.conn.executemany("UPDATE wakes SET state = 'dropped', done_at = ? WHERE id = ? AND "
                                      "state = 'queued'", [(w.now, wid) for wid in wake_ids])
                w.append("session", {"status": live.status(), "reason": "input_refused", "error": exc.message,
                                     "wakes_dropped": wake_ids}, **live.meta.ids.columns(epoch=live.epoch))
            await live.write(drop)
        elif exc.code == "process_died":
            live.dying = True           # not again: the pump records the end, then it starts afresh

    async def _interrupt_for_now(self, live: LiveSession) -> None:
        """"Send now": interrupt the running turn; its end admits the wakes."""
        async with live.lock:
            turn = live.facts.turn
            if turn is not None and live.interrupted_turn != turn.id:
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
            if live.recycle or live.release_after_turn:
                await live.close("recycle" if live.recycle else "memory")
                return
            effort, live.pending_effort = live.pending_effort, None
            await live.update(effort=effort)
        self.became_idle(live)

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
            return
        new = replace(pool, state=state, resets_at=resets_at, overloaded=overloaded and state == "warn")
        ask = Ask(kind="usage", pool=pool.id, title=f"Your {agent} subscription has moved onto paid extra usage. "
                                                    "Continue until the next limit reset, or wait?") \
            if state == "paid" and pool.state != "paid" else None

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

    def _reopen_at(self, pool_id: str, resets_at: str) -> None:
        """A closed or paid pool opens again at its limit's reset."""
        when = parse_ts(resets_at)
        if when is None:
            return

        async def reopen() -> None:
            pool = self._pools[pool_id]
            if pool.state in ("closed", "paid") and pool.resets_at == resets_at:
                await self._set_pool(replace(pool, paid_continue_until=None), "open", None, {"status": "reset"},
                                     agent=pool_id)
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
                _try(w, "park", lambda: self.desk.park(w, item, "restart"))
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
                        model: str | None, effort: str | None, mode: str | None) -> None:
        if model is not None or effort is not None:
            try:
                models = {m.id: m for m in await runtime.models()}
            except c.RuntimeFailure as exc:
                raise SessionRefused("runtime_unavailable", exc.message, status=503) from exc
            if model is not None and model not in models:
                raise SessionRefused("unsupported_model", f"{model} isn't one of {runtime.name}'s models: "
                                                          f"{', '.join(sorted(models))}.")
            chosen = models.get(model or spec.model or "")
            efforts = chosen.efforts if chosen and chosen.efforts else features.get("effort", c.FeatureSupport()).values
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


def _steers(live: LiveSession) -> bool:
    entry = live.features().get("send_while_running")
    return entry is not None and entry.behaviour == "steered"


def _input(wakes: list[tuple]) -> c.UserInput:
    """Several queued wakes go into one turn, in order, each its own marked
    block (ch 07 §4.1). The turn is the person's when any of its wakes is."""
    metas = [json.loads(w[2]) for w in wakes]
    origin = "person" if any(m.get("origin") == "person" for m in metas) else \
        "system" if all(m.get("origin") == "system" for m in metas) else "wake"
    contexts = [m["context"] for m in metas if m.get("context")]
    if len(wakes) == 1:
        text = wakes[0][1] or ""
    else:
        text = "\n\n".join(f"[{n} of {len(wakes)}: {reason}]\n{envelope or ''}"
                           for n, (_id, envelope, _meta, _created, reason) in enumerate(wakes, 1))
    return c.UserInput(text=text, origin=origin, context="\n\n".join(contexts) or None)


def _decision_wait(items: list[str]) -> str:
    return f"waiting for your decision on {', '.join(items)}"


def _decision_text(call: CallRef, decision: Decision) -> str:
    if call.kind == "question":
        answers = "; ".join(f"{q}: {a}" for q, a in (decision.answers or {}).items())
        return f"The person answered your question ({call.summary}): {answers or 'no answer'}."
    if decision.allow:
        return f"The person allowed {call.summary}. Run it again."
    note = f" Their note: {decision.message}" if decision.message else ""
    return f"The person denied {call.summary}.{note}"


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
