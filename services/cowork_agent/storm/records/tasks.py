"""Tasks: one thread's piece of work (record design v2 §2, §4, §5 rules 6
to 8; w5-w7-design W5).

A coordinator makes a task with ``create_task``, together with its thread,
in one transaction; ``instructions`` is its brief. A thread that has none
(it predates the upgrade, or a job or a continuation started it) gets one
at its first report or result. A task's thread reports with ``report``:
each report is kept in ``task_reports``, oldest first, as reported (one the
same as the report before it isn't kept twice), and ``last_report`` holds the
newest for cheap reads, with a ``task.report`` event; nobody is woken. Each change writes the row, its event and one ledger entry
in the caller's transaction, and a ``request_key`` seen before returns the
first task and writes nothing. A task is never edited in place: a report
and a state change (``move``) are its only changes. Whether its thread is
working, waiting or stalled is derived, never stored.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.cowork_agent.storm.records import RecordInvalid, check_asker, ledger, orphans
from services.cowork_agent.storm.redact import redact

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes

STATES = ("open", "done", "failed", "stopped", "cancelled")
#: The states that end a task; a coordinator's ``stopped`` it can undo.
ENDED = ("done", "failed", "cancelled")
_SELECT = f"SELECT {', '.join(ledger.TASK_COLUMNS)} FROM tasks"


@dataclass(frozen=True)
class Task:
    """One ``tasks`` row; its fields are the table's columns, in order."""

    id: str
    goal_id: str | None
    chat_id: str
    thread_ts: str
    title: str
    instructions: str
    state: str
    last_report: str | None
    reported_at: str | None
    asked_by: str
    request_key: str
    created_at: str
    updated_at: str


def get(conn: sqlite3.Connection | Writes, task_id: str) -> Task | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", (task_id,)).fetchone()
    return Task(*row) if row else None


@dataclass(frozen=True)
class Report:
    """One ``task_reports`` row: a report as its thread made it."""

    seq: int
    task_id: str
    text: str
    by: str
    at: str


#: A report row's columns, in order.
REPORT_COLUMNS = ("seq", "task_id", "text", "by", "at")


def reports(conn: sqlite3.Connection | Writes, task_id: str, *, limit: int,
            before: int | None = None) -> list[Report]:
    """The task's reports, newest first, at most ``limit``; ``before`` (a
    report's seq) pages back."""
    return [Report(*row) for row in conn.execute(
        f"SELECT {', '.join(REPORT_COLUMNS)} FROM task_reports WHERE task_id = ? AND seq < ? "
        "ORDER BY seq DESC LIMIT ?", (task_id, before if before is not None else 2 ** 62, limit))]


def report_count(conn: sqlite3.Connection | Writes, task_id: str) -> int:
    return conn.execute("SELECT count(*) FROM task_reports WHERE task_id = ?", (task_id,)).fetchone()[0]


def by_key(conn: sqlite3.Connection | Writes, request_key: str) -> Task | None:
    """The task a request made, if it made one."""
    row = conn.execute(f"{_SELECT} WHERE request_key = ?", (request_key,)).fetchone()
    return Task(*row) if row else None


def of_thread(conn: sqlite3.Connection | Writes, chat_id: str, thread_ts: str) -> Task | None:
    row = conn.execute(f"{_SELECT} WHERE chat_id = ? AND thread_ts = ?", (chat_id, thread_ts)).fetchone()
    return Task(*row) if row else None


def of_chat(conn: sqlite3.Connection | Writes, chat_id: str, *, limit: int) -> list[Task]:
    """The chat's tasks, the open ones first, then the latest changed."""
    return [Task(*row) for row in conn.execute(
        f"{_SELECT} WHERE chat_id = ? ORDER BY state <> 'open', updated_at DESC, id LIMIT ?", (chat_id, limit))]


def create(w: Writes, *, chat_id: str, thread_ts: str, title: str, instructions: str, asked_by: str, by: str,
           request_key: str, goal_id: str | None = None) -> tuple[Task, bool]:
    """The thread's task ``request_key`` asks for, and whether this call
    made it: a key seen before returns that task and writes nothing. Its
    thread must exist (a foreign key), and holds one task. ``asked_by`` is
    who asked for the work (the founder, citing their message: the thread's
    root; or a session); ``by`` is the session that recorded it."""
    if (found := by_key(w, request_key)) is not None:
        return found, False
    check_asker(asked_by)
    task = Task(f"task_{uuid.uuid4().hex[:16]}", goal_id, chat_id, thread_ts, title, redact(instructions), "open",
                None, None, asked_by, request_key, w.now, w.now)
    w.execute(f"INSERT INTO tasks ({', '.join(ledger.TASK_COLUMNS)}) VALUES "
              f"({', '.join('?' * len(ledger.TASK_COLUMNS))})", dataclasses.astuple(task))
    _record(w, task, "task", action="create", actor=by)
    return task, True


def report(w: Writes, task: Task, *, text: str, actor: str) -> Task:
    """The thread's report on ``task``, as reported: kept in its history
    (unless it is the same as the report before it) and as its newest, with
    its ``task.report`` event and ledger entry. It wakes no one."""
    reported = dataclasses.replace(task, last_report=redact(text), reported_at=w.now, updated_at=w.now)
    if reported.last_report != task.last_report:
        w.execute("INSERT INTO task_reports (task_id, text, by, at) VALUES (?, ?, ?, ?)",
                  (task.id, reported.last_report, actor, w.now))
    w.execute("UPDATE tasks SET last_report = ?, reported_at = ?, updated_at = ? WHERE id = ?",
              (reported.last_report, reported.reported_at, reported.updated_at, task.id))
    _record(w, reported, "task.report", action="report", actor=actor)
    return reported


def move(w: Writes, task: Task, *, state: str, actor: str) -> Task:
    """Take ``task`` to ``state``, with its event and ledger entry; a task
    already there is returned as it is, and nothing is written. A task that
    ends takes its timers, leases and timer wakes with it (``orphans``)."""
    if state not in STATES:
        raise RecordInvalid(f"A task's state is one of {', '.join(STATES)}; {state!r} isn't.")
    if task.state == state:
        return task
    moved = dataclasses.replace(task, state=state, updated_at=w.now)
    w.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (state, w.now, task.id))
    _record(w, moved, "task", action="move", actor=actor)
    if state in ENDED and task.state not in ENDED:
        orphans.settle(w, task_ids=[task.id], why="task_ended", said=f"its task {task.id} ended ({state})")
    return moved


def _record(w: Writes, task: Task, event: str, *, action: str, actor: str) -> None:
    """A change's event (ids and state; the words stay in the row) and its
    ledger entry."""
    w.append(event, {"action": action, "id": task.id, "state": task.state, "goal_id": task.goal_id,
                     "asked_by": task.asked_by, "by": actor}, chat_id=task.chat_id, thread_ts=task.thread_ts,
             session_id=actor if actor.startswith("cse_") else None)
    ledger.append_row(w, actor=actor, action=action, kind="task", row_id=task.id)
