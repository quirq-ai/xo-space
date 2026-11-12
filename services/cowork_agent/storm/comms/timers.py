"""The timer and lease tools, for threads and coordinators (w5-w7-design
W6; record design v2 §4, §5 rules 5, 6 and 9).

* ``wake_me`` arms a timer on the caller itself: ``at`` an ISO time, or
  ``on`` an event, with ``about`` naming what (a resource, a pid, a task;
  for ``pool_reopened``, the caller's own account), and the ``note`` its
  wake will say. It is its role's timer (the thread's, or the chat's
  coordinator's), so it survives restarts and a coordinator's replacement,
  and the owner sees it. A thread's timer belongs to its task while that is
  open (and to the task's goal), a coordinator's to the goal it names: it
  expires when that ends. An event that can't come any more (the task has
  ended, nobody holds the resource, no such process runs, the account
  admits work now) is refused, saying so. A retried call returns the same
  timer.
* ``cancel_wake`` cancels one of the caller's own armed timers (a job is
  turned off with ``cancel_job``).
* ``lease`` takes a named resource for the caller, held by its own CLI
  process or by the process it names (``pid``: a server it started), for
  ``seconds`` at most when given; anyone else's live lease refuses it with
  who holds it and since when. ``release`` gives it back.
* ``mine`` is what ``read_state`` adds: the caller's armed timers and live
  leases.

The agents' CLIs keep their own timers for a wait inside the session; these
are for waits that must outlive the process, or that the owner should see.
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import Caller, CommsError, ToolCall
from services.cowork_agent.storm.comms.tools import ID_MAX, SHORT_TEXT_MAX, TIME_MAX, WORD_MAX
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.admission import HOLDS
from services.cowork_agent.storm.engine.engine import pool_account, pool_agent
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import goals, leases, tasks, timers
from services.cowork_agent.storm.records.leases import Lease, LeaseHeld
from services.cowork_agent.storm.records.ledger import request_key
from services.cowork_agent.storm.records.timers import Timer
from services.timestamps import parse_ts
from utils.commands import process_start_ticks

#: What a refused lease adds: how to wait for it.
WAIT_FOR_IT = " wake_me with on lease_freed and about the resource wakes you when it ends."
CANCELLED = "cancelled with cancel_wake"


def _place(caller: Caller) -> tuple[str, str, str | None]:
    """Where the caller's timers wake: its chat, its role, and its thread's ts for a thread."""
    return caller.chat_id, caller.role, caller.thread_ts if caller.role == "thread" else None


def _open_task(w: Writes, caller: Caller) -> tasks.Task | None:
    """A thread's task while it is open: what its timers and leases belong to."""
    if caller.role != "thread":
        return None
    task = tasks.of_thread(w, caller.chat_id, caller.thread_ts)
    return task if task is not None and task.state not in tasks.ENDED else None


def _timer_view(timer: Timer) -> dict[str, Any]:
    return {"timer_id": timer.id, "kind": timer.kind, "at": timer.due_at, "on": timer.event, "note": timer.note,
            "state": timer.state, "task_id": timer.task_id, "goal_id": timer.goal_id, "set_at": timer.created_at,
            "set_by": timer.asked_by}


def _lease_view(lease: Lease) -> dict[str, Any]:
    return {"lease_id": lease.id, "resource": lease.resource, "since": lease.acquired_at, "until": lease.expires_at,
            "pid": lease.holder_pid, "task_id": lease.task_id, "released_at": lease.released_at,
            "released_why": lease.released_why}


def _whole(t: ToolCall, key: str) -> int | None:
    value = t.args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommsError("invalid_argument", f"{key} is a whole number, 1 or more.")
    return value


async def _event(t: ToolCall, event: str, about: str | None) -> str:
    """The ``on`` timer's event, checked where it needs no transaction (a
    process, the caller's account); the rest is checked in it."""
    if event == "pool_reopened":
        if about is not None:
            raise CommsError("invalid_argument", "pool_reopened waits for your own account: leave about out.")
        pool = await t.engine.store.read(lambda conn: conn.execute(
            "SELECT pool FROM sessions WHERE id = ?", (t.caller.session_id,)).fetchone()[0])
        if pool is None or t.engine.pool(pool_agent(pool), pool_account(pool)).state not in HOLDS:
            raise CommsError("not_waiting", "Your account's usage limit isn't holding work now: nothing to wait for.")
        return timers.on(event, pool)
    if about is None:
        raise CommsError("missing_argument", f"about is required for {event}: what it waits for.")
    if event == "process_exited":
        pid = int(about) if about.isdigit() else None
        ticks = await asyncio.to_thread(process_start_ticks, pid) if pid else None
        if ticks is None:
            raise CommsError("no_such_process", f"No process {about} is running: nothing to wait for.")
        return timers.on(event, f"{pid}:{ticks}")
    return timers.on(event, about)


def _still_to_come(w: Writes, caller: Caller, event: str) -> None:
    """Refuse an ``on`` timer whose task or lease event can't come any more."""
    kind, _, about = event.partition(":")
    if kind == "task_done":
        task = tasks.get(w, about)
        if task is None or task.chat_id != caller.chat_id:
            raise CommsError("no_such_task", f"There is no task {about!r} in this chat.")
        if task.state in tasks.ENDED:
            raise CommsError("not_waiting", f"Task {about} has already ended: it is {task.state}.")
    elif kind == "lease_freed" and leases.held(w, about) is None:
        raise CommsError("not_waiting", f"Nobody holds {about} now: lease it.")


async def wake_me(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    at = a.text(t.args, "at", required=False, limit=TIME_MAX)
    on = a.choice(t.args, "on", timers.EVENTS)
    about = a.text(t.args, "about", required=False, limit=WORD_MAX)
    note = a.text(t.args, "note", limit=SHORT_TEXT_MAX)
    goal_id = a.text(t.args, "goal_id", required=False, limit=ID_MAX)
    if (at is None) == (on is None):
        raise CommsError("invalid_argument", "Give either at (an ISO time) or on (an event).")
    if goal_id is not None and caller.role == "thread":
        raise CommsError("invalid_argument", "A thread's timer belongs to its task and that task's goal; goal_id is "
                                             "a coordinator's.")
    if at is not None:
        when = parse_ts(at)
        if when is None:
            raise CommsError("invalid_argument", "at is an ISO-8601 time with a UTC offset, such as "
                                                 "2026-11-12T18:00:00Z.")
        if when.timestamp() <= t.engine.clock.time():
            raise CommsError("invalid_argument", "at must be in the future.")
    event = await _event(t, on, about) if on is not None else None
    chat_id, role, thread_ts = _place(caller)
    key = request_key("wake_me", caller.session_id, at, event, note, goal_id)

    def fn(w: Writes) -> dict[str, Any]:
        if event is not None:
            _still_to_come(w, caller, event)
        task = _open_task(w, caller)
        goal = goals.get(w, goal_id) if goal_id else None
        if goal_id is not None and (goal is None or goal.chat_id != chat_id or goal.state in goals.FINAL):
            raise CommsError("no_such_goal", f"There is no live goal {goal_id!r} in this chat.")
        timer, _made = timers.arm(w, kind="at" if at is not None else "on", chat_id=chat_id, role=role,
                                  thread_ts=thread_ts, due_at=at, event=event, note=note,
                                  goal_id=goal_id or (task.goal_id if task else None),
                                  task_id=task.id if task else None, asked_by=caller.session_id,
                                  by=caller.session_id, request_key=key)
        return t.record(w, _timer_view(timer), timer_id=timer.id)
    return await t.engine.write(fn)


async def cancel_wake(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    timer_id = a.text(t.args, "timer_id", limit=ID_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        timer = timers.get(w, timer_id)
        if timer is None or timer.job is not None or (timer.chat_id, timer.role, timer.thread_ts) != _place(caller):
            raise CommsError("no_such_timer", f"There is no timer {timer_id!r} of yours; read_state lists them.")
        if timer.state != "armed":
            raise CommsError("timer_over", f"Timer {timer_id} is already {timer.state}.")
        ended = timers.end(w, timer, state="cancelled", reason=CANCELLED, actor=caller.session_id)
        return t.record(w, _timer_view(ended), timer_id=timer_id)
    return await t.engine.write(fn)


async def lease(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    resource = a.text(t.args, "resource", limit=WORD_MAX)
    seconds = _whole(t, "seconds")
    pid = _whole(t, "pid")
    ticks = await asyncio.to_thread(process_start_ticks, pid) if pid is not None else None
    if pid is not None and ticks is None:
        raise CommsError("no_such_process", f"No process {pid} is running.")
    expires_at = iso(t.engine.clock.time() + seconds) if seconds else None

    def fn(w: Writes) -> dict[str, Any]:
        holder = (pid, ticks) if pid is not None else w.execute(
            "SELECT pid, start_ticks FROM procs WHERE session_id = ? AND epoch = ? ORDER BY started_at DESC LIMIT 1",
            (caller.session_id, caller.epoch)).fetchone() or (None, None)
        task = _open_task(w, caller)
        try:
            held, _made = leases.acquire(w, resource=resource, holder_session=caller.session_id, holder_pid=holder[0],
                                         holder_start_ticks=holder[1], task_id=task.id if task else None,
                                         expires_at=expires_at)
        except LeaseHeld as exc:
            raise CommsError(exc.code, exc.message + WAIT_FOR_IT) from exc
        return t.record(w, _lease_view(held), lease_id=held.id)
    return await t.engine.write(fn)


async def release(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    resource = a.text(t.args, "resource", limit=WORD_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        held = leases.held(w, resource)
        if held is None or held.holder_session != caller.session_id:
            raise CommsError("not_your_lease", f"You hold no lease on {resource}.")
        ended = leases.release(w, held, why="released", actor=caller.session_id)
        return t.record(w, _lease_view(ended), lease_id=ended.id)
    return await t.engine.write(fn)


def mine(w: Writes, caller: Caller) -> dict[str, Any]:
    """The caller's armed timers and live leases, for ``read_state``."""
    chat_id, role, thread_ts = _place(caller)
    return {"timers": [_timer_view(timer) for timer in timers.addressed(w, chat_id=chat_id, role=role,
                                                                        thread_ts=thread_ts)],
            "leases": [_lease_view(held) for held in leases.of_session(w, caller.session_id)]}
