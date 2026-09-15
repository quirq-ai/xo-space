"""
Read-only access to Hermes's session state, across every profile.

Hermes (CLI, gateway, and api_server) writes every session and every message
to a SQLite WAL database. Profiles are independent workspaces:

- The implicit ``default`` profile maps to the flat layout — ``~/.hermes/state.db``.
- Custom profiles live under ``~/.hermes/profiles/<name>/state.db``.

We surface sessions from *every* profile at once so the xo-cowork sidebar
can group them by profile name (the same way openclaw groups by agent id).

Schema reference: ``~/.hermes/hermes-agent/hermes_state.py`` (SCHEMA_VERSION 6+).
Connections are opened read-only (``mode=ro`` URI) so we never contend with
the gateway's WAL write lock. Rows that fail to map are skipped, so an
upstream schema bump degrades gracefully instead of 500-ing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.helpers import iso_now
from services.cowork_agent.adapters.hermes.paths import HERMES_DIR, HERMES_PROFILES_DIR


_DEFAULT_PROFILE = "default"
_TITLE_FALLBACK = "Untitled Session"

# ── In-flight exchange cache ──────────────────────────────────────────────────
#
# Hermes commits messages to state.db ~3-10 s after the chat stream ends. In
# that window the frontend refetches /api/messages/<sid> and gets an empty
# list — the just-completed chat appears to vanish until the user types
# something else (which forces another refetch after commit lag passes).
#
# We bridge that gap with a small in-memory cache: the hermes adapter calls
# ``register_inflight_exchange`` when a stream completes; ``load_hermes_session_records``
# falls back to the cache if state.db has fewer records than the cache holds.
# Entries expire ``_INFLIGHT_TTL`` seconds after the last write — well past
# any realistic commit lag — so the cache never serves stale data once
# state.db owns the truth.

_INFLIGHT_TTL = 60.0  # seconds
_inflight_lock = threading.Lock()
_inflight_records: dict[str, list[dict[str, Any]]] = {}
_inflight_expires_at: dict[str, float] = {}


def _profile_state_dbs() -> list[tuple[str, Path]]:
    """Return ``[(profile_name, state_db_path), ...]`` for every profile with a state.db.

    Order: ``default`` first if present, then custom profiles alphabetically.
    Profiles without a state.db (freshly created, no sessions yet) are skipped
    so SQLite queries don't try to open files that don't exist. Use
    ``list_all_profile_names()`` when you need every profile regardless of
    whether it has chat history yet (e.g. sidebar agent listing).
    """
    out: list[tuple[str, Path]] = []
    default_db = HERMES_DIR / "state.db"
    if default_db.is_file():
        out.append((_DEFAULT_PROFILE, default_db))
    if HERMES_PROFILES_DIR.is_dir():
        for entry in sorted(p for p in HERMES_PROFILES_DIR.iterdir() if p.is_dir()):
            db = entry / "state.db"
            if db.is_file():
                out.append((entry.name, db))
    return out


def list_all_profile_names() -> list[str]:
    """Return every hermes profile name, regardless of whether it has a state.db.

    Used by ``/api/agents`` so a freshly-created profile (via ``hermes profile
    create`` / ``POST /api/agents``) shows up in the sidebar immediately,
    even before the user has had their first chat in it.

    Order matches ``_profile_state_dbs``: ``default`` first if the home dir
    exists, then custom profiles alphabetically.
    """
    names: list[str] = []
    if HERMES_DIR.is_dir():
        names.append(_DEFAULT_PROFILE)
    if HERMES_PROFILES_DIR.is_dir():
        for entry in sorted(p for p in HERMES_PROFILES_DIR.iterdir() if p.is_dir()):
            names.append(entry.name)
    return names


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection so we never contend with the gateway."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _epoch_to_iso(ts: float | None) -> str:
    if ts is None:
        return iso_now()
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return iso_now()


# ── Public surface ────────────────────────────────────────────────────────────


def list_hermes_sessions() -> list[dict[str, Any]]:
    """Return all hermes sessions across every profile, in openclaw session-dict shape.

    Each entry mirrors the dict produced by ``sessions_io.load_all_sessions``
    so the caller can merge results without reshaping. ``agent`` is set to
    the profile name — one sidebar bucket per profile.
    """
    # Hide stale orphans: hermes creates the sessions row at request-start and
    # commits the first user message ~3-10 seconds later. A fresh row has
    # message_count=0 and that's expected briefly. Anything older than 60 s
    # with no messages is a never-completed chat (e.g. dev tests, aborted
    # requests) — drop those so the sidebar isn't littered with "Untitled
    # Session" stubs. Recent zero-count rows stay so a just-started chat
    # appears in the sidebar immediately.
    import time as _time
    orphan_cutoff = _time.time() - 60.0

    sessions: list[dict[str, Any]] = []
    for profile, db_path in _profile_state_dbs():
        try:
            with _ro_connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, title, started_at, ended_at, message_count
                    FROM sessions
                    WHERE message_count > 0 OR started_at >= ?
                    ORDER BY started_at DESC
                    """,
                    (orphan_cutoff,),
                ).fetchall()
        except sqlite3.Error:
            continue

        for row in rows:
            session_id = row["id"]
            if not session_id:
                continue

            title = (row["title"] or "").strip() or _derive_title_from_first_message(
                db_path, session_id
            ) or _TITLE_FALLBACK

            time_created = _epoch_to_iso(row["started_at"])
            time_updated = _epoch_to_iso(row["ended_at"]) if row["ended_at"] else time_created

            sessions.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": profile,
                "directory": "",
                "title": title,
                "version": 1,
                "summary_additions": 0,
                "summary_deletions": 0,
                "summary_files": 0,
                "summary_diffs": [],
                "is_pinned": False,
                "permission": {},
                "time_created": time_created,
                "time_updated": time_updated,
                "time_compacting": None,
                "time_archived": None,
            })

    return sessions


def find_hermes_profile(session_id: str) -> str | None:
    """Return the profile name that owns ``session_id``, or None.

    Cheap lookup used by ``sessions_io.find_session_backend`` to decide
    whether the hermes branch applies.
    """
    if not session_id:
        return None
    for profile, db_path in _profile_state_dbs():
        try:
            with _ro_connect(db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return profile
    return None


def session_title_and_start(session_id: str) -> tuple[str | None, str | None]:
    """``(title, started_at ISO)`` for one hermes session, or ``(None, None)``.

    Lets a project-index row for a hermes session show the same title and
    creation time the native listing above does.
    """
    profile = find_hermes_profile(session_id)
    if profile is None:
        return None, None
    db_path = HERMES_DIR / "state.db" if profile == _DEFAULT_PROFILE else HERMES_PROFILES_DIR / profile / "state.db"
    try:
        with _ro_connect(db_path) as conn:
            row = conn.execute(
                "SELECT title, started_at FROM sessions WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    title = (row["title"] or "").strip() or _derive_title_from_first_message(db_path, session_id)
    return title, _epoch_to_iso(row["started_at"])


def load_hermes_session_records(session_id: str) -> list[dict[str, Any]]:
    """Return the session's messages as openclaw-shaped JSONL records.

    The records are consumable by ``services.cowork_agent.engine.messages.convert_messages``
    unchanged — the unified messaging layer stays the single source of truth
    for the frontend ``MessageResponse`` shape.

    Maps hermes ``messages`` rows:
      role=user      → ``{message: {role: user,      content: [{type:text, text:...}]}}``
      role=assistant → ``{message: {role: assistant, content: [thinking?, text?, toolCall*], model, stopReason}}``
      role=tool      → ``{message: {role: toolResult, toolCallId, content: [{type:text, text:...}], isError: False}}``
      role=session_meta → skipped

    During the 3-10 s window between stream end and state.db commit we
    fall back to the in-flight cache so the frontend doesn't see an empty
    chat. Once state.db has *at least as many records* as the cache, the
    cache yields and state.db wins.
    """
    if not session_id:
        return []

    profile = find_hermes_profile(session_id)
    cached = _get_inflight_records(session_id)

    if profile is None:
        # State.db doesn't know about this session yet (commit hasn't even
        # created the sessions row). Serve the in-flight cache if we have one.
        return cached

    db_path = HERMES_DIR / "state.db" if profile == _DEFAULT_PROFILE else HERMES_PROFILES_DIR / profile / "state.db"

    try:
        with _ro_connect(db_path) as conn:
            session_row = conn.execute(
                "SELECT model FROM sessions WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            session_model = session_row["model"] if session_row else None

            rows = conn.execute(
                """
                SELECT id, role, content, tool_call_id, tool_calls,
                       tool_name, timestamp, finish_reason, reasoning
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp, id
                """,
                (session_id,),
            ).fetchall()
    except sqlite3.Error:
        return cached

    records: list[dict[str, Any]] = []
    for row in rows:
        record = _row_to_openclaw_record(row, session_model)
        if record is not None:
            records.append(record)

    # State.db has caught up if it has at least as many message records as
    # the cache; otherwise the commit is still in flight and we serve cached.
    if cached and len(records) < len(cached):
        return cached
    return records


def register_inflight_exchange(
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    model: str | None = None,
) -> None:
    """Record a just-completed user/assistant exchange for ``session_id``.

    Called by the hermes adapter as soon as a stream ends. Bridges the
    commit-lag window so ``/api/messages`` doesn't return an empty list
    for a chat that *just* happened. Records expire after ``_INFLIGHT_TTL``
    seconds — longer than any observed commit lag, short enough that stale
    cache entries can't outlive state.db's truth.
    """
    if not session_id or (not user_text and not assistant_text):
        return

    now = time.time()
    new_records: list[dict[str, Any]] = []
    if user_text:
        new_records.append({
            "type": "message",
            "id": f"inflight_{session_id}_user_{int(now * 1000)}",
            "timestamp": _epoch_to_iso(now),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            },
        })
    if assistant_text:
        new_records.append({
            "type": "message",
            "id": f"inflight_{session_id}_asst_{int(now * 1000)}",
            "timestamp": _epoch_to_iso(now + 0.001),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
                "model": model,
                "stopReason": "stop",
            },
        })

    with _inflight_lock:
        _inflight_records.setdefault(session_id, []).extend(new_records)
        _inflight_expires_at[session_id] = now + _INFLIGHT_TTL
        # Opportunistically GC expired entries while we hold the lock.
        for sid in list(_inflight_expires_at):
            if _inflight_expires_at[sid] < now:
                _inflight_records.pop(sid, None)
                _inflight_expires_at.pop(sid, None)


def _get_inflight_records(session_id: str) -> list[dict[str, Any]]:
    """Return a copy of cached records for ``session_id``, or [] if none/expired."""
    now = time.time()
    with _inflight_lock:
        expires = _inflight_expires_at.get(session_id, 0.0)
        if expires < now:
            _inflight_records.pop(session_id, None)
            _inflight_expires_at.pop(session_id, None)
            return []
        return list(_inflight_records.get(session_id, []))


# ── Internal helpers ──────────────────────────────────────────────────────────


def _derive_title_from_first_message(db_path: Path, session_id: str) -> str | None:
    """Use the first user message as a fallback title when sessions.title is NULL."""
    try:
        with _ro_connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT content FROM messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY timestamp, id
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["content"]:
        return None
    text = str(row["content"]).strip().splitlines()[0] if row["content"] else ""
    if len(text) > 60:
        text = text[:57].rstrip() + "..."
    return text or None


def _row_to_openclaw_record(row: sqlite3.Row, session_model: str | None) -> dict[str, Any] | None:
    """Map one hermes ``messages`` row to an openclaw-shaped JSONL record."""
    role = row["role"]
    if role == "session_meta":
        return None

    record_id = f"hermes_{row['id']}"
    timestamp = _epoch_to_iso(row["timestamp"])
    content_text = row["content"] or ""

    if role == "user":
        if not content_text.strip():
            return None
        return {
            "type": "message",
            "id": record_id,
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": content_text}],
            },
        }

    if role == "assistant":
        content_blocks: list[dict[str, Any]] = []

        reasoning_text = (row["reasoning"] or "").strip()
        if reasoning_text:
            content_blocks.append({"type": "thinking", "thinking": reasoning_text})

        if content_text.strip():
            content_blocks.append({"type": "text", "text": content_text})

        tool_calls_raw = row["tool_calls"]
        if tool_calls_raw:
            try:
                tool_calls = json.loads(tool_calls_raw)
            except (TypeError, ValueError):
                tool_calls = []
            for tc in tool_calls if isinstance(tool_calls, list) else []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments")
                if isinstance(args_raw, str):
                    try:
                        arguments = json.loads(args_raw)
                    except (TypeError, ValueError):
                        arguments = {"_raw": args_raw}
                elif isinstance(args_raw, dict):
                    arguments = args_raw
                else:
                    arguments = {}
                content_blocks.append({
                    "type": "toolCall",
                    "name": fn.get("name") or tc.get("name") or "unknown",
                    "id": tc.get("id") or tc.get("call_id") or "",
                    "arguments": arguments,
                })

        if not content_blocks:
            return None

        return {
            "type": "message",
            "id": record_id,
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": content_blocks,
                "model": session_model,
                "stopReason": row["finish_reason"],
            },
        }

    if role == "tool":
        return {
            "type": "message",
            "id": record_id,
            "timestamp": timestamp,
            "message": {
                "role": "toolResult",
                "toolCallId": row["tool_call_id"] or "",
                "content": [{"type": "text", "text": content_text}],
                "isError": False,
            },
        }

    return None
