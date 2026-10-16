"""What the engine writes, as SQL run inside one store transaction each
(ch 08 §3). These functions run on the store's thread: plain SQL, no awaiting.

Event types follow the stream's vocabulary (ch 09 §3): ``session``,
``turn.started``, ``turn.finished``, ``turn.stopped``, ``run.started``,
``run.finished``, ``pool``. A root turn's durable contract events are kept as
``step.*`` events (the history the presenter rebuilds blocks from, ch 09 §2);
a sub-agent's own steps are not stored (ch 14 §2.1).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.review import input_sha256
from services.cowork_agent.storm.store import hand_back

#: A root turn's durable events, as the state log keeps them.
STEP_TYPES: Mapping[type, str] = {
    c.ToolStarted: "step.tool", c.ToolFinished: "step.tool_done", c.TextDone: "step.text",
    c.ThinkingDone: "step.thinking", c.PlanUpdated: "step.plan", c.PermissionRequested: "step.permission",
    c.PermissionResolved: "step.permission_done", c.QuestionAsked: "step.question",
    c.QuestionAnswered: "step.question_done", c.Notice: "step.notice", c.Error: "step.error",
}
#: What counts as one of a turn's steps (the presenter's units, ch 09 §2).
COUNTED_STEPS = (c.ToolStarted, c.TextDone, c.ThinkingDone, c.PlanUpdated)
#: The envelope every contract event carries: it goes to the event's columns
#: or is dropped (``raw`` is never stored).
_ENVELOPE = frozenset({"session", "ts", "seq", "native_id", "turn_id", "run", "raw"})


class StaleEpoch(Exception):
    """A write from a process whose session has since moved to a new epoch
    (ch 07 §9.2): it is dropped, never applied."""

    def __init__(self, session_id: str, epoch: int, current: int | None) -> None:
        super().__init__(f"session {session_id}: a write from epoch {epoch} (the session is at {current})")
        self.session_id, self.epoch, self.current = session_id, epoch, current


@dataclass(frozen=True)
class Ids:
    """The ids a session's events carry in their columns."""

    session_id: str
    chat_id: str | None = None
    thread_ts: str | None = None

    def columns(self, **more: Any) -> dict[str, Any]:
        return {"session_id": self.session_id, "chat_id": self.chat_id, "thread_ts": self.thread_ts, **more}


def fence(w: Writes, session_id: str, epoch: int) -> None:
    """Compare-and-set on the epoch: every write for an attached process
    starts here."""
    row = w.execute("SELECT epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or row[0] != epoch:
        raise StaleEpoch(session_id, epoch, row[0] if row else None)


def set_status(w: Writes, ids: Ids, status: str, epoch: int, data: Mapping[str, Any] | None = None) -> None:
    w.execute("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", (status, w.now, ids.session_id))
    w.append("session", {"status": status, **(data or {})}, **ids.columns(epoch=epoch))


class TurnIdTaken(Exception):
    """A runtime reused a turn id another session's turn has: the contract's
    turn ids must be unique, and one session's events never land on
    another's turn."""


def ensure_turn(w: Writes, ids: Ids, turn_id: str, origin: str, epoch: int) -> bool:
    """The turn's row and its ``turn.started``, once: the admission (after
    ``send``) and the runtime's ``TurnStarted`` may each come first."""
    inserted = w.execute(
        "INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES (?, ?, ?, ?, 'running', ?) "
        "ON CONFLICT (id) DO NOTHING", (turn_id, ids.session_id, epoch, origin, w.now)).rowcount == 1
    if inserted:
        w.append("turn.started", {"origin": origin}, **ids.columns(turn_id=turn_id, epoch=epoch))
    elif w.execute("SELECT session_id FROM turns WHERE id = ?", (turn_id,)).fetchone()[0] != ids.session_id:
        raise TurnIdTaken(f"turn id {turn_id} already belongs to another session")
    return inserted


def take_wakes(w: Writes, wake_ids: Iterable[str], turn_id: str) -> str:
    """Hand the wakes to the turn: ``taken`` while it runs; ``consumed`` if it
    already ended (the runtime's result was recorded first). Returns which."""
    running = w.execute("SELECT status = 'running' FROM turns WHERE id = ?", (turn_id,)).fetchone()[0]
    state = "taken" if running else "consumed"
    w.tx.conn.executemany(
        "UPDATE wakes SET state = ?, turn_id = ?, taken_at = ?, done_at = ? WHERE id = ? AND state = 'queued'",
        [(state, turn_id, w.now, None if running else w.now, wake_id) for wake_id in wake_ids])
    return state


@dataclass(frozen=True)
class TurnEnd:
    turn_id: str
    status: str                        # finished | failed | stopped | interrupted
    stop_reason: str | None
    error_class: str | None
    error: str | None
    usage: c.TokenUsage | None
    wall_ms: int
    wait_person_ms: int
    steps: int
    tool_calls: int
    peak_rss_kb: int | None
    rate_limit: Mapping[str, Any] | None
    stopped_by: str | None = None
    last_step: str | None = None
    hand_back: str | None = None       # the resume note when the wakes go back to the queue


def finish_turn(w: Writes, ids: Ids, end: TurnEnd, epoch: int) -> list[str]:
    """The turn's numbers (ch 07 §11), what becomes of its wakes (consumed;
    or handed back with ``end.hand_back`` before them, dropped after too many
    tries), ``turn.finished`` (and ``turn.stopped`` after a Stop): one
    transaction. Returns the wakes dropped."""
    usage = dataclasses.asdict(end.usage) if end.usage else None
    cost = end.usage.cost_usd if end.usage else None
    w.execute("UPDATE turns SET status = ?, ended_at = ?, stop_reason = ?, error_class = ?, usage = json(?), "
              "cost_usd = ?, cost_source = ?, wall_ms = ?, wait_person_ms = ?, steps = ?, tool_calls = ?, "
              "peak_rss_kb = ?, rate_limit = json(?) WHERE id = ?",
              (end.status, w.now, end.stop_reason, end.error_class, _dumps(usage), cost,
               "reported" if cost is not None else None, end.wall_ms, end.wait_person_ms, end.steps,
               end.tool_calls, end.peak_rss_kb, _dumps(end.rate_limit), end.turn_id))
    wakes, requeued, dropped = [], [], []
    if end.hand_back is not None:
        requeued, dropped = hand_back(w.tx, end.turn_id, end.hand_back)
    else:
        wakes = [r[0] for r in w.execute("SELECT id FROM wakes WHERE turn_id = ? AND state = 'taken' ORDER BY rowid",
                                         (end.turn_id,)).fetchall()]
        w.execute("UPDATE wakes SET state = 'consumed', done_at = ? WHERE turn_id = ? AND state = 'taken'",
                  (w.now, end.turn_id))
    w.append("turn.finished", {
        "status": end.status, "stop_reason": end.stop_reason, "error_class": end.error_class, "error": end.error,
        "wakes": wakes, "wakes_requeued": requeued, "wakes_dropped": dropped, "usage": usage, "cost_usd": cost, "wall_ms": end.wall_ms,
        "wait_person_ms": end.wait_person_ms, "steps": end.steps, "tool_calls": end.tool_calls,
        "peak_rss_kb": end.peak_rss_kb}, **ids.columns(turn_id=end.turn_id, epoch=epoch))
    if end.stopped_by is not None:
        w.append("turn.stopped", {"by": end.stopped_by, "last_step": end.last_step},
                 **ids.columns(turn_id=end.turn_id, epoch=epoch))
    return dropped


def step(w: Writes, ids: Ids, event: c.Event, epoch: int) -> None:
    w.append(STEP_TYPES[type(event)], event_data(event), **ids.columns(turn_id=event.turn_id, epoch=epoch))


def run_event(w: Writes, ids: Ids, event: c.RunStarted | c.RunFinished, epoch: int) -> None:
    kind = "run.started" if isinstance(event, c.RunStarted) else "run.finished"
    w.append(kind, event_data(event), **ids.columns(turn_id=event.turn_id, run_id=event.run, epoch=epoch))


def event_data(event: c.Event) -> dict[str, Any]:
    """An event's own fields, without its envelope. A tool's or a request's
    raw input is left out: the CLI's transcript keeps it (ch 08 §4), and the
    neutral fields (title, target, command, paths) say what it was."""
    data = {k: v for k, v in dataclasses.asdict(event).items() if k not in _ENVELOPE}
    if isinstance(event, c.ToolStarted):
        data.pop("input", None)
    if isinstance(event, c.PermissionRequested):
        # The hash a grant is bound to, so "the same action" can be matched
        # (the loop breaker, ch 15 §9) without keeping the input.
        data["request"]["input_sha256"] = input_sha256(event.request.input)
        data["request"].pop("input", None)
    return data


def usage_totals(w: "Writes | sqlite3.Connection", session_id: str) -> dict[str, int]:
    """The session's tokens over every turn, as today's session-index row
    carries them."""
    row = w.execute(
        "SELECT coalesce(sum(json_extract(usage, '$.input')), 0), coalesce(sum(json_extract(usage, '$.output')), 0),"
        " coalesce(sum(json_extract(usage, '$.cache_write')), 0), coalesce(sum(json_extract(usage, '$.cache_read')), 0)"
        " FROM turns WHERE session_id = ?", (session_id,)).fetchone()
    return {"input_tokens": row[0], "output_tokens": row[1], "cache_creation_input_tokens": row[2],
            "cache_read_input_tokens": row[3]}


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
