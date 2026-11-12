"""Replacing a coordinator (w5-w7-design W7; the plan's decision 4 and C):
only when its context is full, when an XO upgrade changed its role (its
tools or prompt: ``upgrade``), or when the founder switched its chat's agent
(``agent_switch``). Never mid-turn, and never for a count of inputs or an
age. Threads are never replaced (D52).

**Full, per runtime.** Each turn records the context's real size at its end
(``turns.usage.context_tokens``), and the runtime's feature list says where
its CLI compacts a context on its own (``context.compacts_at``, measured from
the CLI's transcripts). A measured turn that leaves the context within one
more input of that point (``GROWTH_MARGIN``) makes the coordinator *nearly
full*: its next input asks it for its note (``NEARLY_FULL``), and only then
does ``handoff_note`` take one. Once an input that started nearly full has
ended, it is full, and replaced whether a note came or not: no turn is ever
run for the note alone. A runtime that doesn't say where its CLI compacts,
or doesn't report the context's size, gets no size-based replacement; nor
does a session whose first measured turn was already nearly full (a window
too small for a successor to start any lower). Such a coordinator is
replaced only on an upgrade or an agent switch.

**The successor starts with the next real wake.** In one transaction the old
session is retired with its reason (``sessions.retired_reason``), the
successor made (incarnation + 1, ``predecessor``), and the old one's queued
wakes moved to it; a wake sent to the old one later follows it there
(``Writes.wake``). Nothing is queued for the successor: its first input is a
real one, and ``Replacement`` (a ``ports.TurnNote``) folds the handoff into
it: the predecessor's note if it wrote one, and the facts, read from the
store as that input starts, never from the note. The 11-10 pilot: a
successor started in a turn of its own invented a founder request in 7 of
20 runs, one started with a real message in none of 40, and no note,
whoever wrote it, kept every open item.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Mapping

from services.cowork_agent.storm.chats import envelope, memory, rows
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import goals, results, tasks, timers

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.adapters.contract import FeatureList, SessionSpec
    from services.cowork_agent.storm.engine import Engine
    from services.cowork_agent.storm.engine.session import Meta

#: The feature saying where a runtime's CLI compacts a context on its own (contract 1.2).
COMPACTS_AT = "context.compacts_at"
#: How much one input can grow a coordinator's context, measured from the
#: transcripts (2026-11-12): a Claude coordinator's largest growth in one
#: input was 149.7k tokens (its 99th percentile 12.6k), any Codex session's
#: 129.2k. A context this close to its CLI's compaction point could pass it
#: in its next input, so the input after that is its last.
GROWTH_MARGIN = 150_000
#: Why a coordinator was replaced, as ``sessions.retired_reason`` records it.
CONTEXT_FULL, UPGRADE, AGENT_SWITCH = "context_full", "upgrade", "agent_switch"
#: What a nearly full coordinator's next input asks of it.
NEARLY_FULL = "Your context is nearly full: in this turn, write handoff_note before you end."
#: The event that keeps a coordinator's note for its successor.
NOTE_EVENT = "handoff.note"
#: What ``handoff_note`` answers once the note is kept.
NOTE_KEPT = ("Kept for the coordinator that replaces you, with XO's records. Now answer this input as usual; you "
             "are replaced once it ends.")
#: How far back a handoff carries the founder's words, and tasks and threads that closed.
FACTS_WINDOW_S = 24 * 3600
#: Session states a coordinator can be retired in (``queued``: cold, its wakes held back).
BETWEEN_INPUTS = ("cold", "queued", "warm")
#: Of those, the ones with no process.
NO_PROCESS = ("cold", "queued")
_MEASURED = "usage IS NOT NULL AND json_extract(usage, '$.context_tokens') IS NOT NULL"


class Busy(Exception):
    """The coordinator took an input meanwhile: it is replaced at its next
    boundary, and what was for it goes to it."""


def is_coordinator(role: str) -> bool:
    return role.endswith("-coordinator")


def input_ended(conn, session_id: str) -> bool:
    """Its last input ended and was judged: no process runs it (a crashed
    input waiting for its retry can't go on in a process that is gone; the
    successor takes the requeued wakes), or every turn it finished is covered
    by an ``input.ended``. The engine's state says when the CLI is done with
    an input (its ``SessionIdle``, a worker gap included, ch 07 §2);
    this is the part after it: the reply gate, cards and routing record
    written (ch 11 §5), so a gate is never lost to a replacement."""
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


# ── full context ─────────────────────────────────────────────────────────────

def compacts_at(features: FeatureList | None, window: int | None) -> int | None:
    """Where the runtime's CLI compacts a context on its own, in tokens, for
    a turn that reported ``window``; None when its feature list doesn't say
    (or says it relative to a window the turn didn't report)."""
    entry = features.features.get(COMPACTS_AT) if features is not None else None
    if entry is None or entry.support != "native" or len(entry.values) != 1 or not entry.values[0].isdigit():
        return None
    tokens = int(entry.values[0])
    if entry.behaviour == "fixed":
        return tokens
    if entry.behaviour == "below_window" and isinstance(window, int) and window > tokens:
        return window - tokens
    return None


def _within_one_input(usage: str | None, features: FeatureList | None) -> bool:
    """A turn's recorded usage left its context within one more input of
    where its CLI compacts."""
    found = json.loads(usage) if usage else {}
    context, point = found.get("context_tokens"), compacts_at(features, found.get("context_window"))
    return isinstance(context, int) and point is not None and context + GROWTH_MARGIN >= point


def _measured(conn, session_id: str, *, first: bool = False, before: int | None = None) -> str | None:
    """The usage of the session's first or latest measured turn (latest of
    those before the turn at rowid ``before``); turns are in rowid order."""
    sql, params = f"SELECT usage FROM turns WHERE session_id = ? AND {_MEASURED}", [session_id]
    if before is not None:
        sql, params = sql + " AND rowid < ?", [*params, before]
    row = conn.execute(f"{sql} ORDER BY rowid {'ASC' if first else 'DESC'} LIMIT 1", params).fetchone()
    return row[0] if row else None


def _room_to_gain(conn, session_id: str, features: FeatureList | None) -> bool:
    """A replacement makes room: the session's first measured turn wasn't
    already nearly full (a successor starts about as large)."""
    first = _measured(conn, session_id, first=True)
    return first is not None and not _within_one_input(first, features)


def nearly_full(conn, session_id: str, features: FeatureList | None) -> bool:
    """Its latest measured turn left it within one more input of where its
    CLI compacts: its next input asks for its note, and ``handoff_note``
    takes one."""
    return _room_to_gain(conn, session_id, features) and _within_one_input(_measured(conn, session_id), features)


def context_full(conn, session_id: str, features: FeatureList | None) -> bool:
    """An input that started nearly full (so it was asked for its note) has
    begun: the turn of its latest input, not one its CLI started itself,
    came after a measured turn that left it nearly full."""
    if not _room_to_gain(conn, session_id, features):
        return False
    latest = conn.execute("SELECT max(rowid) FROM turns WHERE session_id = ? AND origin <> 'injected'",
                          (session_id,)).fetchone()[0]
    return latest is not None and _within_one_input(_measured(conn, session_id, before=latest), features)


def reason(conn, session_id: str, *, features: FeatureList | None, fingerprint_changed: bool,
           agent_changed: bool) -> str | None:
    """Why this coordinator should be replaced before its next input, or None."""
    if context_full(conn, session_id, features):
        return CONTEXT_FULL
    if fingerprint_changed:
        return UPGRADE
    if agent_changed:
        return AGENT_SWITCH
    return None


# ── the replacement ──────────────────────────────────────────────────────────

def succeed(w: Writes, *, old: str, new: str, why: str, chat: Mapping[str, Any]) -> None:
    """Inside the successor's creating transaction: retire ``old`` saying
    ``why``, make ``new`` its next incarnation, and move ``old``'s queued
    wakes to it through the engine's wake gate (``Writes.move_queued``; their
    hops now lead to ``new``). Nothing more is queued: ``new`` starts with
    its next real wake. Raises ``Busy`` when ``old`` is not between inputs
    (its process busy, or its last input not ended): nothing is written."""
    row = w.execute("SELECT incarnation, status, epoch, retired_at FROM sessions WHERE id = ?", (old,)).fetchone()
    taken = w.execute("SELECT 1 FROM wakes WHERE session_id = ? AND state = 'taken' LIMIT 1", (old,)).fetchone()
    if row is None or row[3] is not None or row[1] not in BETWEEN_INPUTS or taken or not input_ended(w, old):
        raise Busy(old)
    incarnation, status, epoch, _retired = row
    # The queued input moves below; a predecessor with no process is now cold.
    # A warm process remains warm until the engine releases it.
    status = "cold" if status == "queued" else status
    w.execute("UPDATE sessions SET status = ?, retired_at = ?, retired_reason = ?, updated_at = ? WHERE id = ?",
              (status, w.now, why, w.now, old))
    w.execute("UPDATE sessions SET incarnation = ?, predecessor = ? WHERE id = ?", (incarnation + 1, old, new))
    w.append("session", {"status": status, "reason": "retired", "why": why, "successor": new},
             session_id=old, chat_id=chat["id"], epoch=epoch)
    moved = w.move_queued(old)
    w.tx.conn.executemany("UPDATE hops SET to_session = ? WHERE wake_id = ? AND to_session = ?",
                          [(new, wid, old) for wid in moved])


def keep_note(w: Writes, *, session_id: str, chat_id: str, sections: Mapping[str, str]) -> None:
    """The outgoing coordinator's note, for its successor: its latest one counts."""
    w.append(NOTE_EVENT, {"sections": dict(sections)}, session_id=session_id, chat_id=chat_id)


def facts(conn, chat_id: str, role: str, *, since: str) -> dict[str, list[dict[str, Any]]]:
    """What the chat's coordinator must know is open, from the store as it
    is now: the goals not ended; the tasks open, and those that closed since
    ``since``, each with its latest report and its results; the asks to or
    from any of its incarnations not yet answered; the chat's open review
    items; its threads not resolved, and those changed since; the timers
    set to wake its role; and the founder's own words since, word for word,
    on the timeline and in its threads. Thread states are as the store holds
    them."""
    line = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE chat_id = ? AND role = ?", (chat_id, role))]
    marks = ", ".join("?" * len(line))
    listed = [t for t in tasks.of_chat(conn, chat_id, limit=-1)
              if t.state not in tasks.ENDED or t.updated_at >= since]
    return {
        "goals": [{"id": g.id, "title": g.title, "state": g.state, "ends_at": g.ends_at, "cites": g.cites}
                  for g in goals.of_chat(conn, chat_id) if g.state not in goals.FINAL or g.updated_at >= since],
        "tasks": [{"id": t.id, "thread": t.thread_ts, "title": t.title, "state": t.state, "goal": t.goal_id,
                   "asked_by": t.asked_by, "last_report": t.last_report, "reported_at": t.reported_at,
                   "results": [{"id": r.id, "summary": r.summary, "at": r.created_at,
                                "artifacts": len(json.loads(r.artifacts))} for r in results.of_task(conn, t.id)]}
                  for t in listed],
        "asks": [dict(zip(("hop", "thread", "from", "to", "question", "asked_at", "state"), r)) for r in conn.execute(
            f"SELECT id, thread_ts, from_role, to_role, note, created_at, state FROM hops WHERE kind = 'ask' "
            f"AND state <> 'answered' AND (to_session IN ({marks}) OR from_session IN ({marks})) ORDER BY seq",
            (*line, *line))],
        "items": [dict(zip(("id", "thread", "kind", "risk", "blocking", "title", "asked_at"), r)) for r in conn.execute(
            "SELECT id, thread_ts, kind, risk, blocking, title, created_at FROM review_items WHERE chat_id = ? "
            "AND state = 'open' ORDER BY urgency, seq", (chat_id,))],
        "threads": [{**t, "card": rows.latest_card(conn, chat_id, t["ts"])} for t in rows.list_threads(conn, chat_id)
                    if t["state"] != "resolved" or t["updated_at"] >= since],
        "timers": [{"id": t.id, "kind": t.kind, "at": t.due_at, "every_s": t.every_s, "on": t.event, "note": t.note,
                    "goal": t.goal_id} for t in timers.addressed(conn, chat_id=chat_id, role=role, thread_ts=None)],
        "founder": [rows.message_view(r) for r in conn.execute(
            f"SELECT {rows.MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND author_kind = 'owner' "
            "AND created_at >= ? ORDER BY seq", (chat_id, since))],
    }


def _first_input(conn, session_id: str, *, now: str, since: str) -> dict[str, Any] | None:
    """A successor's handoff, while it has taken no input yet; else None."""
    predecessor, chat_id, role = conn.execute("SELECT predecessor, chat_id, role FROM sessions WHERE id = ?",
                                              (session_id,)).fetchone()
    if predecessor is None or conn.execute("SELECT 1 FROM turns WHERE session_id = ? LIMIT 1",
                                           (session_id,)).fetchone():
        return None
    why = conn.execute("SELECT retired_reason FROM sessions WHERE id = ?", (predecessor,)).fetchone()[0]
    note = conn.execute("SELECT data, ts FROM events WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
                        (predecessor, NOTE_EVENT)).fetchone()
    return {"predecessor": predecessor, "reason": why, "chat": rows.get_chat(conn, chat_id),
            "note": json.loads(note[0])["sections"] if note else None, "note_at": note[1] if note else None,
            "facts": facts(conn, chat_id, role, since=since), "now": now}


class Replacement:
    """``ports.TurnNote`` for coordinators: a nearly full one's next input
    asks for its note; a successor's first input starts with its handoff,
    read from the store (and the chat's memory index from its file) as that
    input starts. Nothing is kept between turns: a successor's handoff goes
    with every input until one of its turns is recorded."""

    async def note(self, engine: Engine, meta: Meta, spec: SessionSpec | None, since: str) -> str | None:
        if not is_coordinator(meta.role) or not meta.chat_id:
            return None
        runtime = engine.runtime(meta.agent)
        features = runtime.features() if runtime is not None else None
        now_s = engine.clock.time()
        now, window_start = iso(now_s), iso(now_s - FACTS_WINDOW_S)

        def read(conn: sqlite3.Connection) -> dict[str, Any] | bool:
            return _first_input(conn, meta.id, now=now, since=window_start) or nearly_full(conn, meta.id, features)
        found = await engine.store.read(read)
        if found is True:
            return f"<system-note>{envelope.escape_text(NEARLY_FULL)}</system-note>"
        if not found:
            return None
        index = (await asyncio.to_thread(memory.read, meta.chat_id))["content"]
        return envelope.handoff(memory_index=index or None, **found)

    def delivered(self, meta: Meta) -> None:
        """Nothing to move: whether a handoff or the ask is due is read from the store."""
