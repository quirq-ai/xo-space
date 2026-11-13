"""Jobs: agent sessions started on a schedule (design ch 10 §4.5, ch 11
§14.6, ch 12 §5.8, ch 17 §2.8; §0.44, §0.51, §0.107; w5-w7-design W6).

An agent job is a timer (``records/timers``, with ``job``): ``every`` when
it repeats, ``at`` when it runs once (with no time: only when run by hand).
The timer loop (``storm/timers.py``), which ``Jobs`` starts, fires it:
``run_fire`` opens the run's thread (``runs.open_run``), and the loop
records what came of it as the timer's last fire. Command jobs stay in the
scheduler (``utils/commands/scheduler.py``).

**Who makes a job.** The person, through ``POST /api/schedules {kind:
"agent"}`` (``save``: localhost-only, no review item; ``PUT`` gives it a new
definition, or turns it off or on). An agent only asks: ``create_job`` opens
a ``create_job`` review item with the exact title, schedule and prompt, and
nothing exists until the person allows it. Allow registers the job
(``register``), approved by that item, in one transaction with the item's
``decision.job_id`` and a ``job.registered`` event; Send back registers
nothing (the agent gets the note as a wake, like every ask). A decision that
committed but wasn't registered when the server stopped is registered at
the next start (``recover``), never twice: the item is its request key. A
job whose project was removed meanwhile records ``decision.job_error``
instead, and the agent, which heard "allowed", is told by a wake.

A job's title and prompt are stored and shown, and the store keeps words
only redacted, so one holding a secret is refused: its runs would read the
redacted prompt.

**From the scheduler, once** (``adopt``, at each start): each of the
scheduler's agent jobs becomes a timer with the same id, its schedule, last
run and on or off kept (its request key is its id, so a second start copies
nothing), then is turned off in the scheduler's files
(``hand_over_agent_job``). A fire the scheduler had handed over and not
settled is opened once (or found, if it opened its thread). A job naming a
chat this store doesn't have isn't copied, and is turned off there, as its
next run would have been; one whose title or prompt holds a secret is
copied turned off, saying why, so it never runs on the redacted prompt.

**A removed project** (§0.102): its chat's armed timers, jobs and wakes, are
cancelled by XO with the reason; their history stays. A job whose run finds
its chat or folder gone is turned off at that run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from services.cowork_agent.storm.chats import ChatRefused, Chats, check_pin, envelope, rows
from services.cowork_agent.storm.engine import EngineUnavailable, Wake, Writes
from services.cowork_agent.storm.jobs.runs import JobRefused, open_run, project_chat
from services.cowork_agent.storm.records import FOUNDER, ledger, timers
from services.cowork_agent.storm.records.timers import Timer
from services.cowork_agent.storm.redact import redact
from services.cowork_agent.storm.timers import TimerLoop
from services.errors import ServiceError
from services.timestamps import iso
from utils.commands import scheduler

log = logging.getLogger(__name__)

#: What a run's record says when opening its thread failed unexpectedly (the trace is in the log).
UNEXPECTED = "The server could not open this run's thread; its log says why."
#: A run's outcomes that mean its job's chat will never run it (its project's folder, removed
#: while storm wasn't running to retire the chat, included): the job is turned off.
CHAT_GONE = ("chat_retired", "no_such_chat", "project_missing")
#: Why XO turned off the jobs of a removed project.
PROJECT_REMOVED = "Its project was removed, so its chat is read-only and runs nothing."
#: What the agent that asked for a job hears when it can't be registered.
NOT_REGISTERED = ("The owner allowed the job “{title}” (review item {item}), but it couldn't be scheduled: {why}. "
                  "Nothing will run; tell the owner if it still matters.")
#: Why a job holding a secret is refused.
SECRET = ("A job's {field} is stored and shown, so keep secrets out of it: save the secret in the secrets store and "
          "name its entry instead.")
#: Why a job the person saved off is off.
SAVED_OFF = "Saved turned off."
#: What the scheduler says of an agent job once it moved, or of one that couldn't.
MOVED = "Moved to XO's timers (timer {id}); it runs from there."
NO_CHAT = "Its chat {chat} is gone, so it can't run."
#: Why a scheduler job holding a secret moved turned off.
SECRET_KEPT_OUT = ("Its title or prompt holds a secret, which XO's store keeps out; save it again naming the "
                   "secrets-store entry instead.")
#: What a run record from the scheduler keeps as a timer's last fire.
_RUN_KEYS = ("status", "trigger", "chat_id", "thread_ts", "already", "code", "reason")
#: The allowed ``create_job`` items whose job isn't registered yet.
_UNREGISTERED = ("SELECT id FROM review_items WHERE kind = 'create_job' AND state = 'decided' "
                 "AND json_extract(decision, '$.decision') = 'allow' AND json_extract(decision, '$.job_id') IS NULL "
                 "AND json_extract(decision, '$.job_error') IS NULL ORDER BY seq")

_jobs: "Jobs | None" = None


def job_view(timer: Timer) -> dict[str, Any]:
    """A job as the person's route shows it, and as a run reads it: the
    scheduler's agent job shape, with its timer's state."""
    definition = timer.definition
    armed = timer.state == "armed"
    return {"id": timer.id, "kind": scheduler.KIND_AGENT, "name": definition["name"],
            "description": definition.get("description"), "chat": timer.chat_id, "prompt": timer.note,
            "agent": definition.get("agent"), "model": definition.get("model"), "effort": definition.get("effort"),
            "every_seconds": timer.every_s, "first_run_at": definition.get("first_run_at"),
            "utc_offset": definition.get("utc_offset") or "+00:00", "enabled": armed, "state": timer.state,
            "state_reason": timer.state_reason, "created_by": created_by(timer),
            "approved_by": None if timer.approved_by == FOUNDER else {"review_id": timer.approved_by},
            "next_run": timer.due_at if armed else None, "last_result": timer.last}


def created_by(timer: Timer) -> dict[str, Any]:
    return {"kind": "person"} if timer.asked_by == FOUNDER else {"kind": "agent", "session_id": timer.asked_by}


def _definition(job: Mapping[str, Any]) -> dict[str, Any]:
    """What a job's timer keeps of its definition besides its prompt and schedule."""
    return {key: job.get(key) for key in ("name", "description", "agent", "model", "effort", "first_run_at",
                                          "utc_offset")}


def arm_job(w: Writes, definition: Mapping[str, Any], *, now: datetime, asked_by: str, approved_by: str, by: str,
            request_key: str) -> Timer:
    """A job's timer from its checked definition
    (``scheduler.validate_definition``), first due on its grid from ``now``,
    on or off as the definition says."""
    armed = definition.get("enabled", True)
    made, _new = timers.arm(w, **_schedule(definition, now), chat_id=definition["chat"], role="thread",
                            note=definition["prompt"], job=_definition(definition), asked_by=asked_by,
                            approved_by=approved_by, by=by, request_key=request_key,
                            state="armed" if armed else "cancelled", state_reason=None if armed else SAVED_OFF)
    return made


def _schedule(job: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """A definition's timer kind, first due time and interval, on the
    scheduler's grid (``first_slot``)."""
    first = scheduler.first_slot(job, now)
    return {"kind": "every" if job["every_seconds"] else "at", "due_at": iso(first) if first else None,
            "every_s": job["every_seconds"]}


class Jobs:
    def __init__(self, chats: Chats) -> None:
        self.chats = chats
        self.timers = TimerLoop(chats, self.run_fire)
        self._retired = self.chat_retired   # one bound method, so the chats are cleared of exactly this one
        self._starting: asyncio.Task | None = None

    @property
    def engine(self):
        return self.chats.engine

    def start(self) -> None:
        self.chats.on_retired(self._retired)
        self._starting = asyncio.ensure_future(self._start())
        self._starting.set_name("storm-jobs:start")

    async def _start(self) -> None:
        for step, work in (("moving the scheduler's agent jobs", self.adopt), ("registering allowed jobs",
                                                                                self.recover)):
            try:
                await work()
            except Exception:  # noqa: BLE001 - the timers run whatever a step at start left; the next start retries
                log.exception("storm jobs: %s failed; the next start tries again", step)
        await self.timers.start()

    async def running(self) -> None:
        """Wait until the start's work is done and the timer loop runs."""
        await asyncio.shield(self._starting)

    async def stop(self) -> None:
        """No timer goes off from now on; a run still opening is cancelled,
        and the next start opens it."""
        self.chats.off_retired(self._retired)
        if self._starting is not None and not self._starting.done():
            self._starting.cancel()
            await asyncio.gather(self._starting, return_exceptions=True)
        await self.timers.stop()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.engine.clock.time(), timezone.utc).replace(microsecond=0)

    # ── runs ─────────────────────────────────────────────────────────────

    async def run_fire(self, timer: Timer, at: str, trigger: str = "schedule") -> dict[str, Any]:
        """Open the run of ``timer``'s fire for the slot ``at`` (the timer
        loop's job runner), or find the one already open; what came of it."""
        try:
            return await open_run(self.chats, job_view(timer), at, trigger)
        except ServiceError as exc:
            log.warning("storm jobs: %s's fire at %s opened no thread (%s): %s", timer.id, at, exc.code, exc.message)
            if exc.code in CHAT_GONE:
                await self._turn_off(timer.id, exc.message)
            return {"status": "failed", "code": exc.code, "reason": exc.message}
        except Exception:  # noqa: BLE001 - the record says it failed; the trace goes to the log
            log.exception("storm jobs: %s's fire at %s failed", timer.id, at)
            return {"status": "failed", "code": "server_error", "reason": UNEXPECTED}

    async def chat_retired(self, chat_id: str) -> None:
        """A removed project's armed timers, its jobs among them, are
        cancelled with its chat (§0.102), by XO with the reason."""
        def off(w: Writes) -> None:
            for timer in timers.armed_in(w, chat_id):
                timers.end(w, timer, state="cancelled", reason=PROJECT_REMOVED, actor=ledger.XO)
        await self.engine.write(off)

    async def _turn_off(self, timer_id: str, reason: str) -> None:
        def off(w: Writes) -> None:
            if (timer := timers.get(w, timer_id)) is not None:
                timers.end(w, timer, state="cancelled", reason=reason, actor=ledger.XO)
        await self.engine.write(off)
        log.info("storm jobs: %s turned off: %s", timer_id, reason)

    # ── making jobs ──────────────────────────────────────────────────────

    async def save(self, body: Any, job_id: str | None = None) -> dict[str, Any]:
        """The person's own agent job (``POST``/``PUT /api/schedules``): its
        project chat, agent, model and effort are checked now, so a mistake
        is told at once rather than at the first run. A new definition keeps
        the job's next time unless its schedule changed or it is turned on."""
        try:
            definition = scheduler.validate_definition(body)
        except ValueError as exc:
            raise JobRefused("invalid_job", str(exc)) from exc
        for field in ("name", "prompt"):
            if redact(definition[field]) != definition[field]:
                raise JobRefused("secret_in_job", SECRET.format(field=field))
        chat = await project_chat(self.chats, definition["chat"])
        _agent, runtime = await self.chats.runtime_for(definition["agent"], "run this job")
        await check_pin(runtime, {}, {"model": definition["model"], "effort": definition["effort"]})
        now = self._now()

        def write(w: Writes) -> dict[str, Any]:
            Chats.writable(rows.get_chat(w, chat["id"]))        # read again: removed meanwhile, it runs nothing
            if job_id is None:
                return job_view(arm_job(w, definition, now=now, asked_by=FOUNDER, approved_by=FOUNDER, by=FOUNDER,
                                        request_key=ledger.request_key("job", uuid.uuid4().hex)))
            current = timers.get(w, job_id)
            if current is None or current.job is None:
                raise JobRefused("job_not_found", f"There is no job {job_id!r}.", status=404)
            same = ((definition["every_seconds"], definition["first_run_at"])
                    == (current.every_s, current.definition.get("first_run_at")))
            keep = same and current.state == "armed"
            schedule = ({"kind": current.kind, "due_at": current.due_at, "every_s": current.every_s} if keep
                        else _schedule(definition, now))
            # The person's own words now, unless the prompt is the one they allowed.
            approved = current.approved_by if definition["prompt"] == current.note else FOUNDER
            return job_view(timers.redefine(w, current, chat_id=chat["id"], **schedule, note=definition["prompt"],
                                            job=_definition(definition), approved_by=approved,
                                            armed=definition["enabled"], reason=SAVED_OFF, actor=FOUNDER))
        return await self.engine.write(write)

    async def register(self, item: Mapping[str, Any]) -> None:
        """The person allowed ``item`` (a ``create_job``): register its job,
        once, in one transaction with the item's record of it."""
        payload = item["detail"]["job"]
        try:
            definition = scheduler.validate_definition(payload)
            await project_chat(self.chats, payload["chat"])
        except (JobRefused, ChatRefused, ValueError) as exc:
            why = getattr(exc, "message", None) or str(exc)
            log.warning("storm jobs: the job item %s allowed can't be registered: %s", item["id"], why)
            await self.engine.write(lambda w: self._settle(w, item, "job_error", why))
            return
        now = self._now()

        def write(w: Writes) -> None:
            chat = rows.get_chat(w, payload["chat"])
            if chat is None or chat.get("retired_at"):           # removed meanwhile
                self._settle(w, item, "job_error", PROJECT_REMOVED)
                return
            made = arm_job(w, definition, now=now, asked_by=item["session_id"], approved_by=item["id"], by=FOUNDER,
                           request_key=ledger.request_key("create_job", item["id"]))
            self._settle(w, item, "job_id", made.id)
        await self.engine.write(write)

    def _settle(self, w: Writes, item: Mapping[str, Any], key: str, value: str) -> None:
        """Record on ``item`` the job it made (``job_id``, with a
        ``job.registered`` event) or why it made none (``job_error``, and
        the agent is told), once."""
        if not w.execute("UPDATE review_items SET decision = json_set(decision, ?, ?) WHERE id = ? "
                         "AND json_extract(decision, '$.job_id') IS NULL "
                         "AND json_extract(decision, '$.job_error') IS NULL",
                         (f"$.{key}", value, item["id"])).rowcount:
            return
        if key == "job_id":
            w.append("job.registered", {"review_id": item["id"], key: value}, chat_id=item["chat_id"],
                     session_id=item["session_id"], thread_ts=item["thread_ts"])
        else:
            self._tell(w, item, value)

    @staticmethod
    def _tell(w: Writes, item: Mapping[str, Any], why: str) -> None:
        """Record that ``item``'s job wasn't registered (``job.refused``) and
        tell the agent that asked, which heard "allowed", unless its session
        was retired (it takes no more input)."""
        w.append("job.refused", {"review_id": item["id"], "job_error": why}, chat_id=item["chat_id"],
                 session_id=item["session_id"], thread_ts=item["thread_ts"])
        live = w.execute("SELECT 1 FROM sessions WHERE id = ? AND retired_at IS NULL", (item["session_id"],)).fetchone()
        if live is None:
            return
        note = NOT_REGISTERED.format(title=item["detail"]["job"]["name"], item=item["id"], why=why.rstrip("."))
        w.wake(item["session_id"], Wake(id=f"job:{item['id']}:refused", priority="next", reason="relay",
                                        text=envelope.relay(sender_role="xo", sender_session=None, note=note,
                                                            reason="job", now=w.now)))

    async def recover(self) -> None:
        """At start: every job the person allowed that isn't registered yet."""
        for (item_id,) in await self.engine.store.read(lambda conn: conn.execute(_UNREGISTERED).fetchall()):
            await self.register(await self.chats.queue.get_item(item_id))

    # ── from the scheduler ───────────────────────────────────────────────

    async def adopt(self) -> None:
        """The scheduler's agent jobs move to timers (the module's docstring
        says how); one that can't move now is tried again at the next start."""
        try:
            found = [job for job in await asyncio.to_thread(scheduler.list_jobs) if scheduler.is_agent(job)]
        except scheduler.SchedulerError as exc:
            log.error("storm jobs: the scheduler's files can't be read (%s); its agent jobs move to XO's timers at "
                      "the next start", exc)
            return
        for job in found:
            try:
                await self._adopt(job)
            except (ServiceError, scheduler.SchedulerError) as exc:
                log.error("storm jobs: the scheduler's agent job %s didn't move to XO's timers (%s); the next start "
                          "tries again", job["id"], exc)

    async def _adopt(self, job: Mapping[str, Any]) -> None:
        now = self._now()
        timer, runs = await self.engine.write(lambda w: self._copy(w, job, now))
        fire = job.get("fire")
        if timer is not None and runs and fire:
            outcome = await self.run_fire(timer, fire["at"], fire["trigger"])
            await self.engine.write(lambda w: timers.fire(w, timers.get(w, timer.id), at=fire["at"], outcome=outcome,
                                                          next_due=None, moves=False))
        reason = MOVED.format(id=timer.id) if timer is not None else NO_CHAT.format(chat=job["chat"])
        await asyncio.to_thread(scheduler.hand_over_agent_job, job["id"], by={"kind": "xo", "reason": reason})
        if timer is not None:
            log.info("storm jobs: the scheduler's agent job %s is timer %s now (%s)", job["id"], timer.id, timer.state)

    @staticmethod
    def _copy(w: Writes, job: Mapping[str, Any], now: datetime) -> tuple[Timer | None, bool]:
        """The scheduler's agent job as a timer, copied once (by its id), and
        whether its runs may go on: None when its chat isn't in this store."""
        key = ledger.request_key("scheduler_job", job["id"])
        if (found := timers.by_key(w, key)) is not None:
            return found, found.state_reason != SECRET_KEPT_OUT
        if rows.get_chat(w, job["chat"]) is None:
            return None, False
        secret = any(redact(job[field]) != job[field] for field in ("name", "prompt"))
        every = job.get("every_seconds")
        ran = not every and bool(job.get("first_run_at")) and not job.get("next_run") and bool(
            job.get("last_run") or job.get("fire"))          # a one-time job that ran, or whose one fire is out
        if every:
            due = job.get("next_run") or iso(scheduler.first_slot(job, now))
        else:
            due = job.get("next_run") or job.get("first_run_at")
        if secret:
            state, reason = "cancelled", SECRET_KEPT_OUT
        elif not job.get("enabled", True):
            state, reason = "cancelled", _turned_off(job.get("disabled_by") or {})
        else:
            state, reason = ("fired" if ran else "armed"), None
        maker = job.get("created_by") or {"kind": "person"}
        last = job.get("last_result")
        timer, _new = timers.arm(
            w, kind="every" if every else "at", chat_id=job["chat"], role="thread", note=job["prompt"],
            job=_definition(job), due_at=due, every_s=every,
            asked_by=maker["session_id"] if maker.get("kind") == "agent" else FOUNDER,
            approved_by=(job.get("approved_by") or {}).get("review_id") or FOUNDER, by=ledger.XO, request_key=key,
            state=state, state_reason=reason, timer_id=job["id"],
            fired={"at": last.get("fire_at") or last.get("started_at"), **{k: last[k] for k in _RUN_KEYS if k in last}}
            if last else None)
        return timer, not secret


def _turned_off(by: Mapping[str, Any]) -> str:
    who = {"person": "the person", "xo": "XO"}.get(by.get("kind"), f"session {by.get('session_id')}")
    return f"Turned off in the scheduler by {who}" + (f": {by['reason']}" if by.get("reason") else ".")


def boot(chats: Chats) -> Jobs:
    """At storm's boot, once its chats run: the scheduler's agent jobs move
    to timers, allowed jobs not yet registered are, and the timer loop runs."""
    global _jobs
    _jobs = Jobs(chats)
    _jobs.start()
    return _jobs


async def shutdown() -> None:
    global _jobs
    jobs, _jobs = _jobs, None
    if jobs is not None:
        await jobs.stop()


def current() -> Jobs:
    if _jobs is None:
        raise EngineUnavailable("engine_not_running", "Storm's jobs aren't running in this server (they start with "
                                                      "the engine); retry shortly.")
    return _jobs
