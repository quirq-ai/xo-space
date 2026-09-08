"""``~/.quirq/workspace/sessions/sessions-augment.json`` — union of
every project's per-project augment file.

Same schema; same key shape (composite session id or native session
id, depending on whether an adapter row exists at the project tier).

Runtime tier since T20, via ``project_layout.workspace_sessions_dir()`` — the
write below is unchanged because that chokepoint is what moved.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from services.cowork_agent.project_layout import runtime_read_path, workspace_sessions_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


# Relative to the project's runtime root (and, for the read-through, to its
# pre-move ``.xo/``). The tier decision itself is project_layout's.
_AUGMENT_RELATIVE = "sessions/sessions-augment.json"


# The payload this process last wrote, per target path — the write-on-change
# baseline (syncplan §3: held in memory rather than re-read, and sound because
# this sink owns the whole document). Keyed by path because
# ``QUIRQ_STATE_ROOT`` is re-read on every call.
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _previous.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26).

    ``project_ids``: the tick-wide project list, resolved once by the
    watcher (docs/syncplan.md §10, T23). ``None`` walks the root."""
    sessions: dict[str, dict] = {}
    for pid in (project_ids if project_ids is not None else list_project_ids()):
        # Runtime tier since T19, read-through to the pre-move copy.
        path = runtime_read_path(pid, _AUGMENT_RELATIVE)
        aug = read_json(path) if path is not None else None
        if not isinstance(aug, dict):
            continue
        for key, row in (aug.get("sessions") or {}).items():
            if isinstance(row, dict):
                sessions[key] = row

    payload = {
        "schema": 2,
        "updated_at": _now_iso(),
        "sessions": sessions,
    }
    target = workspace_sessions_dir() / "sessions-augment.json"
    key = str(target)
    if key in _previous and target.exists():
        # Steady state: one ``stat`` and a dict comparison, no read.
        changed = write_json_atomic_if_changed(
            target, payload, ("updated_at",), previous=_previous[key]
        )
    else:
        # No baseline yet (first tick of the process), or the file was
        # removed underneath us — ``rm -rf ~/.quirq`` is a documented clean
        # reset (syncplan §4) and must repopulate on the next tick, not on
        # the next content change. The helper takes its baseline from disk.
        changed = write_json_atomic_if_changed(target, payload, ("updated_at",))
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = payload
    return changed
