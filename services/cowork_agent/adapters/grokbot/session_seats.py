"""Persist Space-to-host identity in the shared, per-session index."""
from __future__ import annotations

import time

from services.cowork_agent.adapters.grokbot.paths import resolve_sand_root
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.helpers import strip_workspace_preamble


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
    value = row.get("nativeSessionId")
    return value if isinstance(value, str) and value.strip() else None


def remember_seat(
    space_session_id: str | None, agent_id: str | None, *, question: str = "",
) -> None:
    if not space_session_id or not agent_id:
        return
    # One atomic shard per Space session: simultaneous new chats cannot
    # overwrite each other's mapping.
    previous = indexed_sessions().get(space_session_id, {})
    now = int(time.time() * 1000)
    title = strip_workspace_preamble(question).strip()
    row = {
        **previous,
        "sessionId": space_session_id,
        "nativeSessionId": agent_id,
        "directory": str(resolve_sand_root()),
        "backend": "grokbot",
        "createdAt": previous.get("createdAt") or previous.get("updatedAt") or now,
        "updatedAt": now,
        "title": previous.get("title") or title[:80] or "Untitled Session",
    }
    # Project selection is not forwarded; these seats use the project-less index.
    sessions_io.write_session_row("", f"grokbot::web:{space_session_id}", row)
