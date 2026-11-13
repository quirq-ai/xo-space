"""The founder's goals: what they asked for, with an end time (record
design v2 §2, §4, §5 rules 6 to 8; w5-w7-design W5).

A coordinator sets one with ``set_goal``, citing the founder's own message
in its chat; the comms tool checks the citation against the store before
``create`` is called. ``create`` writes the row, one ``goal`` event and one
ledger entry in the caller's transaction, so the three commit together or
not at all, and a ``request_key`` seen before returns the first goal and
writes nothing. A goal is never edited in place: ``move`` takes it to
another state, with its reason, its event and its entry. A coordinator
closes a goal as done or failed, as it reports it; the other states are
the founder's and the goal keeper's.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.cowork_agent.storm.records import RecordInvalid, check_asker, ledger, orphans
from services.cowork_agent.storm.redact import redact
from services.timestamps import iso, parse_ts

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes

STATES = ("active", "paused", "stalled", "done", "failed", "cancelled", "expired")
#: The states a goal never leaves.
FINAL = ("done", "failed", "cancelled", "expired")
_SELECT = f"SELECT {', '.join(ledger.GOAL_COLUMNS)} FROM goals"


@dataclass(frozen=True)
class Goal:
    """One ``goals`` row; its fields are the table's columns, in order."""

    id: str
    chat_id: str
    cites: str
    title: str
    ends_at: str
    state: str
    state_reason: str | None
    asked_by: str
    request_key: str
    created_at: str
    updated_at: str


def get(conn: sqlite3.Connection | Writes, goal_id: str) -> Goal | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", (goal_id,)).fetchone()
    return Goal(*row) if row else None


def of_chat(conn: sqlite3.Connection | Writes, chat_id: str) -> list[Goal]:
    """The chat's goals, the live ones first, then the latest changed."""
    marks = ", ".join("?" * len(FINAL))
    return [Goal(*row) for row in conn.execute(
        f"{_SELECT} WHERE chat_id = ? ORDER BY state IN ({marks}), updated_at DESC, id", (chat_id, *FINAL))]


def create(w: Writes, *, chat_id: str, cites: str, title: str, ends_at: str, asked_by: str,
           request_key: str) -> tuple[Goal, bool]:
    """The goal ``request_key`` asks for, and whether this call made it: a
    key seen before returns that goal and writes nothing. ``ends_at`` is an
    ISO time, kept as the store writes times. Raises ``RecordInvalid``
    before writing anything."""
    found = w.execute(f"{_SELECT} WHERE request_key = ?", (request_key,)).fetchone()
    if found:
        return Goal(*found), False
    check_asker(asked_by)
    if (end := parse_ts(ends_at)) is None:
        raise RecordInvalid(f"ends_at is an ISO time, such as 2026-11-12T18:00:00Z; {ends_at!r} isn't.")
    goal = Goal(f"goal_{uuid.uuid4().hex[:16]}", chat_id, cites, redact(title), iso(end), "active", None, asked_by,
                request_key, w.now, w.now)
    w.execute(f"INSERT INTO goals ({', '.join(ledger.GOAL_COLUMNS)}) VALUES "
              f"({', '.join('?' * len(ledger.GOAL_COLUMNS))})", dataclasses.astuple(goal))
    _record(w, goal, action="create", actor=asked_by)
    return goal, True


def move(w: Writes, goal: Goal, *, state: str, reason: str | None, actor: str) -> Goal:
    """Take ``goal`` to ``state`` (with why), with its event and ledger
    entry; a final goal stays as it is (``RecordInvalid``). A goal that
    ends takes its timers and its tasks' timers and leases with it
    (``orphans``)."""
    if state not in STATES:
        raise RecordInvalid(f"A goal's state is one of {', '.join(STATES)}; {state!r} isn't.")
    if goal.state in FINAL:
        raise RecordInvalid(f"Goal {goal.id} is {goal.state}; a {goal.state} goal stays so.")
    moved = dataclasses.replace(goal, state=state, state_reason=redact(reason) if reason else None, updated_at=w.now)
    w.execute("UPDATE goals SET state = ?, state_reason = ?, updated_at = ? WHERE id = ?",
              (moved.state, moved.state_reason, moved.updated_at, goal.id))
    _record(w, moved, action="move", actor=actor)
    if state in FINAL:
        owned = [row[0] for row in w.execute("SELECT id FROM tasks WHERE goal_id = ?", (goal.id,))]
        orphans.settle(w, task_ids=owned, goal_id=goal.id, why="goal_ended", said=f"its goal {goal.id} ended ({state})")
    return moved


def _record(w: Writes, goal: Goal, *, action: str, actor: str) -> None:
    """A change's ``goal`` event (ids and state; the title stays in the row)
    and its ledger entry."""
    w.append("goal", {"action": action, "id": goal.id, "state": goal.state, "ends_at": goal.ends_at,
                      "cites": goal.cites, "asked_by": goal.asked_by, "by": actor}, chat_id=goal.chat_id)
    ledger.append_row(w, actor=actor, action=action, kind="goal", row_id=goal.id)
