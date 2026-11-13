"""Results: what a thread reported it delivered (record design v2 §2, §3,
§5 rules 6 to 8, §6 invariant 10; w5-w7-design W5).

A result is recorded as reported: XO never judges whether it is right.
What makes it auditable is what the server adds itself: who made it (the
session, its agent and model), what it cost, and its artifacts' hashes
(a commit's sha read with git, a file's sha256 computed from its bytes),
taken when it was reported. ``create`` writes the row, one ``result``
event and one ledger entry in the caller's transaction, and a
``request_key`` seen before returns the first result and writes nothing.
A result is never changed after that.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from services.cowork_agent.storm.records import RecordInvalid, ledger
from services.cowork_agent.storm.redact import redact

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes
    from services.cowork_agent.storm.records.tasks import Task

#: A result's model when its session ran its CLI's default and the catalog
#: couldn't name it: XO asked for no model, and says so.
CLI_DEFAULT = "cli-default"
_SELECT = f"SELECT {', '.join(ledger.RESULT_COLUMNS)} FROM results"


@dataclass(frozen=True)
class Result:
    """One ``results`` row; its fields are the table's columns, in order
    (``artifacts`` as the JSON the table holds)."""

    id: str
    task_id: str
    submitted_by: str
    agent: str
    model: str
    summary: str
    artifacts: str
    cost_usd: float | None
    request_key: str
    created_at: str


def of_task(conn: sqlite3.Connection | Writes, task_id: str) -> list[Result]:
    return [Result(*row) for row in conn.execute(f"{_SELECT} WHERE task_id = ? ORDER BY created_at, id",
                                                  (task_id,))]


def create(w: Writes, task: Task, *, submitted_by: str, agent: str, model: str, summary: str,
           artifacts: Sequence[Mapping[str, Any]], cost_usd: float | None, request_key: str) -> tuple[Result, bool]:
    """The result ``request_key`` records on ``task``, and whether this call
    made it: a key seen before returns that result and writes nothing."""
    found = w.execute(f"{_SELECT} WHERE request_key = ?", (request_key,)).fetchone()
    if found:
        return Result(*found), False
    if not submitted_by.startswith("cse_") or not agent or not model:
        raise RecordInvalid("A result names the session that made it, its agent and its model.")
    result = Result(f"res_{uuid.uuid4().hex[:16]}", task.id, submitted_by, agent, model, redact(summary),
                    json.dumps(list(artifacts)), cost_usd, request_key, w.now)
    w.execute(f"INSERT INTO results ({', '.join(ledger.RESULT_COLUMNS)}) VALUES "
              f"({', '.join('?' * len(ledger.RESULT_COLUMNS))})", dataclasses.astuple(result))
    w.append("result", {"id": result.id, "task_id": task.id, "submitted_by": submitted_by, "agent": agent,
                        "model": model, "cost_usd": cost_usd, "artifacts": len(artifacts)},
             chat_id=task.chat_id, thread_ts=task.thread_ts, session_id=submitted_by)
    ledger.append_row(w, actor=submitted_by, action="create", kind="result", row_id=result.id)
    return result, True
