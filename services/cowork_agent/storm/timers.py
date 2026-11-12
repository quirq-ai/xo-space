"""The timer loop: every armed timer goes off at its time or on its event,
and a lease ends with its holder's process or its time (record design v2
§4, §5 rules 5 and 9; w5-w7-design W6).

One task, woken by the engine's clock at the next ``due_at`` or lease expiry
(never later than ``MAX_SLEEP_S``, so a wall clock that jumped, as when the
machine slept, is caught up) and by every commit that changes what it
waits for; nothing polls. It runs on the engine: its clock, its store, its
commits, and ``Writes.wake``, the one gate every wake takes. It sits above
the engine because a wake's text is an envelope and a job's fire opens a
thread, both the chats' work; ``storm/jobs`` starts it.

* **A wake** (``wake_me``) goes to its role's session: the thread's, or the
  chat's coordinator (started if the chat has none, as for a message), in
  one transaction with the timer's own change. Its id is
  ``timer:<id>:<slot>``, so it goes off once. ``Writes.wake`` takes a
  replaced coordinator's to its successor, refuses one into a scope the
  founder stopped and lets a pause hold it; the timer records which.
* **A job** opens its run's thread through ``storm/jobs`` (one thread per
  job and slot, however often it is handed over), off the loop, and then
  records what came of it; the loop doesn't fire it again meanwhile.
* **An ``every``** moves on to the slot after the latest one due
  (``timers.slot``): no drift, and the slots missed while the server was
  down give one fire.
* **Events** fire ``on`` timers: a lease ended (its ``lease`` event), a
  task ended (``task``), an account's pool reopening (``pool``: from a hold
  to admitting work), and a process exiting. Each armed ``process_exited``
  timer's process and each live lease's holder is watched with a pidfd;
  a holder's exit releases its leases, which fires ``lease_freed`` in turn.
* **Restarts**: the first pass fires what came due while the server was
  down, once each, and finds the watched processes that exited meanwhile;
  ``on`` timers whose task ended or whose lease ended meanwhile fire at start.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Coroutine, Mapping, Sequence

from services.cowork_agent.storm.chats import ChatRefused, envelope, rows
from services.cowork_agent.storm.engine import IntentRefused, Wake, Writes
from services.cowork_agent.storm.engine.admission import HOLDS
from services.cowork_agent.storm.engine.model import held_by_founder
from services.cowork_agent.storm.engine.ports import Timer as Handle
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import leases, ledger, tasks, timers
from services.cowork_agent.storm.records.timers import Timer
from services.cowork_agent.storm.store import StoredEvent
from services.timestamps import parse_ts
from utils.commands import process_start_ticks

log = logging.getLogger(__name__)

#: The longest the loop sleeps without looking again.
MAX_SLEEP_S = 300.0
#: How long it waits after a pass failed.
RETRY_S = 10.0
#: What a wake for a thread that has no session records.
NO_SESSION = "its thread has no session"
NO_LIVE = "no live session takes it"

#: Opens a job timer's run for a slot and says what came of it (``storm/jobs``).
JobRunner = Callable[[Timer, str], Awaitable[Mapping[str, Any]]]
#: A process by its pid and start ticks.
Process = tuple[int, int]


class TimerLoop:
    def __init__(self, chats: Any, run_job: JobRunner) -> None:
        self.chats = chats
        self._run_job = run_job
        self._kicked = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sleep: Handle | None = None
        self._opening: dict[str, asyncio.Task] = {}      # job timers whose fire is opening its thread
        self._tasks: set[asyncio.Task] = set()            # event fires
        self._watched: dict[Process, int] = {}            # → its pidfd
        self._exiting: set[Process] = set()               # exits being recorded

    @property
    def engine(self):
        return self.chats.engine

    async def start(self) -> None:
        self.engine.subscribe(self)
        await self._catch_up()
        self._task = asyncio.create_task(self._loop(), name="storm-timers")
        self.kick()

    async def stop(self) -> None:
        """A job's fire still opening is cancelled; it is still due, so the
        next start opens it (or finds the thread it opened)."""
        self.engine.unsubscribe(self)
        if self._sleep is not None:
            self._sleep.cancel()
        running = [t for t in (self._task, *self._opening.values(), *self._tasks) if t is not None]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        for process in list(self._watched):
            self._unwatch(process)

    def kick(self) -> None:
        self._kicked.set()

    # ── the engine's commits ─────────────────────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            data = event.data
            if event.type == "timer" or (event.type == "lease" and data.get("action") == "acquire"):
                self.kick()             # a time or a holder to watch, or one no longer
            elif event.type == "lease" and data.get("action") == "release":
                self.kick()
                self._on(timers.on("lease_freed", data["resource"]),
                         f"the lease on {data['resource']} ended ({data['why']})")
            elif event.type == "task" and data.get("action") == "move" and data.get("state") in tasks.ENDED:
                self._on(timers.on("task_done", data["id"]), f"task {data['id']} ended: {data['state']}")
            elif event.type == "pool" and data.get("was") in HOLDS and data.get("state") not in HOLDS:
                self._on(timers.on("pool_reopened", data["pool"]), f"the usage limit on {data['pool']} lifted")

    def ephemeral(self, event: Any, epoch: int) -> None:
        pass

    # ── the loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            await self._kicked.wait()
            self._kicked.clear()
            try:
                await self._pass()
            except Exception:  # noqa: BLE001 - the loop outlives one failed pass (the store, a bug): it looks again
                log.exception("storm timers: a pass failed; looking again in %s s", RETRY_S)
                self._sleep_for(RETRY_S)

    async def _pass(self) -> None:
        """Fire what is due, end the leases whose time is up, watch the
        processes something waits on, and sleep until the next time."""
        now = self.engine.clock.time()
        stamp = iso(now)
        due, ended = await self.engine.store.read(lambda conn: (timers.due(conn, stamp), leases.expired(conn, stamp)))
        for timer in due:
            if timer.job is not None:
                if timer.id not in self._opening:
                    self._open(timer, now)
                continue
            at, next_due = timers.slot(timer, now)
            await self._go_off(timer, at=at, next_due=next_due, cause=f"its time came ({at})")
        for lease in ended:
            await self.engine.write(lambda w, lease=lease: _release(w, lease.id, "expired"))

        def upcoming(conn) -> tuple[list[str], set[Process]]:
            times = [timers.next_due(conn, besides=list(self._opening)), leases.next_expiry(conn)]
            return [t for t in times if t], _processes(conn)
        times, processes = await self.engine.store.read(upcoming)
        self._watch(processes)
        self._sleep_for(min((parse_ts(t).timestamp() for t in times), default=now + MAX_SLEEP_S) - now)

    def _sleep_for(self, delay: float) -> None:
        if self._sleep is not None:
            self._sleep.cancel()
        self._sleep = None
        if delay <= 0:
            self.kick()
        else:
            self._sleep = self.engine.clock.call_later(min(delay, MAX_SLEEP_S), self.kick)

    async def _catch_up(self) -> None:
        """At start: the ``on`` timers whose task or lease ended while the
        server was down go off, once."""
        def read(conn) -> list[tuple[Timer, str]]:
            gone = []
            for timer in timers.waiting(conn):
                event, _, about = timer.event.partition(":")
                if event == "task_done" and (task := tasks.get(conn, about)) is not None and task.state in tasks.ENDED:
                    gone.append((timer, f"task {about} ended: {task.state}"))
                elif event == "lease_freed" and leases.held(conn, about) is None:
                    gone.append((timer, f"the lease on {about} ended"))
            return gone
        for timer, cause in await self.engine.store.read(read):
            await self._go_off(timer, at=iso(self.engine.clock.time()), next_due=None, cause=cause)

    # ── going off ────────────────────────────────────────────────────────

    async def _go_off(self, timer: Timer, *, at: str, next_due: str | None, cause: str) -> None:
        """One wake, in one transaction with its timer's change. A timer
        changed since it was read (cancelled, gone off on another event)
        does nothing."""
        def fire(w: Writes, session_id: str | None) -> None:
            current = timers.get(w, timer.id)
            if current is not None and current.state == "armed" and current.due_at == timer.due_at:
                timers.fire(w, current, at=at, outcome=self._wake(w, current, session_id, at, cause),
                            next_due=next_due)
        try:
            if timer.role == "thread":
                await self.engine.write(lambda w: fire(w, w.execute(
                    "SELECT session_id FROM threads WHERE chat_id = ? AND ts = ?",
                    (timer.chat_id, timer.thread_ts)).fetchone()[0]))
            else:
                await self.chats.with_coordinator(await self.chats.chat(timer.chat_id), fire)
        except (IntentRefused, ChatRefused) as exc:     # no coordinator may start there
            def refused(w: Writes) -> None:
                current = timers.get(w, timer.id)
                if current is not None and current.state == "armed" and current.due_at == timer.due_at:
                    timers.fire(w, current, at=at, outcome={"status": "refused", "reason": exc.message},
                                next_due=next_due)
            await self.engine.write(refused)

    @staticmethod
    def _wake(w: Writes, timer: Timer, session_id: str | None, at: str, cause: str) -> dict[str, Any]:
        """Queue the timer's wake through the one gate; what came of it."""
        if session_id is None:
            return {"status": "refused", "reason": NO_SESSION}
        text = envelope.timer(chat=rows.get_chat(w, timer.chat_id), thread_ts=timer.thread_ts, timer_id=timer.id,
                              set_by=timer.asked_by, set_at=timer.created_at, cause=cause, note=timer.note, now=w.now)
        if w.wake(session_id, Wake(id=f"timer:{timer.id}:{at}", text=text, reason="timer")):
            return {"status": "woke"}
        return {"status": "refused", "reason": held_by_founder(w.refused) if w.refused is not None else NO_LIVE}

    def _open(self, timer: Timer, now: float) -> None:
        at, next_due = timers.slot(timer, now)

        async def run() -> None:
            outcome = await self._run_job(timer, at)

            def record(w: Writes) -> None:
                current = timers.get(w, timer.id)
                timers.fire(w, current, at=at, outcome=outcome, next_due=next_due,
                            moves=current.due_at == timer.due_at)
            await self.engine.write(record)
        task = asyncio.create_task(run(), name=f"storm-timers:{timer.id}")
        self._opening[timer.id] = task
        task.add_done_callback(lambda done: self._opened(timer.id, done))

    def _opened(self, timer_id: str, task: asyncio.Task) -> None:
        self._opening.pop(timer_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm timers: job %s's fire failed; it is tried again", timer_id, exc_info=task.exception())
        self.kick()

    def _on(self, event: str, cause: str) -> None:
        self._spawn(self._fire_on(event, cause), f"storm-timers:{event}")

    async def _fire_on(self, event: str, cause: str) -> None:
        for timer in await self.engine.store.read(lambda conn: timers.waiting(conn, event)):
            await self._go_off(timer, at=iso(self.engine.clock.time()), next_due=None, cause=cause)

    # ── processes ────────────────────────────────────────────────────────

    def _watch(self, wanted: set[Process]) -> None:
        """Watch exactly the processes something waits on, each through a
        pidfd, which the loop reads when the process exits; one already gone
        (its pid freed, or taken by another process) has exited."""
        for process in set(self._watched) - wanted:
            self._unwatch(process)
        loop = asyncio.get_running_loop()
        for process in wanted - set(self._watched) - self._exiting:
            pid, ticks = process
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                self._exited(process)
                continue
            if process_start_ticks(pid) != ticks:
                os.close(fd)
                self._exited(process)
                continue
            self._watched[process] = fd
            loop.add_reader(fd, self._exited, process)

    def _unwatch(self, process: Process) -> None:
        fd = self._watched.pop(process, None)
        if fd is not None:
            asyncio.get_running_loop().remove_reader(fd)
            os.close(fd)

    def _exited(self, process: Process) -> None:
        self._unwatch(process)
        if process not in self._exiting:
            self._exiting.add(process)
            self._spawn(self._process_ended(process), f"storm-timers:exit:{process[0]}")

    async def _process_ended(self, process: Process) -> None:
        """The process's leases end, then the timers waiting for it go off."""
        pid, ticks = process
        try:
            await self.engine.write(lambda w: [_release(w, lease.id, "process_exited")
                                               for lease in leases.of_process(w, pid, ticks)])
            await self._fire_on(timers.on("process_exited", f"{pid}:{ticks}"), f"process {pid} exited")
        finally:
            self._exiting.discard(process)

    def _spawn(self, work: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm timers: %s failed", task.get_name(), exc_info=task.exception())


def _release(w: Writes, lease_id: str, why: str) -> None:
    lease = leases.get(w, lease_id)
    if lease is not None:
        leases.release(w, lease, why=why, actor=ledger.XO)


def _processes(conn) -> set[Process]:
    """The processes something waits on: each live lease's holder, and each
    armed ``process_exited`` timer's process."""
    wanted = {(lease.holder_pid, lease.holder_start_ticks) for lease in leases.live(conn)
              if lease.holder_pid is not None}
    for timer in timers.waiting(conn):
        event, _, about = timer.event.partition(":")
        if event == "process_exited":
            pid, _, ticks = about.partition(":")
            wanted.add((int(pid), int(ticks)))
    return wanted
