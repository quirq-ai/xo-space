"""Timers: every wake set for later, and every agent job (record design v2
§2, §4, §5 rules 5 to 7 and 9; w5-w7-design W6).

One table holds three kinds:

* ``at``: once, at ``due_at``;
* ``every``: an agent job repeating on the grid its ``due_at`` keeps: each
  fire moves ``due_at`` to the slot after the latest one due, so a late fire
  never shifts the ones after it, and slots missed while the server was down
  give one fire, not one each;
* ``on``: once, when an engine event happens (``event``, one of ``EVENTS``
  with what it names: ``on(event, about)``).

A timer wakes a role in a chat (a coordinator, or a thread by its ts); the
timer loop (``storm/timers.py``) finds that role's live session when it
fires, so a replaced coordinator's timer reaches its successor. With
``job`` it is an agent job instead: each fire opens a new thread whose first
input is ``note``, the job's prompt (``storm/jobs``). Agents arm their own
one-off and event timers (``wake_me``); a job exists only once the founder
made it or allowed it (``approved_by``: the founder, or the review item).

States: ``armed`` → ``fired``, ``cancelled`` (by the caller, the founder,
or XO when the chat is retired) or ``expired`` (its task or goal ended:
``orphans``). An ``every`` stays armed. ``fired`` holds the last fire as
recorded: its slot and what came of it.

Each change writes the row, one ``timer`` event and one ledger entry in the
caller's transaction; a ``request_key`` seen before returns the first timer
and writes nothing. ``note`` and a job's name are kept redacted, as every
agent's words are.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from services.cowork_agent.storm.records import RecordInvalid, check_asker, ledger
from services.cowork_agent.storm.redact import redact
from services.timestamps import iso, parse_ts

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes

KINDS = ("at", "every", "on")
STATES = ("armed", "fired", "cancelled", "expired")
#: The roles a timer wakes.
ROLES = ("workspace-coordinator", "project-coordinator", "thread")
#: What an ``on`` timer waits for. Each names its instance after a colon: a
#: resource, ``<pid>:<start ticks>``, a pool id, a task id.
EVENTS = ("lease_freed", "process_exited", "pool_reopened", "task_done")
_SELECT = f"SELECT {', '.join(ledger.TIMER_COLUMNS)} FROM timers"


@dataclass(frozen=True)
class Timer:
    """One ``timers`` row; its fields are the table's columns, in order
    (``job`` and ``fired`` as the JSON the table holds)."""

    id: str
    kind: str
    chat_id: str
    role: str
    thread_ts: str | None
    due_at: str | None
    every_s: int | None
    event: str | None
    note: str
    job: str | None
    goal_id: str | None
    task_id: str | None
    state: str
    state_reason: str | None
    fired: str | None
    asked_by: str
    approved_by: str | None
    request_key: str
    created_at: str
    updated_at: str

    @property
    def definition(self) -> dict[str, Any] | None:
        """A job's name, agent, model, effort, anchor and offset; None for a wake."""
        return json.loads(self.job) if self.job else None

    @property
    def last(self) -> dict[str, Any] | None:
        """The last fire as recorded, or None."""
        return json.loads(self.fired) if self.fired else None


def on(event: str, about: str) -> str:
    """The ``event`` of an ``on`` timer waiting for ``event`` about ``about``."""
    return f"{event}:{about}"


def get(conn: sqlite3.Connection | Writes, timer_id: str) -> Timer | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", (timer_id,)).fetchone()
    return Timer(*row) if row else None


def by_key(conn: sqlite3.Connection | Writes, request_key: str) -> Timer | None:
    row = conn.execute(f"{_SELECT} WHERE request_key = ?", (request_key,)).fetchone()
    return Timer(*row) if row else None


def addressed(conn: sqlite3.Connection | Writes, *, chat_id: str, role: str, thread_ts: str | None) -> list[Timer]:
    """The armed wakes (not jobs) set for one role in a chat, soonest first
    (event timers last)."""
    return [Timer(*row) for row in conn.execute(
        f"{_SELECT} WHERE chat_id = ? AND role = ? AND thread_ts IS ? AND job IS NULL AND state = 'armed' "
        "ORDER BY due_at IS NULL, due_at, created_at, id", (chat_id, role, thread_ts))]


def jobs(conn: sqlite3.Connection | Writes) -> list[Timer]:
    """Every agent job, in any state, oldest first."""
    return [Timer(*row) for row in conn.execute(f"{_SELECT} WHERE job IS NOT NULL ORDER BY created_at, id")]


def armed_in(conn: sqlite3.Connection | Writes, chat_id: str) -> list[Timer]:
    return [Timer(*row) for row in conn.execute(f"{_SELECT} WHERE chat_id = ? AND state = 'armed' ORDER BY id",
                                                (chat_id,))]


def due(conn: sqlite3.Connection | Writes, now: str) -> list[Timer]:
    """The armed timers whose time has come at ``now``, soonest first."""
    return [Timer(*row) for row in conn.execute(
        f"{_SELECT} WHERE state = 'armed' AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at, id", (now,))]


def next_due(conn: sqlite3.Connection | Writes, *, besides: Sequence[str] = ()) -> str | None:
    """The soonest ``due_at`` of an armed timer, those in ``besides`` aside."""
    marks = ", ".join("?" * len(besides))
    return conn.execute(f"SELECT min(due_at) FROM timers WHERE state = 'armed' AND due_at IS NOT NULL"
                        f"{f' AND id NOT IN ({marks})' if besides else ''}", tuple(besides)).fetchone()[0]


def waiting(conn: sqlite3.Connection | Writes, event: str | None = None) -> list[Timer]:
    """The armed ``on`` timers: every one, or those waiting for ``event``."""
    if event is None:
        return [Timer(*row) for row in conn.execute(f"{_SELECT} WHERE state = 'armed' AND kind = 'on' ORDER BY id")]
    return [Timer(*row) for row in conn.execute(f"{_SELECT} WHERE state = 'armed' AND event = ? ORDER BY id",
                                                (event,))]


def of_work(conn: sqlite3.Connection | Writes, *, task_ids: Sequence[str], goal_id: str | None) -> list[Timer]:
    """The timers of these tasks or of this goal, in any state."""
    marks = ", ".join("?" * len(task_ids))
    return [Timer(*row) for row in conn.execute(
        f"{_SELECT} WHERE task_id IN ({marks}) OR goal_id = ? ORDER BY id", (*task_ids, goal_id))]


def slot(timer: Timer, now: float) -> tuple[str, str | None]:
    """The slot a due timer fires for at ``now`` (wall seconds), and an
    ``every``'s next due: the latest slot of its grid that has come, and the
    one after it."""
    if timer.kind != "every":
        return timer.due_at or _iso(now), None
    start = parse_ts(timer.due_at).timestamp()
    passed = max(0, int((now - start) // timer.every_s))
    at = start + passed * timer.every_s
    return _iso(at), _iso(at + timer.every_s)


def arm(w: Writes, *, kind: str, chat_id: str, role: str, note: str, asked_by: str, by: str, request_key: str,
        thread_ts: str | None = None, due_at: str | None = None, every_s: int | None = None, event: str | None = None,
        job: Mapping[str, Any] | None = None, goal_id: str | None = None, task_id: str | None = None,
        approved_by: str | None = None, state: str = "armed", state_reason: str | None = None,
        fired: Mapping[str, Any] | None = None, timer_id: str | None = None) -> tuple[Timer, bool]:
    """The timer ``request_key`` asks for, and whether this call made it: a
    key seen before returns that timer and writes nothing. ``by`` is who
    recorded it; ``timer_id`` keeps an id given elsewhere (a job the
    scheduler named). Raises ``RecordInvalid`` before writing anything."""
    if (found := by_key(w, request_key)) is not None:
        return found, False
    check_asker(asked_by)
    if kind not in KINDS or role not in ROLES or state not in STATES:
        raise RecordInvalid(f"A timer's kind is one of {KINDS}, its role one of {ROLES}, its state one of {STATES}.")
    if event is not None and event.partition(":")[0] not in EVENTS:
        raise RecordInvalid(f"An event timer waits for one of {', '.join(EVENTS)}; {event!r} isn't.")
    timer = Timer(timer_id or f"tmr_{uuid.uuid4().hex[:16]}", kind, chat_id, role, thread_ts, _stamp(due_at), every_s,
                  event, redact(note), _job(job), goal_id, task_id, state, state_reason,
                  json.dumps(dict(fired)) if fired else None, asked_by, approved_by, request_key, w.now, w.now)
    w.execute(f"INSERT INTO timers ({', '.join(ledger.TIMER_COLUMNS)}) VALUES "
              f"({', '.join('?' * len(ledger.TIMER_COLUMNS))})", dataclasses.astuple(timer))
    _record(w, timer, action="arm", actor=by)
    return timer, True


def fire(w: Writes, timer: Timer, *, at: str, outcome: Mapping[str, Any], next_due: str | None,
         moves: bool = True) -> Timer:
    """Record ``timer``'s fire for the slot ``at`` and what came of it. It
    ``moves`` on: an ``every`` to ``next_due``, an ``at`` or ``on`` to
    ``fired``. A timer that changed while its fire was opening (cancelled,
    or the founder gave a job a new schedule) doesn't move: only its last
    fire is recorded."""
    changes: dict[str, Any] = {"fired": json.dumps({"at": at, **outcome})}
    if moves and timer.state == "armed" and timer.kind == "every":
        changes["due_at"] = next_due
    elif moves and timer.state == "armed":
        changes["state"] = "fired"
    return _change(w, timer, changes, action="fire", actor=ledger.XO)


def end(w: Writes, timer: Timer, *, state: str, reason: str, actor: str) -> Timer:
    """Cancel or expire an armed timer, saying why; one already over is
    returned as it is, and nothing is written."""
    if state not in ("cancelled", "expired"):
        raise RecordInvalid(f"A timer ends cancelled or expired; {state!r} isn't.")
    if timer.state != "armed":
        return timer
    return _change(w, timer, {"state": state, "state_reason": redact(reason)}, action=state, actor=actor)


def redefine(w: Writes, timer: Timer, *, chat_id: str, kind: str, due_at: str | None, every_s: int | None,
             note: str, job: Mapping[str, Any], approved_by: str, armed: bool, reason: str | None,
             actor: str) -> Timer:
    """The founder's new definition of a job: its project's chat, schedule,
    prompt and choices, on or off (with why)."""
    if timer.job is None:
        raise RecordInvalid(f"Timer {timer.id} isn't a job: only a job is redefined.")
    return _change(w, timer, {"chat_id": chat_id, "kind": kind, "due_at": _stamp(due_at), "every_s": every_s,
                              "note": redact(note),
                              "job": _job(job), "approved_by": approved_by, "state": "armed" if armed else "cancelled",
                              "state_reason": None if armed else redact(reason or "")}, action="redefine", actor=actor)


def _change(w: Writes, timer: Timer, changes: Mapping[str, Any], *, action: str, actor: str) -> Timer:
    changed = dataclasses.replace(timer, **changes, updated_at=w.now)
    columns = [c for c in ledger.TIMER_COLUMNS if c != "id"]
    w.execute(f"UPDATE timers SET {', '.join(f'{c} = ?' for c in columns)} WHERE id = ?",
              (*(getattr(changed, c) for c in columns), timer.id))
    _record(w, changed, action=action, actor=actor)
    return changed


def _record(w: Writes, timer: Timer, *, action: str, actor: str) -> None:
    """A change's ``timer`` event (ids, kind, state and times; the note stays
    in the row) and its ledger entry."""
    w.append("timer", {"action": action, "id": timer.id, "kind": timer.kind, "state": timer.state,
                       "due_at": timer.due_at, "event": timer.event, "role": timer.role, "job": timer.job is not None,
                       "task_id": timer.task_id, "goal_id": timer.goal_id, "asked_by": timer.asked_by, "by": actor,
                       **({"fired": timer.last} if action == "fire" else {})},
             chat_id=timer.chat_id, thread_ts=timer.thread_ts,
             session_id=actor if actor.startswith("cse_") else None)
    ledger.append_row(w, actor=actor, action=action, kind="timer", row_id=timer.id)


def _job(job: Mapping[str, Any] | None) -> str | None:
    if job is None:
        return None
    return json.dumps({**job, "name": redact(job["name"])} if job.get("name") else dict(job))


def _stamp(value: str | None) -> str | None:
    """A time as the store writes it, so times compare as text."""
    if value is None:
        return None
    if (when := parse_ts(value)) is None:
        raise RecordInvalid(f"A timer's time is an ISO time, such as 2026-11-12T18:00:00Z; {value!r} isn't.")
    return iso(when)


def _iso(seconds: float) -> str:
    return iso(datetime.fromtimestamp(seconds, timezone.utc))
