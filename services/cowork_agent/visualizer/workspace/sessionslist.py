"""
``~/.quirq/workspace/sessions/sessionslist.json`` — union of every project's
adapter-written session index.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.project_layout import workspace_sessions_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.workspace_index import list_project_ids


# The payload this process last wrote, per target path — the write-on-change
# baseline (syncplan §3: held in memory rather than re-read, and sound because
# this sink owns the whole document).
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _previous.clear()


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26)."""
    merged: dict[str, dict] = {}
    for pid in (project_ids if project_ids is not None else list_project_ids()):
        # The per-project index is partitioned across shard files now, so this
        # is a merge rather than one read — see engine.sessions_io.
        merged.update(session_index.read_session_index(pid))
    # sessionslist.json has no top-level wrapper — it's the flat map, and no
    # field in it is volatile, so the comparison excludes nothing (T26).
    target = workspace_sessions_dir() / "sessionslist.json"
    key = str(target)
    if key in _previous and target.exists():
        # Steady state: one ``stat`` and a dict comparison, no read.
        changed = write_json_atomic_if_changed(
            target, merged, (), previous=_previous[key]
        )
    else:
        # No baseline yet (first tick of the process), or the file was removed
        # underneath us — ``rm -rf ~/.quirq`` is a documented clean reset
        # (syncplan §4) and must repopulate on the next tick, not on the next
        # content change.
        changed = write_json_atomic_if_changed(target, merged, ())
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = merged
    return changed
