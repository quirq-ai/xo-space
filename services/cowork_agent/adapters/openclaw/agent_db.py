"""
OpenClaw's session store, read-only.

Current OpenClaw keeps each agent's sessions in one SQLite database,
``~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`` (schema:
``src/state/openclaw-agent-schema.sql`` in openclaw):

* ``session_nodes``: one row per session key; ``current_session_id`` names
  the live transcript generation.
* ``session_windows``: one row per transcript generation (``session_id``)
  under its ``session_key``; a reset or rollover opens a new one.
* ``transcript_events``: the transcript, ``event_json`` per ``seq``, in the
  record shape the JSONL files used (a ``type: "session"`` header, then
  ``type: "message"`` records carrying ``message.role/content/usage``).

Releases before that move kept ``~/.openclaw/agents/<id>/sessions/sessions.json``
(session key -> ``{sessionId, updatedAt}``) and one ``<sessionId>.jsonl`` per
session. An agent without a database is read from those files, so a pinned
older gateway keeps working.

Transcript generations OpenClaw has moved to its archive tables after a reset
or delete (``session_transcript_archives``) are not read.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR

AGENT_DB_NAME = "openclaw-agent.sqlite"
_LOCATOR_SEP = "#"


@dataclass(frozen=True)
class SessionInfo:
    agent_id: str
    session_key: str
    session_id: str
    created_ms: Optional[int]
    updated_ms: Optional[int]
    title: Optional[str]


# ── Layout ────────────────────────────────────────────────────────────────────


def agent_ids() -> list[str]:
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(d.name for d in AGENTS_DIR.iterdir() if d.is_dir())


def agent_db_path(agent_id: str) -> Path:
    return AGENTS_DIR / agent_id / "agent" / AGENT_DB_NAME


def legacy_sessions_dir(agent_id: str) -> Path:
    return AGENTS_DIR / agent_id / "sessions"


def has_database(agent_id: str) -> bool:
    return agent_db_path(agent_id).is_file()


def session_store_path(agent_id: str) -> Path:
    """Where the agent's sessions are recorded: its database, or the legacy index."""
    if has_database(agent_id):
        return agent_db_path(agent_id)
    return legacy_sessions_dir(agent_id) / "sessions.json"


def agent_from_session_key(session_key: str) -> Optional[str]:
    """OpenClaw session keys look like ``agent:<agentId>:<rest>``."""
    parts = (session_key or "").split(":")
    if len(parts) >= 3 and parts[0] == "agent" and parts[1]:
        return parts[1]
    return None


# ── Low-level reads ───────────────────────────────────────────────────────────


def _query(agent_id: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run one read-only query against an agent database; [] on any SQLite
    error (missing table, busy file, schema drift) so a listing never fails."""
    uri = f"{agent_db_path(agent_id).resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _decode_event(raw: Any) -> Optional[dict]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _safe_legacy_id(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and "\\" not in session_id


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                event = _decode_event(line.strip())
                if event is not None:
                    records.append(event)
    except OSError:
        return []
    return records


def _legacy_index(agent_id: str) -> dict:
    try:
        data = json.loads((legacy_sessions_dir(agent_id) / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ── Sessions ──────────────────────────────────────────────────────────────────


def list_sessions(agent_id: str) -> list[SessionInfo]:
    """The agent's sessions, one per session key (archived ones excluded)."""
    if has_database(agent_id):
        rows = _query(
            agent_id,
            "SELECT session_key, current_session_id, created_at, updated_at, label, display_name"
            " FROM session_nodes WHERE archived_at IS NULL",
        )
        return [
            SessionInfo(
                agent_id=agent_id,
                session_key=row["session_key"],
                session_id=row["current_session_id"],
                created_ms=_int_or_none(row["created_at"]),
                updated_ms=_int_or_none(row["updated_at"]),
                title=row["label"] or row["display_name"] or None,
            )
            for row in rows
            if row["current_session_id"]
        ]
    sessions: list[SessionInfo] = []
    for key, meta in _legacy_index(agent_id).items():
        if not isinstance(meta, dict):
            continue
        session_id = meta.get("sessionId")
        if isinstance(session_id, str) and session_id:
            sessions.append(
                SessionInfo(agent_id, key, session_id, None, _int_or_none(meta.get("updatedAt")), None)
            )
    return sessions


def session_id_for_key(session_key: str) -> Optional[str]:
    """OpenClaw's current transcript id for a session key, once the gateway
    has created the session."""
    owner = agent_from_session_key(session_key)
    for aid in [owner] if owner else agent_ids():
        if has_database(aid):
            rows = _query(
                aid,
                "SELECT current_session_id FROM session_nodes WHERE session_key = ? LIMIT 1",
                (session_key,),
            )
            if rows and rows[0]["current_session_id"]:
                return rows[0]["current_session_id"]
            continue
        meta = _legacy_index(aid).get(session_key)
        if isinstance(meta, dict) and isinstance(meta.get("sessionId"), str) and meta["sessionId"]:
            return meta["sessionId"]
    return None


def find_session(session_id: str) -> Optional[tuple[str, Optional[str]]]:
    """``(agent_id, session_key)`` of the agent whose store holds transcript
    ``session_id``, or None. The key is None for a legacy transcript file the
    agent's index no longer lists."""
    if not session_id:
        return None
    for aid in agent_ids():
        if has_database(aid):
            rows = _query(
                aid,
                "SELECT session_key FROM session_windows WHERE session_id = ? LIMIT 1",
                (session_id,),
            )
            if rows:
                return aid, rows[0]["session_key"]
            continue
        if _safe_legacy_id(session_id) and (legacy_sessions_dir(aid) / f"{session_id}.jsonl").is_file():
            key = next(
                (
                    k
                    for k, m in _legacy_index(aid).items()
                    if isinstance(m, dict) and m.get("sessionId") == session_id
                ),
                None,
            )
            return aid, key
    return None


# ── Transcripts ───────────────────────────────────────────────────────────────


def list_generations(agent_id: str, session_key: str) -> list[str]:
    """Transcript ids recorded under a session key, oldest first."""
    if has_database(agent_id):
        rows = _query(
            agent_id,
            "SELECT session_id FROM session_windows WHERE session_key = ? ORDER BY created_at, session_id",
            (session_key,),
        )
        return [row["session_id"] for row in rows]
    meta = _legacy_index(agent_id).get(session_key)
    if isinstance(meta, dict) and isinstance(meta.get("sessionId"), str) and meta["sessionId"]:
        return [meta["sessionId"]]
    return []


def read_generation(agent_id: str, session_id: str) -> list[dict]:
    """One transcript generation's records, in order."""
    if has_database(agent_id):
        rows = _query(
            agent_id,
            "SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return [e for e in (_decode_event(r["event_json"]) for r in rows) if e is not None]
    if not _safe_legacy_id(session_id):
        return []
    return _read_jsonl(legacy_sessions_dir(agent_id) / f"{session_id}.jsonl")


def read_generation_after(
    agent_id: str, session_id: str, after_seq: int, limit: int
) -> list[tuple[int, Optional[dict]]]:
    """``(seq, record)`` rows of a database generation past ``after_seq``; the
    record is None for a row that doesn't decode, so offsets still advance."""
    rows = _query(
        agent_id,
        "SELECT seq, event_json FROM transcript_events WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (session_id, after_seq, limit),
    )
    return [(int(r["seq"]), _decode_event(r["event_json"])) for r in rows]


def read_conversation(agent_id: str, session_id: str) -> list[dict]:
    """Every generation of the conversation ``session_id`` belongs to (all
    windows of its session key), oldest first."""
    if has_database(agent_id):
        rows = _query(
            agent_id,
            "SELECT e.event_json FROM transcript_events e"
            " JOIN session_windows w ON w.session_id = e.session_id"
            " WHERE w.session_key = (SELECT session_key FROM session_windows WHERE session_id = ?)"
            " ORDER BY w.created_at, e.session_id, e.seq",
            (session_id,),
        )
        return [e for e in (_decode_event(r["event_json"]) for r in rows) if e is not None]
    return read_generation(agent_id, session_id)


# ── Usage discovery ───────────────────────────────────────────────────────────
#
# The usage contract discovers sessions as path strings. A database generation
# is addressed as ``<database path>#<session_id>``.


def list_generation_ids(agent_id: str) -> list[str]:
    rows = _query(agent_id, "SELECT session_id FROM session_windows ORDER BY session_id")
    return [row["session_id"] for row in rows]


def generation_updated_ms(agent_id: str, session_id: str) -> Optional[int]:
    rows = _query(
        agent_id,
        "SELECT COALESCE(transcript_updated_at, updated_at) AS ts FROM session_windows WHERE session_id = ? LIMIT 1",
        (session_id,),
    )
    return _int_or_none(rows[0]["ts"]) if rows else None


def generation_locator(agent_id: str, session_id: str) -> str:
    return f"{agent_db_path(agent_id)}{_LOCATOR_SEP}{session_id}"


def parse_generation_locator(locator: str) -> Optional[tuple[str, str]]:
    """``(agent_id, session_id)`` for a locator from :func:`generation_locator`."""
    db, sep, session_id = str(locator).rpartition(_LOCATOR_SEP)
    if not sep or not session_id:
        return None
    path = Path(db)
    if path.name != AGENT_DB_NAME:
        return None
    return path.parent.parent.name, session_id
