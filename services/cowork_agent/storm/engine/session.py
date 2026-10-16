"""One attached session: a runtime session (its CLI process), the pump that
records its events, the running turn, the calls held for the person and the
live sub-agents (ch 07 §2, §4, §6).

Every write goes through ``LiveSession.write``: one transaction,
compare-and-set on the epoch this process attached at (a write from a stale
process raises ``StaleEpoch`` and changes nothing), and the session's state
change, when there is one, in the same commit. A session's writes run one at
a time, and a fact a write records changes as that write starts
(``prepare``), so a state change never commits ahead of the event that
caused it.

The pump is the only reader of the runtime's events and never waits on an
operation (send, stop, update), so an operation can never deadlock it. What
it stores reaches the listeners after the commit (``Store.on_commit``); what
it doesn't store (deltas, bytes, progress, a sub-agent's own steps) goes to
the listeners at once, in the same order the pump handles them.

How a turn's end settles its wakes (one transaction with ``turn.finished``):

* it finished, failed for good, was stopped, or "send now" interrupted it:
  consumed;
* the engine ended it by closing the process (the server stopping): handed
  back with the resume note, as boot recovery does (ch 07 §9.3);
* it failed on a retryable error (overload, a 5xx) or because the CLI died:
  handed back with the note, and the session backs off (ch 07 §5.1).

A wake handed back ``MAX_REDELIVERIES`` times is dropped instead, and the
person is told why.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import records
from services.cowork_agent.storm.engine.memory import peak_rss_bytes
from services.cowork_agent.storm.engine.model import AlreadyDecided, Ask, CallRef, Decision
from services.cowork_agent.storm.engine.places import policy_view
from services.cowork_agent.storm.engine.ports import Writes, iso
from services.cowork_agent.storm.engine.records import Ids, StaleEpoch, TurnEnd
from services.cowork_agent.storm.review import (
    Action,
    Grant,
    Match,
    PolicyContext,
    Rule,
    SessionRef,
    Tier,
    Verdict,
    decide,
    input_sha256,
)
from services.cowork_agent.storm.store import RESUME_NOTE, StoreFailed, dropped_message
from services.cowork_agent.storm.store.recovery import RESTARTED
from services.timestamps import now_iso

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine.engine import Engine

log = logging.getLogger(__name__)

#: How long a Stop (or "send now") gives the turn to end before the runtime
#: terminates the process group (ch 07 §7); also each sub-agent's stop.
STOP_GRACE_S = 10.0
#: How long ``close`` gives the process to exit before SIGKILL.
CLOSE_GRACE_S = 5.0
#: A one-time grant's life (ch 07 §6).
GRANT_TTL_S = 30 * 60
HOST_ERROR = "XO could not check this action (host error), so it was not allowed."
PARK_MESSAGE = ("Waiting for the person's approval of {what}; you'll be woken when they decide (review item "
                "{item}). End your turn or continue with other work.")
#: Why a turn's input was handed back, for the resume note.
CRASHED = "because the CLI stopped unexpectedly"
RETRYING = "because the model's service failed ({code}); it is retried after a pause"
#: A close the engine makes itself (not a Stop): the turn it ends is handed back.
STOP_CLOSE = "stopped"

Outcome = Literal["completed", "consumed", "handed_back", "retry"]


@dataclass(frozen=True)
class Parked:
    """What a held call gets when it is parked: deny, naming the item."""

    item_id: str


@dataclass(frozen=True)
class Meta:
    """What never changes for a session once created."""

    id: str
    agent: str
    role: str
    preset: str
    chat_id: str | None
    thread_ts: str | None
    project_pid: str | None
    project: str                 # the round-robin key: its chat, else its project's name

    @property
    def ids(self) -> Ids:
        return Ids(self.id, self.chat_id, self.thread_ts)


@dataclass
class Turn:
    id: str
    origin: str
    started: float                       # monotonic
    first_token: float | None = None
    waiting_ms: int = 0
    steps: int = 0
    tool_calls: int = 0
    last_title: str | None = None
    usage: c.TokenUsage | None = None
    rate_limit: dict[str, Any] | None = None
    error_code: str | None = None
    retryable: c.Error | None = None     # a retryable, non-fatal error the runtime reported
    stopped_by: str | None = None


@dataclass
class Held:
    """A CLI call waiting for the person."""

    request_id: str
    kind: str                            # permission | question
    call: CallRef
    since: float                         # monotonic
    future: asyncio.Future
    item_id: str | None = None
    deciding: bool = False               # a decision's write is in flight: a second one is refused


@dataclass
class _Facts:
    """What the session's state is computed from."""

    starting: bool = True
    stopping: bool = False
    closed: bool = False
    turn: Turn | None = None
    held: dict[str, Held] = field(default_factory=dict)
    runs: set[str] = field(default_factory=set)
    parked_on_close: bool = False


class LiveSession:
    def __init__(self, engine: "Engine", meta: Meta, runtime: c.Runtime, spec: c.SessionSpec, epoch: int,
                 native_id: str | None, pool: str) -> None:
        self.engine = engine
        self.meta = meta
        self.runtime = runtime
        self.spec = spec
        self.epoch = epoch
        self.native_id = native_id
        self.pool = pool                     # pinned while this process lives (ch 07 §5.1)
        self.session: c.Session | None = None
        self.facts = _Facts()
        self.pid: int | None = None
        self.attached_at = engine.clock.monotonic()
        self.last_active = self.attached_at
        self.close_reason: str | None = None
        self.recycle = False
        self.release_after_turn = False      # a call was parked for memory: release the process at idle
        self.pending_effort: c.Effort | None = None
        self.interrupting = False
        self.interrupted_turn: str | None = None    # the turn "send now" interrupted
        self.stale = False                   # fenced, or the store failed: nothing more is recorded
        self.backed_off = False              # its session backs off already for this process's failure
        self.closed = asyncio.Event()
        self._written_status: str | None = "starting"
        self._ended_turn: str | None = None
        self._pump: asyncio.Task | None = None
        self._policy: tuple | None = None
        self._lock = asyncio.Lock()          # the session's operations: send, stop, update, close
        self._writing = asyncio.Lock()       # its writes, one at a time

    # ── state ────────────────────────────────────────────────────────────

    def status(self) -> str:
        f = self.facts
        if f.closed:
            return "parked" if f.parked_on_close else "cold"
        if f.stopping:
            return "stopping"
        if f.starting:
            return "starting"
        if f.held:
            return "waiting_on_person"
        if f.turn is not None or f.runs:
            return "running"
        return "warm"

    @property
    def idle(self) -> bool:
        """No running turn, nothing held, no live sub-agent (ch 07 §2)."""
        return self.status() == "warm" and not self.interrupting

    @property
    def ended(self) -> bool:
        """The process is gone or going (its end may not be recorded yet)."""
        return self.facts.closed or (self.session is not None and self.session.state in ("closing", "closed",
                                                                                          "failed"))

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def features(self) -> Mapping[str, c.FeatureSupport]:
        return self.runtime.features().features

    def held_items(self) -> list[str]:
        return [h.item_id for h in self.facts.held.values() if h.item_id]

    async def write(self, fn: Callable[[Writes], Any] | None = None, *, reason: str | None = None,
                    data: Mapping[str, Any] | None = None,
                    prepare: Callable[[], Callable[[], None] | None] | None = None) -> Any:
        """One transaction for this process: fenced on its epoch, with the
        session's new state (if it changed) in the same commit. ``prepare``
        changes the facts as this write starts and returns how to undo that
        if the write fails."""
        async with self._writing:
            undo = prepare() if prepare is not None else None
            status, before = self.status(), self._written_status
            ids, epoch = self.meta.ids, self.epoch
            extra = {**({"reason": reason} if reason else {}), **(data or {})}

            def run(w: Writes) -> Any:
                records.fence(w, ids.session_id, epoch)
                out = fn(w) if fn else None
                if status != before:
                    records.set_status(w, ids, status, epoch, extra)
                return out
            try:
                out = await self.engine.write(run)
            except BaseException:
                if undo is not None:
                    undo()
                raise
            self._written_status = status
            return out

    def _set(self, **changes: Any) -> Callable[[], None]:
        """A ``prepare`` that sets facts, undone if the write fails."""
        before = {k: getattr(self.facts, k) for k in changes}
        for key, value in changes.items():
            setattr(self.facts, key, value)

        def undo() -> None:
            for key, value in before.items():
                setattr(self.facts, key, value)
        return undo

    # ── attach and close ─────────────────────────────────────────────────

    async def attach(self) -> None:
        """Open the runtime session (resuming the CLI's own session when there
        is one) and start the pump. Raises ``RuntimeFailure``; the caller
        records it."""
        spec = self.spec
        if self.native_id and spec.persist:
            spec = replace(spec, resume=self.native_id, fork=False, fork_at=None)
        self.session = await self.runtime.open(spec, EngineHost(self), session_id=self.meta.id)
        self.native_id = self.session.native_id
        self._pump = asyncio.create_task(self._run_pump(), name=f"storm-pump:{self.meta.id}")
        report = json.dumps(report_rows(self.session.report))
        native, pool = self.native_id, self.pool
        try:
            await self.write(lambda w: w.execute(
                "UPDATE sessions SET native_id = ?, report = json(?), recycle_pending = 0, pool = ? WHERE id = ?",
                (native, report, pool, self.meta.id)), prepare=lambda: self._set(starting=False), reason="attached",
                data={"native_id": native, "pool": pool})
        except BaseException:
            await asyncio.shield(self.close("attach_failed"))    # never leave an unrecorded process running
            raise
        self.engine.index_row(self)
        self.engine.became_idle(self)

    async def close(self, reason: str) -> None:
        """Release the process (the session stays resumable) and wait until
        the pump has recorded the end. A turn this ends is handed back."""
        self.close_reason = self.close_reason or reason
        if self.session is None or self._pump is None:
            return                          # never attached: nothing runs
        await self.session.close(grace_s=CLOSE_GRACE_S)
        await self.closed.wait()

    # ── operations (the engine holds ``lock``) ───────────────────────────

    async def send(self, user_input: c.UserInput, wake_ids: list[str]) -> str:
        """Send the input (a new turn, or into the running one where the
        runtime steers) and hand the wakes to that turn in one transaction."""
        assert self.session is not None
        turn_id = await self.session.send(user_input)
        if self.facts.turn is None and turn_id != self._ended_turn:
            self._begin_turn(turn_id, user_input.origin)
        origin = user_input.origin

        def take(w: Writes) -> str:
            records.ensure_turn(w, self.meta.ids, turn_id, origin, self.epoch)
            return records.take_wakes(w, wake_ids, turn_id)
        await self.write(take)
        return turn_id

    async def stop(self, *, by: str | None) -> tuple[str | None, bool]:
        """End the running turn and the live sub-agents (ch 07 §7, ch 14
        §5.3): each sub-agent where the runtime stops them one by one, then
        the turn; with no turn running, the sub-agents alone (through the
        runtime's interrupt when it ends them all). ``by`` None is "send
        now": the turn only. Returns (the turn it stopped, whether it stopped
        anything)."""
        turn, runs = self.facts.turn, sorted(self.facts.runs)
        if self.session is None or self.ended or (turn is None and (by is None or not runs)):
            return None, False
        features = self.features()
        if by is not None:
            if turn is not None:
                turn.stopped_by = by
            await self.write(prepare=lambda: self._set(stopping=True), reason="stop", data={"by": by})
        acted = False
        try:
            if by is not None and runs and native(features, "runs.stop"):
                await asyncio.gather(*(self._stop_run(run) for run in runs))
                acted = True
            if turn is not None or (not acted and _behaviour(features, "interrupt") == STOPS_RUNS):
                if native(features, "interrupt"):
                    self.interrupting = True
                    await self.session.interrupt(grace_s=STOP_GRACE_S)
                else:
                    self.close_reason = STOP_CLOSE
                    await self.session.close(grace_s=CLOSE_GRACE_S)
                acted = True
        finally:
            self.interrupting = False
            if self.facts.stopping:
                with contextlib.suppress(StaleEpoch, StoreFailed):
                    await self.write(prepare=lambda: self._set(stopping=False))
            # The turn may have ended while this ran: re-admit and release as its end would.
            self._after_change()
            self.engine.kick()
        return (turn.id if turn else None), acted

    async def _stop_run(self, run: str) -> None:
        """``stop_run`` is confirmed only by the run's end event; a runtime
        that doesn't answer in time can't hold the Stop up."""
        assert self.session is not None
        with contextlib.suppress(c.RuntimeFailure, asyncio.TimeoutError):
            await asyncio.wait_for(self.session.stop_run(run), STOP_GRACE_S)

    async def update(self, **changes: Any) -> c.SupportReport:
        assert self.session is not None
        return await self.session.update(**changes)

    async def park_oldest(self) -> bool:
        """Under memory pressure: park the longest-held permission call (deny
        naming its review item), and release the process once its turn has
        ended; the decision comes back as a grant and a wake. Returns
        whether one was parked."""
        held = [h for h in self.facts.held.values() if h.kind == "permission" and h.item_id
                and not h.future.done() and not h.deciding]
        if not held:
            return False
        oldest = min(held, key=lambda h: h.since)
        item = oldest.item_id
        await self.settle(oldest, lambda w: self.engine.desk.park(w, item, "memory"), reason="memory")
        self.release_after_turn = True
        oldest.future.set_result(Parked(item))
        return True

    async def answer(self, held: Held, decision: Decision, record: Callable[[Writes, str], int]) -> None:
        """The person decided a call still held in this process: the decision
        commits first, then the answer goes into the call (``in_call``). If
        the write fails the call stays held; a second decision meanwhile is
        refused."""
        if held.deciding:
            raise AlreadyDecided(held.item_id or held.request_id)
        held.deciding = True
        try:
            await self.settle(held, lambda w: record(w, "in_call"))
        finally:
            held.deciding = False
        if not held.future.done():
            held.future.set_result(decision)

    async def settle(self, held: Held, fn: Callable[[Writes], Any], **write: Any) -> Any:
        """Write ``fn`` and, with it, take ``held`` off the session; put it
        back if the write fails."""
        def take() -> Callable[[], None]:
            self.facts.held.pop(held.request_id, None)
            return lambda: self.facts.held.__setitem__(held.request_id, held)
        out = await self.write(fn, prepare=take, **write)
        if self.facts.turn is not None:
            self.facts.turn.waiting_ms += int((self.engine.clock.monotonic() - held.since) * 1000)
        self._after_change()
        return out

    def held_by_item(self, item_id: str) -> Held | None:
        return next((h for h in self.facts.held.values() if h.item_id == item_id), None)

    # ── held calls (from the Host) ───────────────────────────────────────

    async def hold(self, kind: str, request_id: str, ask: Ask, call: CallRef) -> Any:
        """Open a review item and wait for the person, as long as it takes
        (ch 07 §6). Returns the ``Decision``, or ``Parked``. Raises
        ``ReviewUnavailable`` (and the like) when no item could open."""
        held = Held(request_id=request_id, kind=kind, call=call, since=self.engine.clock.monotonic(),
                    future=asyncio.get_running_loop().create_future())
        self.engine.became_busy(self)

        def prepare() -> Callable[[], None]:
            self.facts.held[request_id] = held
            return lambda: self.facts.held.pop(request_id, None)

        def open_item(w: Writes) -> None:
            # Set before the commit is announced: a decision can come back
            # the moment the item is seen.
            held.item_id = self.engine.desk.open(w, ask)
        opening = asyncio.ensure_future(self.write(open_item, prepare=prepare))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The CLI withdrew the call (or ended) while the item opened: the
            # item still opens, and the pump settles it as for a held call.
            with contextlib.suppress(Exception):
                await opening
            raise
        except BaseException:
            held.item_id = None
            self._after_change()
            raise
        return await held.future

    def _after_change(self) -> None:
        if self.idle:
            self.engine.became_idle(self)

    async def policy_context(self) -> tuple[PolicyContext, SessionRef]:
        if self._policy is None:
            self._policy = await asyncio.to_thread(
                policy_view, session_id=self.meta.id, role=self.meta.role, preset=self.meta.preset,
                cwd=self.spec.cwd, thread_ts=self.meta.thread_ts, project_pid=self.meta.project_pid,
                project_config_paths=self.engine.project_config_paths)
        places, ref = self._policy
        now = iso(self.engine.clock.time())
        session_id, project_pid = self.meta.id, self.meta.project_pid

        def read(conn) -> tuple[list, list]:
            grants = conn.execute("SELECT id, session_id, tool, input_sha256, expires_at FROM grants "
                                  "WHERE session_id = ? AND used_at IS NULL AND expires_at > ?",
                                  (session_id, now)).fetchall()
            # Only rules that can apply here: the workspace's, and this project's.
            rules = conn.execute("SELECT id, scope, effect, tool, match, sentence, tier, project_pid, thread_ts, "
                                 "from_item, expires_at FROM rules WHERE scope = 'workspace' OR project_pid = ?",
                                 (project_pid,)).fetchall()
            return grants, rules
        grants, rules = await self.engine.store.read(read)
        ctx = PolicyContext(places=places, now=now, rules=tuple(_rules(rules)),
                            grants=tuple(Grant(*g) for g in grants), xo_ports=self.engine.xo_ports)
        return ctx, ref

    # ── the pump ─────────────────────────────────────────────────────────

    def _begin_turn(self, turn_id: str, origin: str) -> None:
        self.facts.turn = Turn(id=turn_id, origin=origin, started=self.engine.clock.monotonic())
        self.engine.became_busy(self)

    async def _run_pump(self) -> None:
        assert self.session is not None
        try:
            async for event in self.session.events():
                if self.stale:
                    continue                # drained, never recorded
                try:
                    await self._handle(event)
                except StaleEpoch as exc:
                    log.warning("storm: dropped an event from a stale process (%s): %s; closing it",
                                exc, type(event).__name__)
                    self.stale = True
                    self.engine.fenced(self)
                except StoreFailed as exc:
                    # Nothing it does can be recorded: stop it, so boot recovery's
                    # rules apply to what it was doing.
                    log.error("storm: the store failed recording session %s (%s); closing its process",
                              self.meta.id, exc.message)
                    self.stale = True
                    self.engine.unusable(self)
                except Exception:  # noqa: BLE001 - one event's failure must not stop the session's record
                    log.exception("storm: recording %s for session %s failed", type(event).__name__, self.meta.id)
        finally:
            if not self.facts.closed:   # ended without a recorded SessionClosed
                self.facts.closed = True
                self.engine.session_closed(self, unexpected=False)
            self.closed.set()

    async def _handle(self, ev: c.Event) -> None:
        f, turn = self.facts, self.facts.turn
        self.last_active = self.engine.clock.monotonic()
        if turn is not None and ev.turn_id == turn.id and turn.first_token is None and isinstance(
                ev, (c.TextDelta, c.ThinkingDelta, c.ToolStarted, c.ToolInputDelta)):
            turn.first_token = self.engine.clock.monotonic()
            self.engine.first_token(turn)
        if isinstance(ev, c.TurnStarted):
            if turn is None or turn.id != ev.turn_id:
                self._begin_turn(ev.turn_id, ev.origin)
            await self.write(lambda w: records.ensure_turn(w, self.meta.ids, ev.turn_id, ev.origin, self.epoch))
        elif isinstance(ev, c.TurnFinished):
            await self._turn_finished(ev)
        elif isinstance(ev, c.SessionClosed):
            await self._session_closed(ev)
        elif isinstance(ev, c.RunStarted):
            self.engine.became_busy(self)
            await self.write(lambda w: records.run_event(w, self.meta.ids, ev, self.epoch),
                             prepare=lambda: self._set(runs=f.runs | {ev.run}))
        elif isinstance(ev, c.RunFinished):
            await self.write(lambda w: records.run_event(w, self.meta.ids, ev, self.epoch),
                             prepare=lambda: self._set(runs=f.runs - {ev.run}))
            self._after_change()
        elif isinstance(ev, (c.PermissionResolved, c.QuestionAnswered)) and ev.request_id in f.held:
            # Resolved without our answer: the CLI withdrew the call.
            held = f.held[ev.request_id]

            def withdrawn(w: Writes) -> None:
                if held.item_id:
                    self.engine.desk.withdraw(w, held.item_id, "withdrawn")
                if ev.run is None:
                    records.step(w, self.meta.ids, ev, self.epoch)
            await self.settle(held, withdrawn)
        elif type(ev) in records.STEP_TYPES and ev.run is None:
            if turn is not None and ev.turn_id == turn.id:
                turn.steps += isinstance(ev, records.COUNTED_STEPS)
                if isinstance(ev, c.ToolStarted):
                    turn.tool_calls += 1
                    turn.last_title = ev.title
                elif isinstance(ev, c.Error):
                    turn.error_code = ev.code
                    if ev.retryable and not ev.fatal:
                        turn.retryable = ev
            await self.write(lambda w: records.step(w, self.meta.ids, ev, self.epoch))
            if isinstance(ev, c.Notice) and ev.code == "credentials_changed":
                await self.engine.credentials_changed(self)
        else:
            if isinstance(ev, c.Usage) and turn is not None:
                turn.usage = ev.usage
            elif isinstance(ev, c.RateLimit):
                if turn is not None:
                    turn.rate_limit = {"status": ev.status, "window": ev.window, "resets_at": ev.resets_at,
                                       "utilization": ev.utilization, "overage": ev.overage}
                await self.engine.rate_limit(self, ev)
            elif isinstance(ev, c.RunSnapshot):
                unknown = {r.run_id for r in ev.live if r.state in ("running", "waiting")} - f.runs
                if unknown:         # live runs the engine missed: busy (they end with their RunFinished)
                    f.runs |= unknown
                    self.engine.became_busy(self)
            self.engine.ephemeral(ev, self.epoch)

    def _outcome(self, ev: c.TurnFinished, turn: Turn) -> tuple[str, Outcome, str | None]:
        """The turn's status, what becomes of its wakes, and the resume note
        when they are handed back."""
        if ev.status == "completed":
            return "finished", "completed", None
        if ev.status == "interrupted":
            if turn.stopped_by:
                return "stopped", "consumed", None
            if self.close_reason not in (None, STOP_CLOSE):
                return "interrupted", "handed_back", RESTARTED      # the engine closed it (the server stopping)
            return "interrupted", "consumed", None
        if ev.status == "failed" and turn.retryable is not None:
            return "failed", "retry", RETRYING.format(code=turn.retryable.code)
        if ev.status == "failed" and self.close_reason is None and self.ended:
            return "failed", "retry", CRASHED                      # the CLI died mid-turn
        return "failed", "consumed", None

    async def _turn_finished(self, ev: c.TurnFinished) -> None:
        clock = self.engine.clock
        turn = self.facts.turn if self.facts.turn and self.facts.turn.id == ev.turn_id else \
            Turn(id=ev.turn_id, origin="injected", started=clock.monotonic())
        status, outcome, why = self._outcome(ev, turn)
        peak = await asyncio.to_thread(peak_rss_bytes, self.pid) if self.pid else None
        if peak:
            self.engine.memory.learn(self.meta.agent, peak)
        end = TurnEnd(
            turn_id=ev.turn_id, status=status, stop_reason=ev.status,
            error_class=turn.error_code or (ev.status if status == "failed" else None), error=ev.error,
            usage=ev.usage or turn.usage, wall_ms=int((clock.monotonic() - turn.started) * 1000),
            wait_person_ms=turn.waiting_ms, steps=turn.steps, tool_calls=turn.tool_calls,
            peak_rss_kb=peak // 1024 if peak else None, rate_limit=turn.rate_limit,
            stopped_by=turn.stopped_by if status == "stopped" else None, last_step=turn.last_title,
            hand_back=RESUME_NOTE.format(step=f"“{turn.last_title}”" if turn.last_title else "it started",
                                         why=why) if why else None)

        def ended() -> Callable[[], None]:
            self._ended_turn = ev.turn_id
            return self._set(turn=None)

        def finish(w: Writes) -> tuple[dict[str, int], list[str]]:
            records.ensure_turn(w, self.meta.ids, ev.turn_id, turn.origin, self.epoch)
            dropped = records.finish_turn(w, self.meta.ids, end, self.epoch)
            if dropped:
                w.append("session", {"status": self.status(), "reason": "input_dropped", "wakes_dropped": dropped,
                                     "error": dropped_message(why or "the same way")},
                         **self.meta.ids.columns(epoch=self.epoch))
            return records.usage_totals(w, self.meta.id), dropped
        totals, _dropped = await self.write(finish, prepare=ended)
        self.engine.index_row(self, totals)
        await self.engine.turn_ended(self, outcome, turn.retryable)

    async def _session_closed(self, ev: c.SessionClosed) -> None:
        """The process is gone. A call still held dies with it and is parked:
        the person's decision comes back as a grant and a wake (ch 07 §6)."""
        items: list[str] = []

        def gone() -> None:
            f = self.facts
            items.extend(h.item_id for h in f.held.values() if h.item_id)
            f.held.clear()
            f.turn, f.closed, f.stopping, f.parked_on_close = None, True, False, bool(items)
            f.runs = set()

        def park(w: Writes) -> None:
            for item in items:
                self.engine.desk.park(w, item, "process_ended")
        try:
            await self.write(park, prepare=gone, reason=self.close_reason or ev.reason,
                             data={"exit_code": ev.exit_code})
        finally:
            self.engine.session_closed(self, unexpected=self.close_reason is None)


class EngineHost:
    """The ``Host`` one attached process calls back into (ch 01 §4.5)."""

    def __init__(self, live: LiveSession) -> None:
        self.live = live
        self.launcher = _Launcher(live)

    async def decide_permission(self, req: c.PermissionRequest) -> c.PermissionDecision:
        live = self.live
        try:
            ctx, ref = await live.policy_context()
            verdict = decide(Action.from_request(req, ref), ctx)
            if verdict.effect == "allow":
                if not await self._spend(verdict):
                    return c.PermissionDecision(allow=False, message="That one-time permission was already used; "
                                                                     "ask again.")
                return c.PermissionDecision(allow=True)
        except Exception:  # noqa: BLE001 - fail closed: any failure here is a deny
            log.exception("storm: deciding a %s check for session %s failed", req.kind, live.meta.id)
            return c.PermissionDecision(allow=False, message=HOST_ERROR)
        if verdict.effect == "deny":
            return c.PermissionDecision(allow=False, message=verdict.message)
        call = CallRef(session_id=live.meta.id, epoch=live.epoch, kind="permission", tool=req.tool,
                       input_sha256=input_sha256(req.input), summary=_what(req))
        answer = await self._hold("permission", req.request_id, call, Ask(
            kind="permission", title=req.summary, request=req, verdict=verdict, call=call, run=req.run,
            **self._where()))
        if isinstance(answer, Parked):
            return c.PermissionDecision(allow=False, message=PARK_MESSAGE.format(what=call.summary,
                                                                                 item=answer.item_id))
        if isinstance(answer, c.PermissionDecision):
            return answer
        return c.PermissionDecision(allow=answer.allow, message=answer.message, scope=answer.scope,
                                    updated_input=answer.updated_input)

    async def answer_question(self, q: c.Question) -> Mapping[str, Any]:
        live = self.live
        title = "; ".join(str(x.get("question") or "") for x in q.questions) or "A question for you"
        call = CallRef(session_id=live.meta.id, epoch=live.epoch, kind="question", tool="question",
                       input_sha256=input_sha256({"questions": list(q.questions)}), summary=title)
        answer = await self._hold("question", q.request_id, call, Ask(
            kind="question", title=title, question=q, call=call, run=q.run, **self._where()))
        if not isinstance(answer, Decision):    # no item could open: the runtime denies (host error)
            raise RuntimeError(answer.message)
        return dict(answer.answers or {})

    async def call_tool(self, call_id: str, name: str, args: Mapping[str, Any]) -> c.ToolResult:
        return c.ToolResult(content=f"XO serves no host tool named {name}", is_error=True)

    async def _hold(self, kind: str, request_id: str, call: CallRef, ask: Ask) -> Any:
        """Hold for the person; a deny (``PermissionDecision``) when no review
        item could be opened (fail closed)."""
        try:
            return await self.live.hold(kind, request_id, ask, call)
        except Exception as exc:  # noqa: BLE001 - no item, no answer: a deny
            message = getattr(exc, "message", None) or HOST_ERROR
            log.warning("storm: could not ask the person for session %s: %s", self.live.meta.id, message)
            return c.PermissionDecision(allow=False, message=message)

    def _where(self) -> dict[str, Any]:
        live = self.live
        return {"session_id": live.meta.id, "epoch": live.epoch, "chat_id": live.meta.chat_id,
                "thread_ts": live.meta.thread_ts, "turn_id": live.facts.turn.id if live.facts.turn else None}

    async def _spend(self, verdict: Verdict) -> bool:
        """Spend the grant and count the rules that allowed, in one
        transaction. False if the grant was spent meanwhile."""
        if verdict.grant_id is None and not verdict.rules_used:
            return True
        grant, used = verdict.grant_id, verdict.rules_used

        def spend(w: Writes) -> bool:
            if grant and w.execute("UPDATE grants SET used_at = ? WHERE id = ? AND used_at IS NULL",
                                   (w.now, grant)).rowcount != 1:
                return False
            w.tx.conn.executemany("UPDATE rules SET uses = uses + 1 WHERE id = ?", [(r,) for r in used])
            return True
        return await self.live.write(spend)


class _Launcher:
    """``contract.Launcher`` on the one executor: every process is registered
    in ``procs`` (pid, start time, session, epoch) until it is reaped, so boot
    recovery can stop one a crash left behind (ch 07 §9.3)."""

    def __init__(self, live: LiveSession) -> None:
        self.live = live

    async def spawn(self, argv, *, cwd: str, env: Mapping[str, str], label: str):
        live = self.live
        handle = await live.engine.spawn(argv, cwd=cwd, env=env, label=label)
        row = (handle.pid, handle.start_ticks or 0, live.meta.id, live.epoch, str(argv[0]), now_iso())
        try:
            await live.engine.write(lambda w: w.execute("INSERT OR REPLACE INTO procs VALUES (?, ?, ?, ?, ?, ?)", row))
        except BaseException:
            await handle.terminate(CLOSE_GRACE_S)     # unregistered, it must not run
            raise
        live.pid = handle.pid
        live.engine.reap_later(handle, row[:2])
        return handle


#: An ``interrupt`` that also ends every live sub-agent (the feature list's word).
STOPS_RUNS = "stops_runs"


def _what(req: c.PermissionRequest) -> str:
    """What the person is asked about, in one phrase: the exact command where
    there is one (ch 15 §6.2: "approval of `npm install p-retry@6`")."""
    return f"`{req.command}`" if req.command else req.summary


def native(features: Mapping[str, c.FeatureSupport], key: str) -> bool:
    entry = features.get(key)
    return entry is not None and entry.support in ("native", "emulated")


def _behaviour(features: Mapping[str, c.FeatureSupport], key: str) -> str | None:
    entry = features.get(key)
    return entry.behaviour if entry is not None else None


def report_rows(report: c.SupportReport) -> list[dict[str, Any]]:
    return [{"feature": i.feature, "support": i.support, "mechanism": i.mechanism, "enforced_by": i.enforced_by,
             "note": i.note} for i in report.items]


def _rules(rows: list[tuple]) -> list[Rule]:
    """The stored rules. An allow rule that can't be read is skipped and
    logged, so one bad row doesn't turn every check into a host error; a deny
    rule that can't be read fails the check closed (it raises)."""
    out = []
    for row in rows:
        try:
            out.append(_rule(row))
        except (ValueError, TypeError, KeyError) as exc:
            if row[2] != "allow":
                raise
            log.error("storm: allow rule %s can't be read (%s); it is ignored", row[0], exc)
    return out


def _rule(row: tuple) -> Rule:
    rid, scope, effect, kind, match, sentence, tier, project_pid, thread_ts, from_item, expires_at = row
    m = {k: tuple(v) if isinstance(v, list) else v for k, v in json.loads(match).items()}
    return Rule(id=rid, scope=scope, effect=effect, kind=kind, match=Match(**m), sentence=sentence,
                tier=Tier[tier.upper()], project_pid=project_pid, thread_ts=thread_ts, from_item=from_item,
                expires_at=expires_at)
