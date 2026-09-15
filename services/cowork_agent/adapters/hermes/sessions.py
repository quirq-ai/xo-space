"""
Hermes sessions capability.

Hermes owns its messages in per-profile ``state.db`` (no JSONL file). Records
are fetched in the openclaw shape so the shared ``convert_messages`` handles
them unchanged.

Like the other chat backends, hermes writes a row per XO session into the
per-project session index (``sessionslist.py``), so the project-tied scan
applies (``USES_PROJECT_SESSIONS = True``). Sessions that exist only in
hermes' own store (created outside XO chat) are still listed by
``list_native_sessions``; the listing de-duplicates the two by native id.
Every read hook accepts either id: an XO session id resolves to its hermes id
through the index, and a native id is used as-is.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.adapters.hermes.sessionslist import (
    BACKEND,
    find_session_row,
    native_session_id_for,
)
from services.cowork_agent.adapters.hermes.state_db import (
    find_hermes_profile,
    list_hermes_sessions,
    load_hermes_session_records,
    session_title_and_start,
)
from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.engine.messages import convert_messages

# Hermes publishes a row per XO session into the per-project session index.
USES_PROJECT_SESSIONS = True


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """``(time_created, title, effective_agent)`` for a project-tied hermes
    session, read from its profile's state.db; the agent is the owning
    profile, matching how the native listing groups hermes sessions."""
    native = meta.get("nativeSessionId") or meta.get("sessionId") or ""
    if not native:
        return None, None, default_agent
    title, started = session_title_and_start(native)
    profile = find_hermes_profile(native)
    return started, title, profile or default_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Hermes messages live in state.db, not a JSONL file."""
    return None


def list_native_sessions() -> list[dict]:
    """Full session rows from ~/.hermes/state.db + per-profile state.dbs."""
    return list_hermes_sessions()


def owns_session(session_id: str) -> bool:
    """True if some hermes profile's state.db contains this session."""
    return find_hermes_profile(native_session_id_for(session_id)) is not None


def get_messages(session_id: str) -> list:
    """Return converted messages for a hermes session from state.db."""
    return convert_messages(session_id, load_hermes_session_records(native_session_id_for(session_id)))


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Record the selected directory on the session's index row; None if not ours.

    Hermes has no per-request working directory: tools run in the profile's
    ``terminal.cwd`` (set when XO creates the profile for a project), so the
    selection is recorded but not applied to a running gateway.
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
    elif find_hermes_profile(session_id) is None:
        return None
    return {
        "ok": True,
        "session_id": session_id,
        "directory": directory,
        "backend": BACKEND,
        "applied": False,
    }
