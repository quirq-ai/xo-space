"""Persist Space-to-host identity in the shared, per-session index."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.grokbot.paths import resolve_sand_root
from services.cowork_agent.engine import sessions_io
from services.storage.reader import read_json
from utils.runtime_env import quirq_state_dir

_MAP_REL = Path("grokbot") / "session-seats.json"


def _map_path() -> Path:
    return quirq_state_dir() / _MAP_REL


def _legacy_seats() -> dict[str, Any]:
    data = read_json(_map_path())
    if not isinstance(data, dict):
        return {}
    seats = data.get("seats")
    return seats if isinstance(seats, dict) else {}


def indexed_sessions() -> dict[str, dict]:
    return {
        row["sessionId"]: row
        for row in sessions_io.read_root_session_index().values()
        if row.get("backend") == "grokbot" and row.get("sessionId")
    }


def lookup_seat(space_session_id: str | None) -> str | None:
    if not space_session_id:
        return None
    row = indexed_sessions().get(space_session_id, {})
    value = row.get("nativeSessionId") or _legacy_seats().get(space_session_id)
    return value if isinstance(value, str) and value.strip() else None


def remember_seat(space_session_id: str | None, agent_id: str | None) -> None:
    if not space_session_id or not agent_id:
        return
    # One atomic shard per Space session: simultaneous new chats cannot
    # overwrite each other's mapping. Old private maps are read-only fallback.
    row = {
        "sessionId": space_session_id,
        "nativeSessionId": agent_id,
        "directory": str(resolve_sand_root()),
        "backend": "grokbot",
        "updatedAt": int(time.time() * 1000),
    }
    sessions_io.write_session_row("", f"grokbot::web:{space_session_id}", row)
