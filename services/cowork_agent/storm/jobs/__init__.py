"""Jobs: agent sessions started by a schedule (design ch 10 §4.5, ch 11
§14.6, ch 12 §5.8, ch 17 §2.8; §0.44, §0.51, §0.107).

The scheduler (``utils/commands/scheduler.py``, ``/api/schedules``) owns the
definitions, the clock and the run history; storm owns what a run is. At
storm's boot ``Jobs`` registers itself as the scheduler's agent runner: the
watcher's tick hands each fire over from its own thread, ``fire`` puts it on
storm's event loop and returns at once, and ``runs.open_run`` opens its
thread there. The run record says which thread, or why none opened.

**Who makes a job.** The person, through ``POST /api/schedules {kind:
"agent"}`` (``save``: localhost-only, no review item). An agent, only by
asking: ``create_job`` opens a ``create_job`` review item with the exact
title, schedule and prompt, and nothing exists until the person allows it.
Allow registers the job (``register``) as approved by the person with the
item's id; Send back registers nothing (the agent gets the note as a wake,
like every ask).

**Registering is outside the store's transaction**, after the decision
commits, so it is made crash-safe: the scheduler registers an item's job at
most once (it finds the job an item already made), and the item records the
job it made (``decision.job_id``, with a ``job.registered`` event). At boot,
every allowed item without one is registered (``recover``): a decision
committed but not registered when the server stopped is registered then,
never twice. A job the scheduler refuses (its project was removed meanwhile)
records ``decision.job_error`` instead, so it isn't retried forever.

**A project removed while its job is being saved.** The chat is checked,
then the job is written to the scheduler's files, outside the store: a
removal in between ran its listener before the job existed. The chat is read
again once the job is written, and the job turned off if it was retired.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import Future
from typing import Any, Mapping

from services.cowork_agent.storm.chats import ChatRefused, Chats, check_pin
from services.cowork_agent.storm.engine import EngineUnavailable, Writes
from services.cowork_agent.storm.jobs.runs import JobRefused, open_run, project_chat
from services.errors import ServiceError
from utils.commands import scheduler

log = logging.getLogger(__name__)

#: What a run's record says when opening its thread failed unexpectedly (the trace is in the log).
UNEXPECTED = "The server could not open this run's thread; its log says why."
#: A run's outcomes that mean its job's chat will never run it (its project's folder, removed
#: while storm wasn't running to retire the chat, included): the job is turned off.
CHAT_GONE = ("chat_retired", "no_such_chat", "project_missing")
#: Why XO turned off the jobs of a removed project.
PROJECT_REMOVED = "Its project was removed, so its chat is read-only and runs nothing."
#: The allowed ``create_job`` items whose job isn't registered yet.
_UNREGISTERED = ("SELECT id FROM review_items WHERE kind = 'create_job' AND state = 'decided' "
                 "AND json_extract(decision, '$.decision') = 'allow' AND json_extract(decision, '$.job_id') IS NULL "
                 "AND json_extract(decision, '$.job_error') IS NULL ORDER BY seq")

_jobs: "Jobs | None" = None


class Jobs:
    def __init__(self, chats: Chats, loop: asyncio.AbstractEventLoop) -> None:
        self.chats = chats
        self._loop = loop
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False
        self._runner = self.fire            # one bound method, so the scheduler is cleared of exactly this one
        self._retired = self.chat_retired   # likewise for the chats' retire listeners

    @property
    def engine(self):
        return self.chats.engine

    def start(self) -> None:
        scheduler.set_agent_runner(self._runner)
        self.chats.on_retired(self._retired)
        self._own(asyncio.ensure_future(self.recover()), "storm-jobs:recover")

    async def stop(self) -> None:
        """No new fire is taken; a fire still opening is cancelled, so the
        scheduler hands it over again after the restart."""
        self._stopped = True
        scheduler.clear_agent_runner(self._runner)
        self.chats.off_retired(self._retired)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── runs ─────────────────────────────────────────────────────────────

    def fire(self, job: Mapping[str, Any], fire_at: str, trigger: str) -> Future:
        """The scheduler's agent runner, called from any thread: the fire is
        opened on storm's loop; this returns without waiting."""
        return asyncio.run_coroutine_threadsafe(self._open(dict(job), fire_at, trigger), self._loop)

    async def _open(self, job: Mapping[str, Any], fire_at: str, trigger: str) -> dict[str, Any]:
        if self._stopped:
            raise asyncio.CancelledError    # handed over after stop: the scheduler keeps it for the next start
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await open_run(self.chats, job, fire_at, trigger)
        except ServiceError as exc:
            log.warning("storm jobs: %s's fire at %s opened no thread (%s): %s", job.get("id"), fire_at, exc.code,
                        exc.message)
            if exc.code in CHAT_GONE:
                await self._turn_off(job["id"], exc.message)
            return {"status": "failed", "code": exc.code, "reason": exc.message}
        except Exception:  # noqa: BLE001 - the record says it failed; the trace goes to the log
            log.exception("storm jobs: %s's fire at %s failed", job.get("id"), fire_at)
            return {"status": "failed", "code": "server_error", "reason": UNEXPECTED}
        finally:
            self._tasks.discard(task)

    async def chat_retired(self, chat_id: str) -> None:
        """A removed project's jobs are turned off with its chat (§0.102),
        by XO with the reason; their history stays."""
        for job in await asyncio.to_thread(scheduler.list_jobs):
            if scheduler.is_agent(job) and job["chat"] == chat_id and job.get("enabled", True):
                await self._turn_off(job["id"], PROJECT_REMOVED)

    async def _turn_off(self, job_id: str, reason: str) -> None:
        try:
            await asyncio.to_thread(scheduler.disable_job, job_id, by={"kind": "xo", "reason": reason})
        except scheduler.UnknownJobError:
            return                          # deleted meanwhile: nothing left to turn off
        except scheduler.SchedulerError as exc:
            log.error("storm jobs: %s could not be turned off (%s); its runs keep failing until it is", job_id, exc)
            return
        log.info("storm jobs: %s turned off: %s", job_id, reason)

    # ── making jobs ──────────────────────────────────────────────────────

    async def save(self, body: Any, job_id: str | None = None) -> dict[str, Any]:
        """The person's own agent job (``POST``/``PUT /api/schedules``): its
        project chat, agent, model and effort are checked now, so a mistake
        is told at once rather than at the first run."""
        try:
            definition = scheduler.validate_definition(body)
        except ValueError as exc:
            raise JobRefused("invalid_job", str(exc)) from exc
        await project_chat(self.chats, definition["chat"])
        _agent, runtime = await self.chats.runtime_for(definition["agent"], "run this job")
        await check_pin(runtime, {}, {"model": definition["model"], "effort": definition["effort"]})
        call = scheduler.create_job if job_id is None else functools.partial(scheduler.update_job, job_id)
        try:
            saved = await asyncio.to_thread(call, body)
        except scheduler.UnknownJobError as exc:
            raise JobRefused("job_not_found", f"There is no job {job_id!r}.", status=404) from exc
        except ValueError as exc:
            raise JobRefused("invalid_job", str(exc)) from exc
        except scheduler.SchedulerError as exc:
            raise JobRefused("scheduler_failed", str(exc), status=500) from exc
        if await self._retired_meanwhile(saved["chat"]):
            return await asyncio.to_thread(scheduler.get_job, saved["id"])
        return saved

    async def register(self, item: Mapping[str, Any]) -> None:
        """The person allowed ``item`` (a ``create_job``): register its job,
        once, and record which job it made."""
        payload = item["detail"]["job"]
        role = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT role FROM sessions WHERE id = ?", (item["session_id"],)).fetchone())
        created_by = {"kind": "agent", "session_id": item["session_id"], "role": role[0] if role else None}
        try:
            await project_chat(self.chats, payload["chat"])
            job = await asyncio.to_thread(scheduler.create_job, payload, created_by=created_by,
                                          approved_by={"review_id": item["id"]})
        except (JobRefused, ChatRefused, ValueError) as exc:
            why = getattr(exc, "message", None) or str(exc)
            log.warning("storm jobs: the job item %s allowed can't be registered: %s", item["id"], why)
            await self._settle(item, "job_error", why)
            return
        await self._retired_meanwhile(payload["chat"])
        await self._settle(item, "job_id", job["id"])

    async def _settle(self, item: Mapping[str, Any], key: str, value: str) -> None:
        def fn(w: Writes) -> None:
            if w.execute("UPDATE review_items SET decision = json_set(decision, ?, ?) WHERE id = ? "
                         "AND json_extract(decision, '$.job_id') IS NULL "
                         "AND json_extract(decision, '$.job_error') IS NULL",
                         (f"$.{key}", value, item["id"])).rowcount:
                w.append("job.registered" if key == "job_id" else "job.refused", {"review_id": item["id"], key: value},
                         chat_id=item["chat_id"], session_id=item["session_id"], thread_ts=item["thread_ts"])
        await self.engine.write(fn)

    async def _retired_meanwhile(self, chat_id: str) -> bool:
        """After a job naming ``chat_id`` was written: if the chat was retired
        meanwhile, its jobs are turned off now, as its retirement would have."""
        if not (await self.chats.chat(chat_id)).get("retired_at"):
            return False
        await self.chat_retired(chat_id)
        return True

    async def recover(self) -> None:
        """At boot: every job the person allowed that isn't registered yet."""
        for (item_id,) in await self.engine.store.read(lambda conn: conn.execute(_UNREGISTERED).fetchall()):
            try:
                await self.register(await self.chats.queue.get_item(item_id))
            except scheduler.SchedulerError as exc:
                log.error("storm jobs: registering the job item %s allowed failed (%s); it is tried again at the "
                          "next start", item_id, exc)

    def _own(self, task: asyncio.Task, name: str) -> None:
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm jobs: %s failed", task.get_name(), exc_info=task.exception())


def boot(chats: Chats) -> Jobs:
    """At storm's boot, once its chats run: agent jobs run from now on, and
    the allowed ones not yet registered are."""
    global _jobs
    _jobs = Jobs(chats, asyncio.get_running_loop())
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
