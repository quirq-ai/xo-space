"""Map Space session ids to Grok Bot host agent seats.

``_dispatcher_sse`` always passes the Space-side UUID as ``our_session_id``,
never the host agent id. This file remembers which throwaway (or default)
seat belongs to that UUID so follow-up turns do not mint a new agent and
do not broadcast.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from services.storage.atomic_write import write_json_atomic
from services.storage.reader import read_json
from utils.runtime_env import quirq_state_dir

_MAP_REL = Path("grokbot") / "session-seats.json"


def _map_path() -> Path:
    return quirq_state_dir() / _MAP_REL


def _load() -> dict[str, Any]:
    data = read_json(_map_path())
    if not isinstance(data, dict):
        return {}
    seats = data.get("seats")
    return seats if isinstance(seats, dict) else {}


def lookup_seat(space_session_id: str | None) -> str | None:
    if not space_session_id:
        return None
    value = _load().get(space_session_id)
    return value if isinstance(value, str) and value.strip() else None


def remember_seat(space_session_id: str | None, agent_id: str | None) -> None:
    if not space_session_id or not agent_id:
        return
    seats = dict(_load())
    if seats.get(space_session_id) == agent_id:
        return
    seats[space_session_id] = agent_id
    write_json_atomic(_map_path(), {"seats": seats})
