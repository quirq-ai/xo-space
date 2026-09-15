"""
Hermes rows in the per-project session index, written the way the other chat
backends write theirs.

XO's session id is also the hermes session id: hermes' api_server takes a
client-supplied ``X-Hermes-Session-Id`` and creates the session under it
(``gateway/platforms/api_server_openai_routes.py`` in hermes-agent), the same
way claude_code pre-allocates ``--session-id``. So the row is written before
the request with ``nativeSessionId`` already known, and a cancelled stream can
never orphan the mapping. Rows written earlier carry a hermes-derived
``nativeSessionId``; they are honoured as-is.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.engine import sessions_io as _session_index

logger = logging.getLogger(__name__)

BACKEND = "hermes"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def make_session_key(agent_id: str, our_session_id: str) -> str:
    """``hermes:<agent_id>:<surface>:<8hex>``. The suffix comes from XO's
    session id, so repeated writes for one session land on one row."""
    suffix = our_session_id.replace("-", "")[:8] if our_session_id else "00000000"
    return f"{BACKEND}:{agent_id}:web:{suffix}"


def agent_id_from_key(session_key: str) -> str:
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else "default"


def find_session_row(session_id: str) -> Optional[tuple[str, str, dict]]:
    """``(project_id, session_key, row)`` of the hermes row for XO session
    ``session_id`` (any project, or the no-project scope), or None."""
    if not session_id:
        return None
    for project_id, _project_dir, index in _session_index.iter_session_indexes():
        for key, meta in index.items():
            if meta.get("backend") == BACKEND and meta.get("sessionId") == session_id:
                return project_id, key, meta
    return None


def native_session_id_for(session_id: str) -> str:
    """The hermes (state.db) id for an XO session id. A session listed from
    hermes' own store has no row, and its id already is the native id."""
    found = find_session_row(session_id)
    if found:
        native = found[2].get("nativeSessionId")
        if isinstance(native, str) and native:
            return native
    return session_id


def write_preliminary_entry(
    session_key: str, session_id: str, native_session_id: str, directory: str
) -> bool:
    """Write the row BEFORE the request, like the CLI backends do."""
    row = {
        "sessionId": session_id,
        "nativeSessionId": native_session_id,
        "directory": directory,
        "backend": BACKEND,
        "updatedAt": _now_ms(),
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    try:
        return _session_index.write_session_row(agent_id_from_key(session_key), session_key, row)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for an index write
        logger.warning("hermes session index write failed: %s", exc)
        return False


def touch_session_row(
    project_id: str, session_key: str, native_session_id: Optional[str]
) -> None:
    """Bump ``updatedAt`` after a turn, filling ``nativeSessionId`` if empty."""
    try:
        meta = dict(_session_index.read_session_index(project_id).get(session_key) or {})
        if not meta:
            return
        if native_session_id and not meta.get("nativeSessionId"):
            meta["nativeSessionId"] = native_session_id
        meta["updatedAt"] = _now_ms()
        _session_index.write_session_row(project_id, session_key, meta)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for an index write
        logger.warning("hermes session index update failed: %s", exc)
