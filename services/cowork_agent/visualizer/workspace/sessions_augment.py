"""
``~/.quirq/workspace/sessions/sessions-augment.json`` — union of every
project's per-project augment file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from services.cowork_agent.project_layout import runtime_read_path, workspace_sessions_dir
from services.cowork_agent.visualizer.atomic_write import ChangeGate
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.todos_store import PROJECT_SESSION
from services.cowork_agent.visualizer.workspace_index import (
    list_project_ids,
    list_project_pids,
)


# Relative to the project's runtime root (and, for the read-through, to its
# pre-move ``.xo/``). The tier decision itself is project_layout's.
_AUGMENT_RELATIVE = "sessions/sessions-augment.json"


_gate = ChangeGate()


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _gate.reset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _union_key(key: str, name: str, pid: str | None) -> str:
    """The key this row takes in the union."""
    if key != PROJECT_SESSION:
        return key
    return f"{pid or name}/{key}"


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26)."""
    sessions: dict[str, dict] = {}
    # ``project_ids`` are directory NAMES — that is what every path helper
    # takes, and what ``list_project_ids`` returns.
    names = project_ids if project_ids is not None else list_project_ids()
    pids = list_project_pids()
    for name in names:
        # Runtime tier since T19, read-through to the pre-move copy.
        path = runtime_read_path(name, _AUGMENT_RELATIVE)
        aug = read_json(path) if path is not None else None
        if not isinstance(aug, dict):
            continue
        pid = pids.get(name)
        for key, row in (aug.get("sessions") or {}).items():
            if isinstance(row, dict):
                sessions[_union_key(key, name, pid)] = row

    payload = {
        "schema": 2,
        "updated_at": _now_iso(),
        "sessions": sessions,
    }
    return _gate.publish(workspace_sessions_dir() / "sessions-augment.json", payload)
