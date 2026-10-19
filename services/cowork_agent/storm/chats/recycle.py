"""Keeping a coordinator usable forever: retire an incarnation between
inputs and start its successor from a ``<handoff>`` (design ch 11 §6.2;
the PoC's ``recycle.py``, D51; thresholds are constants, sedc 09 #15).

An incarnation is retired, never mid-turn, when it has taken ``TURNS_MAX``
inputs, is older than ``AGE_MAX_S``, its last turn's context passed
``CONTEXT_MAX_TOKENS``, its role's fingerprint changed (``upgrade``), or its
chat was pinned to another agent (``agent``). Threads are never recycled
(D52). In one transaction the old session is retired, the successor created
(incarnation + 1, ``predecessor``), and its first input is the handoff,
followed by every wake that was queued for the old one.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.engine import Wake, Writes
from services.timestamps import parse_ts

TURNS_MAX = 30
AGE_MAX_S = 24 * 3600
CONTEXT_MAX_TOKENS = 120_000
THREADS_IN_HANDOFF = 20
TIMELINE_IN_HANDOFF = 30
#: Session states a coordinator can be retired in (``queued``: cold, its wakes held back).
BETWEEN_INPUTS = ("cold", "queued", "warm")
#: Of those, the ones with no process.
NO_PROCESS = ("cold", "queued")
#: Threads in these states are listed as pending in a handoff.
PENDING_STATES = ("needs_reply", "needs_approval", "failed", "paused")


class Busy(Exception):
    """The coordinator took an input meanwhile: it is recycled at its next
    boundary, and what was for it goes to it."""


def input_ended(conn, session_id: str) -> bool:
    """Its last input ended: no process runs it (a crashed input waiting for
    its retry can't go on in a process that is gone; the successor takes the
    requeued wakes), or every turn it finished is covered by an
    ``input.ended``. Between a worker's end and claude's own second turn the
    engine is idle, but the input isn't over (ch 11 §5)."""
    status = conn.execute("SELECT status FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if status is not None and status[0] in NO_PROCESS:
        return True
    last_turn = conn.execute("SELECT max(seq) FROM events WHERE session_id = ? AND type = 'turn.finished'",
                             (session_id,)).fetchone()[0]
    if last_turn is None:
        return True
    through = conn.execute("SELECT json_extract(data, '$.through') FROM events WHERE session_id = ? "
                           "AND type = 'input.ended' ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
    return through is not None and through[0] >= last_turn


def reason(conn, session_id: str, *, now_s: float, fingerprint_changed: bool, agent_changed: bool) -> str | None:
    """Why this coordinator should be replaced before its next input, or None."""
    created_at, = conn.execute("SELECT created_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    inputs = conn.execute("SELECT count(*) FROM turns WHERE session_id = ? AND origin <> 'injected'",
                          (session_id,)).fetchone()[0]
    if inputs >= TURNS_MAX:
        return "turns"
    born = parse_ts(created_at)
    if born is not None and now_s - born.timestamp() > AGE_MAX_S:
        return "age"
    last = conn.execute("SELECT usage FROM turns WHERE session_id = ? AND usage IS NOT NULL ORDER BY started_at DESC "
                        "LIMIT 1", (session_id,)).fetchone()
    # The runtime's own measure; a runtime that doesn't report it gives no context reason.
    context = (json.loads(last[0]) if last else {}).get("context_tokens")
    if context is not None and context > CONTEXT_MAX_TOKENS:
        return "context"
    if fingerprint_changed:
        return "upgrade"
    if agent_changed:
        return "agent"
    return None


def handoff_parts(conn, chat: Mapping[str, Any]) -> dict[str, Any]:
    """What the successor reads: the chat's threads with their cards and its
    recent timeline (the memory index is read from its file by the caller)."""
    threads = [{**t, "card": rows.latest_card(conn, chat["id"], t["ts"])}
               for t in rows.list_threads(conn, chat["id"], limit=THREADS_IN_HANDOFF)]
    return {"threads": threads,
            "timeline": rows.latest_messages(conn, chat_id=chat["id"], thread_ts=None, limit=TIMELINE_IN_HANDOFF),
            "pending": [f"{t['title']}: {t['state']}" for t in threads if t["state"] in PENDING_STATES]}


def succeed(w: Writes, *, old: str, new: str, why: str, chat: Mapping[str, Any], memory_index: str | None,
            parts: Mapping[str, Any]) -> None:
    """Inside the successor's creating transaction: retire ``old``, make
    ``new`` its next incarnation, and queue the handoff, then the old one's
    queued wakes, in that order (their hops now lead to ``new``). Raises
    ``Busy`` when ``old`` is not between inputs (its process busy, or its
    last input not ended): nothing is written."""
    row = w.execute("SELECT incarnation, status, epoch, retired_at FROM sessions WHERE id = ?", (old,)).fetchone()
    taken = w.execute("SELECT 1 FROM wakes WHERE session_id = ? AND state = 'taken' LIMIT 1", (old,)).fetchone()
    if row is None or row[3] is not None or row[1] not in BETWEEN_INPUTS or taken or not input_ended(w, old):
        raise Busy(old)
    incarnation, status, epoch, _retired = row
    w.execute("UPDATE sessions SET retired_at = ?, updated_at = ? WHERE id = ?", (w.now, w.now, old))
    w.execute("UPDATE sessions SET incarnation = ?, predecessor = ? WHERE id = ?", (incarnation + 1, old, new))
    w.append("session", {"status": status, "reason": "retired", "why": why, "successor": new},
             session_id=old, chat_id=chat["id"], epoch=epoch)
    queued = w.execute("SELECT id, priority, reason, envelope, meta, redelivery, created_at FROM wakes "
                       "WHERE session_id = ? AND state = 'queued' ORDER BY rowid", (old,)).fetchall()
    w.execute("DELETE FROM wakes WHERE session_id = ? AND state = 'queued'", (old,))
    w.wake(new, Wake(id=f"handoff:{old}", priority="now", reason="handoff", origin="system", text=envelope.handoff(
        predecessor=old, chat=chat, memory_index=memory_index, threads=parts["threads"], timeline=parts["timeline"],
        pending=parts["pending"], now=w.now)))
    w.tx.conn.executemany(
        "INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, redelivery, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
        [(wid, new, priority, why_, text, meta, redelivery, created) for wid, priority, why_, text, meta, redelivery,
         created in queued])
    w.tx.conn.executemany("UPDATE hops SET to_session = ? WHERE wake_id = ? AND to_session = ?",
                          [(new, wid, old) for wid, *_rest in queued])
