"""
OpenClaw sessions capability.

Ownership detection, message reading, and session-directory updates for
openclaw-backed sessions. Resolved generically by the session routes via
``load_capability('sessions', agent=<backend>)`` so no core router names a
backend.

Like the other chat backends, the adapter writes a row per XO session into
the per-project session index (``sessionslist.py``), so the project-tied scan
applies (``USES_PROJECT_SESSIONS = True``). Sessions started outside XO chat
are listed from OpenClaw's own store (``agent_db.py``) and de-duplicated
against those rows. Every read hook accepts either id: an XO session id
resolves through its index row, an OpenClaw transcript id is looked up in
OpenClaw's store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.helpers import derive_title, iso_now, ms_to_iso
from services.cowork_agent.engine.messages import convert_messages
from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.adapters.openclaw.sessionslist import BACKEND, find_session_row

# Rows for XO chats live in the project index; OpenClaw's own store supplies
# the rest, so both scans apply.
USES_PROJECT_SESSIONS = True


def _agent_from_key(key: str, default_agent: str) -> str:
    """OpenClaw session keys look like ``agent:<agent>:...``; the agent id is
    the second segment when present, else the given default."""
    return agent_db.agent_from_session_key(key) or default_agent


def _title_and_start(agent_id: str, native_session_id: str) -> tuple[Optional[str], Optional[str]]:
    try:
        records = agent_db.read_conversation(agent_id, native_session_id)
        if not records:
            return None, None
        ts = records[0].get("timestamp")
        return derive_title(records), ts if isinstance(ts, str) and ts else None
    except Exception:
        return None, None


def _locate(session_id: str) -> Optional[tuple[str, str]]:
    """``(openclaw agent, transcript id)`` for an XO or OpenClaw session id."""
    found = find_session_row(session_id)
    if found is not None:
        _project_id, key, meta = found
        native = meta.get("nativeSessionId") or agent_db.session_id_for_key(key)
        if not native:
            return None
        agent = agent_db.agent_from_session_key(key)
        if agent is None:
            located = agent_db.find_session(native)
            agent = located[0] if located else None
        return (agent, native) if agent else None
    located = agent_db.find_session(session_id)
    return (located[0], session_id) if located else None


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` for a project-tied
    openclaw session. Messages live in OpenClaw's store; the effective agent
    comes from the session key."""
    oc_agent = _agent_from_key(key, default_agent)
    native = meta.get("nativeSessionId") or ""
    if not native:
        return None, None, oc_agent
    title, time_created = _title_and_start(oc_agent, native)
    return time_created, title, oc_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """The transcript file of a session on an agent without a database;
    None when OpenClaw keeps it in SQLite."""
    native = meta.get("nativeSessionId") or session_id
    if not native or "/" in native or "\\" in native:
        return None
    for agent_id in agent_db.agent_ids():
        if agent_db.has_database(agent_id):
            continue
        path = agent_db.legacy_sessions_dir(agent_id) / f"{native}.jsonl"
        if path.is_file():
            return path
    return None


def list_native_sessions() -> list[dict]:
    """Full session rows for OpenClaw sessions XO chat didn't start, read
    from each agent's store. Caller de-duplicates by id."""
    xo_keys = {
        key
        for _project_id, _project_dir, index in _session_index.iter_session_indexes()
        for key, meta in index.items()
        if meta.get("backend") == BACKEND
    }
    rows: list[dict] = []
    for agent_id in agent_db.agent_ids():
        for info in agent_db.list_sessions(agent_id):
            if info.session_key in xo_keys:
                continue
            time_updated = ms_to_iso(info.updated_ms) if info.updated_ms else iso_now()
            time_created = ms_to_iso(info.created_ms) if info.created_ms else None
            title = info.title
            if not title or not time_created:
                derived_title, first_ts = _title_and_start(agent_id, info.session_id)
                title = title or derived_title
                time_created = time_created or first_ts
            rows.append({
                "id": info.session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": _agent_from_key(info.session_key, agent_id),
                "directory": "",
                "title": title or "Untitled Session",
                "version": 1,
                "summary_additions": 0,
                "summary_deletions": 0,
                "summary_files": 0,
                "summary_diffs": [],
                "is_pinned": False,
                "permission": {},
                "time_created": time_created or time_updated,
                "time_updated": time_updated,
                "time_compacting": None,
                "time_archived": None,
            })
    return rows


def owns_session(session_id: str) -> bool:
    """True if this is an OpenClaw session: an XO chat row, or a transcript in OpenClaw's store."""
    return find_session_row(session_id) is not None or agent_db.find_session(session_id) is not None


def get_messages(session_id: str) -> list:
    """Return converted messages for an openclaw session (empty if unknown)."""
    located = _locate(session_id)
    if not located:
        return []
    agent_id, native = located
    return convert_messages(session_id, agent_db.read_conversation(agent_id, native))


def find_session_key(session_id: str) -> str | None:
    """Look up the openclaw session key for an XO or OpenClaw session id."""
    found = find_session_row(session_id)
    if found is not None:
        return found[1]
    located = agent_db.find_session(session_id)
    return located[1] if located else None


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Record the selected directory on the session's index row; None if not ours.

    OpenClaw has no per-request working directory (an agent's tools run in its
    configured workspace), so the selection is recorded but not applied to the
    running gateway. A session with no XO row has nowhere to record it.
    """
    found = find_session_row(session_id)
    if found is not None:
        project_id, key, meta = found
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        row = dict(meta)
        history = list(row.get("directoryHistory") or [])
        history.append({"directory": directory, "selectedAt": now_ms})
        row["directoryHistory"] = history[-200:]
        row["directory"] = directory
        row["updatedAt"] = now_ms
        _session_index.write_session_row(project_id, key, row)
    elif agent_db.find_session(session_id) is None:
        return None
    return {"ok": True, "session_id": session_id, "directory": directory}
