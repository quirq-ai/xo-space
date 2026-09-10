"""Machine-local union of every project's open sessions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.state import (
    project_activity_path,
    workspace_activity_path,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids


# The payload this process last wrote, per target path — the write-on-change
# baseline (syncplan §3: in memory, not a re-read, because this sink owns the
# whole document).
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _previous.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_sort_key(row: dict) -> tuple[str, str, str]:
    """Total order over the union (T24)."""
    return (
        str(row.get("project_id", "")),
        str(row.get("session_id", "")),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26)."""
    open_sessions: list[dict] = []
    for pid in (project_ids if project_ids is not None else list_project_ids()):
        act = read_json(project_activity_path(pid))
        if not isinstance(act, dict):
            continue
        for s in act.get("open_sessions") or []:
            if isinstance(s, dict):
                tagged = dict(s)
                tagged["project_id"] = pid
                open_sessions.append(tagged)

    open_sessions.sort(key=_row_sort_key)

    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "open_sessions": open_sessions,
    }
    target = workspace_activity_path()
    key = str(target)
    if key in _previous and target.exists():
        # Steady state: one ``stat`` and a dict comparison, no read.
        changed = write_json_atomic_if_changed(
            target, payload, ("updated_at",), previous=_previous[key]
        )
    else:
        # No baseline yet (first tick of the process), or the file was removed
        # underneath us — ``rm -rf ~/.quirq`` is a documented clean reset
        # (syncplan §4) and must repopulate on the next tick, not on the next
        # content change.
        changed = write_json_atomic_if_changed(target, payload, ("updated_at",))
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = payload
    return changed
