#!/usr/bin/env python3
"""Storm's audit: the numbers each step of the storm rebuild is measured
against (the plan's §3 targets, the review's §5 table), read from a store.

It never writes. It opens the store read-only (SQLite's ``mode=ro``, then
``query_only``) and refuses the live store's own path: give it a copy made
with SQLite's backup API, which is safe while XO Space runs::

    sqlite3 ~/.quirq/engine/xo.db ".backup /tmp/storm-build/xo.db"
    venv/bin/python scripts/storm_audit.py /tmp/storm-build/xo.db             # JSON
    venv/bin/python scripts/storm_audit.py /tmp/storm-build/xo.db --table     # for people
    venv/bin/python scripts/storm_audit.py /tmp/storm-build/xo.db --since 2026-11-10T12:00:00Z

``--since`` is an ISO time or a duration before now (``24h``, the default;
``90m``, ``7d``). Each measure's ``definitions`` entry says what it counts.
Agents are never named here: an agent is whatever the store's ``agent``
column says.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))       # run as `python scripts/storm_audit.py`: the repo's packages resolve

with warnings.catch_warnings():     # importing the store resolves the active agent, which may warn
    warnings.simplefilter("ignore")
    from services.cowork_agent.storm.records import intents, ledger  # noqa: E402
    from services.cowork_agent.storm.store.db import SCHEMA_VERSION, store_path  # noqa: E402
    from services.cowork_agent.storm.store.retention import (  # noqa: E402
        HELD_TURNS,
        PRUNED_EVENT_FAMILIES,
        RETENTION_S,
    )
from services.timestamps import iso, parse_ts  # noqa: E402

#: The session states in which a coordinator or thread has a process mid-turn.
TURN_IN_PROGRESS = ("starting", "running", "stopping", "waiting_on_person")
#: Roles by kind, as the store names them: the coordinators' roles end so.
COORDINATOR = "%coordinator"

DEFINITIONS = {
    "turns_by_origin": "turns started, by who started them (turns.origin), over all time and since `since`",
    "injected_turns": "turns with origin 'injected', by the kind of session they ran in",
    "wakes_by_reason": "wakes created since `since`, by reason, whatever became of them",
    "answers_stuck_on_retired_sessions": "wakes still queued for a session that was retired (an answer that "
                                         "never reached a coordinator); target 0",
    "threads_working": "threads whose state is 'working'; of them, those whose session has no turn running "
                       "(turns.status 'running'; target 0), and those whose session isn't in a turn's state "
                       f"({', '.join(TURN_IN_PROGRESS)})",
    "sessions": "sessions not retired, by status; `paused` counts those with a pause reason",
    "coordinator_incarnations": "coordinator sessions created since `since`, per chat (each one a replacement "
                                "or a first start)",
    "replacements": "coordinators retired (session events with reason 'retired'), by why",
    "pools": "each pool (agent and account) and its state as the store has it",
    "retention": "events the daily prune would delete as of `as_of` (its own event families and rules), and "
                 "every event in those families",
    "context_tokens_missing": "turns whose usage has no context_tokens (usage missing included), by kind of "
                              "session and agent",
    "intents": "the founder's intents that hold as of `as_of`, by key and scope, and those whose `until` has "
               "passed; null for a store from before schema 3",
    "ledger": "the ledger's entries, and whether it verifies: the chain end to end, and every intent's, goal's, "
              "task's and result's row against its last entry. Verified means no mistake or confused edit was "
              "found; agents can reach the store, so a deliberate rewrite that recomputes the hashes isn't caught. "
              "Null for a store from before schema 3",
    "work": "the founder's goals and the threads' tasks by state, and the results threads reported (recorded as "
            "reported, never judged); null for a store from before schema 3",
}


def open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def since_time(value: str, now: datetime) -> datetime:
    if match := re.fullmatch(r"(\d+)([mhd])", value.strip()):
        unit = {"m": "minutes", "h": "hours", "d": "days"}[match[2]]
        return now - timedelta(**{unit: int(match[1])})
    if (parsed := parse_ts(value)) is None:
        raise argparse.ArgumentTypeError(f"--since {value!r} is neither an ISO time nor a duration like 24h")
    return parsed


def audit(conn: sqlite3.Connection, *, now: datetime, since: datetime) -> dict[str, Any]:
    """Every measure, as of ``now``; ``since`` bounds the recent ones."""
    cut = iso(since)
    q = conn.execute
    head_seq, head_ts = q("SELECT seq, ts FROM events ORDER BY seq DESC LIMIT 1").fetchone() or (0, None)
    kind = (f"CASE WHEN s.role LIKE '{COORDINATOR}' THEN 'coordinator' WHEN s.role = 'thread' THEN 'thread' "
            "ELSE s.role END")
    return {
        "as_of": iso(now),
        "since": cut,
        "store": {
            "schema_version": q("PRAGMA user_version").fetchone()[0],
            "this_build_schema_version": SCHEMA_VERSION,
            "head_seq": head_seq,
            "head_ts": head_ts,
            "events": q("SELECT count(*) FROM events").fetchone()[0],
        },
        "turns_by_origin": {
            "all": _shares(q("SELECT origin, count(*) FROM turns GROUP BY origin").fetchall()),
            "since": _shares(q("SELECT origin, count(*) FROM turns WHERE started_at >= ? GROUP BY origin",
                               (cut,)).fetchall()),
        },
        "injected_turns": {
            "all": _counts(q(f"SELECT {kind}, count(*) FROM turns t JOIN sessions s ON s.id = t.session_id "
                             "WHERE t.origin = 'injected' GROUP BY 1").fetchall()),
            "since": _counts(q(f"SELECT {kind}, count(*) FROM turns t JOIN sessions s ON s.id = t.session_id "
                               "WHERE t.origin = 'injected' AND t.started_at >= ? GROUP BY 1", (cut,)).fetchall()),
        },
        "wakes_by_reason": _counts(q("SELECT reason, count(*) FROM wakes WHERE created_at >= ? GROUP BY reason",
                                     (cut,)).fetchall()),
        "answers_stuck_on_retired_sessions": _stuck(conn),
        "threads_working": _threads_working(conn),
        "sessions": {
            "by_status": _counts(q("SELECT status, count(*) FROM sessions WHERE retired_at IS NULL "
                                   "GROUP BY status").fetchall()),
            "paused": q("SELECT count(*) FROM sessions WHERE retired_at IS NULL AND paused IS NOT NULL").fetchone()[0],
            "retired": q("SELECT count(*) FROM sessions WHERE retired_at IS NOT NULL").fetchone()[0],
        },
        "coordinator_incarnations": _incarnations(conn, cut),
        "replacements": {
            "all": _counts(q("SELECT json_extract(data, '$.why'), count(*) FROM events WHERE type = 'session' "
                             "AND json_extract(data, '$.reason') = 'retired' GROUP BY 1").fetchall()),
            "since": _counts(q("SELECT json_extract(data, '$.why'), count(*) FROM events WHERE type = 'session' "
                               "AND json_extract(data, '$.reason') = 'retired' AND ts >= ? GROUP BY 1",
                               (cut,)).fetchall()),
        },
        "pools": [dict(zip(("id", "state", "resets_at", "updated_at"), row)) for row in q(
            "SELECT id, state, resets_at, updated_at FROM pools ORDER BY id")],
        "retention": _retention(conn, now),
        "context_tokens_missing": {
            "all": _context(conn, kind, None),
            "since": _context(conn, kind, cut),
        },
        **_records(conn, now),
        "definitions": DEFINITIONS,
    }


def _counts(rows: list[tuple]) -> dict[str, int]:
    return {str(key): count for key, count in sorted(rows, key=lambda row: (-row[1], str(row[0])))}


def _shares(rows: list[tuple]) -> dict[str, Any]:
    counts = _counts(rows)
    total = sum(counts.values())
    return {"total": total, "by_origin": counts,
            "share_pct": {k: round(100 * v / total, 1) for k, v in counts.items()} if total else {}}


def _stuck(conn: sqlite3.Connection) -> dict[str, Any]:
    count, oldest, newest = conn.execute(
        "SELECT count(*), min(w.created_at), max(w.created_at) FROM wakes w JOIN sessions s ON s.id = w.session_id "
        "WHERE w.state = 'queued' AND s.retired_at IS NOT NULL").fetchone()
    by_reason = conn.execute(
        "SELECT w.reason, count(*) FROM wakes w JOIN sessions s ON s.id = w.session_id "
        "WHERE w.state = 'queued' AND s.retired_at IS NOT NULL GROUP BY w.reason").fetchall()
    return {"count": count, "by_reason": _counts(by_reason), "oldest": oldest, "newest": newest}


def _threads_working(conn: sqlite3.Connection) -> dict[str, Any]:
    from services.cowork_agent.storm.chats.rows import get_thread
    legacy = any(row[1] == "state" for row in conn.execute("PRAGMA table_info(threads)"))
    where = "WHERE th.state = 'working'" if legacy else ""
    rows = conn.execute(
        "SELECT coalesce(s.status, 'no session'), EXISTS (SELECT 1 FROM turns t WHERE t.session_id = th.session_id "
        "AND t.status = 'running'), th.chat_id, th.ts FROM threads th LEFT JOIN sessions s ON s.id = th.session_id "
        + where).fetchall()
    rows = [(status, running) for status, running, chat, ts in rows
            if legacy or get_thread(conn, chat, ts)["state"] == "working"]
    by_status: dict[str, int] = {}
    for status, _running in rows:
        by_status[status] = by_status.get(status, 0) + 1
    return {"count": len(rows), "with_no_turn_running": sum(1 for _status, running in rows if not running),
            "with_no_session_mid_turn": sum(1 for status, _running in rows if status not in TURN_IN_PROGRESS),
            "by_session_status": _counts(list(by_status.items()))}


def _incarnations(conn: sqlite3.Connection, cut: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT s.chat_id, coalesce(c.project_name, c.kind), count(*), max(s.incarnation) FROM sessions s "
        "LEFT JOIN chats c ON c.id = s.chat_id WHERE s.role LIKE ? AND s.created_at >= ? "
        "GROUP BY s.chat_id ORDER BY 3 DESC, 1", (COORDINATOR, cut)).fetchall()
    return {"total": sum(row[2] for row in rows),
            "per_chat": [{"chat_id": chat, "chat": name, "started": count, "latest_incarnation": latest}
                         for chat, name, count, latest in rows]}


def _retention(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    cutoff = iso(now - timedelta(seconds=RETENTION_S))
    family = " OR ".join("type GLOB ?" for _ in PRUNED_EVENT_FAMILIES)
    patterns = tuple(f"{prefix}*" for prefix in PRUNED_EVENT_FAMILIES)
    total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    in_families = conn.execute(f"SELECT count(*) FROM events WHERE {family}", patterns).fetchone()[0]
    due = conn.execute(
        f"SELECT count(*) FROM events WHERE ({family}) AND ts < ? AND seq < (SELECT max(seq) FROM events) "
        f"AND (turn_id IS NULL OR turn_id NOT IN ({HELD_TURNS}))", (*patterns, cutoff)).fetchone()[0]
    return {"families": list(PRUNED_EVENT_FAMILIES), "kept_days": RETENTION_S // 86400, "events": total,
            "in_pruned_families": in_families,
            "in_pruned_families_pct": round(100 * in_families / total, 1) if total else 0.0,
            "prunable_now": due}


def _context(conn: sqlite3.Connection, kind: str, cut: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {kind}, s.agent, count(*), sum(t.usage IS NULL), "
        "sum(json_extract(t.usage, '$.context_tokens') IS NULL) FROM turns t JOIN sessions s ON s.id = t.session_id "
        f"{'WHERE t.started_at >= ?' if cut else ''} GROUP BY 1, 2 ORDER BY 1, 2", (cut,) if cut else ()).fetchall()
    return [{"kind": k, "agent": agent, "turns": turns, "usage_missing": missing, "context_tokens_null": null,
             "null_pct": round(100 * null / turns, 1)} for k, agent, turns, missing, null in rows]


#: The tables schema 3's records live in.
RECORD_TABLES = ("intents", "ledger", "goals", "tasks", "results")


def _records(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """The founder's intents, the work's records and the ledger; all None
    for a store from before schema 3, which has none of them."""
    marks = ", ".join("?" * len(RECORD_TABLES))
    if conn.execute(f"SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN ({marks})",
                    RECORD_TABLES).fetchone()[0] < len(RECORD_TABLES):
        return {"intents": None, "ledger": None, "work": None}
    live: dict[str, dict[str, int]] = {}
    lapsed = 0
    for intent in intents.load(conn).values():
        if intent.live(now.timestamp()):
            by_scope = live.setdefault(intent.key, {})
            by_scope[intent.scope] = by_scope.get(intent.scope, 0) + 1
        else:
            lapsed += 1
    findings = ledger.verify(conn)
    return {"intents": {"live": {key: _counts(list(by_scope.items())) for key, by_scope in live.items()},
                        "lapsed": lapsed},
            "work": {"goals": _counts(conn.execute("SELECT state, count(*) FROM goals GROUP BY state").fetchall()),
                     "tasks": _counts(conn.execute("SELECT state, count(*) FROM tasks GROUP BY state").fetchall()),
                     "results": conn.execute("SELECT count(*) FROM results").fetchone()[0]},
            "ledger": {"entries": conn.execute("SELECT count(*) FROM ledger").fetchone()[0],
                       "verified": not findings, "findings": [dataclasses.asdict(f) for f in findings]}}


def render(value: Any, indent: int = 0) -> list[str]:
    """The report for people: one line per value, nested by indentation."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(render(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {item if item not in ({}, []) else '-'}")
    elif isinstance(value, list):
        for item in value:
            lines.append(pad + "- " + (", ".join(f"{k}={v}" for k, v in item.items()) if isinstance(item, dict)
                                       else str(item)))
    else:
        lines.append(f"{pad}{value}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=Path, help="a copy of the store (never the live file)")
    parser.add_argument("--since", default="24h", help="ISO time, or a duration before now: 24h (default), 7d")
    parser.add_argument("--table", action="store_true", help="a report for people instead of JSON")
    args = parser.parse_args(argv)
    if not args.store.is_file():
        parser.error(f"{args.store} isn't a file")
    if args.store.resolve() == store_path().resolve():
        parser.error(f"{args.store} is the live store; audit a copy: sqlite3 {args.store} \".backup "
                     "/tmp/storm-build/xo.db\"")
    now = datetime.now(timezone.utc)
    try:
        since = since_time(args.since, now)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    conn = open_read_only(args.store)
    try:
        report = audit(conn, now=now, since=since)
    except sqlite3.Error as exc:
        print(f"storm_audit: {args.store} couldn't be read as a storm store ({exc})", file=sys.stderr)
        return 1
    finally:
        conn.close()
    report["store"]["path"] = str(args.store)
    print("\n".join(render(report)) if args.table else json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
