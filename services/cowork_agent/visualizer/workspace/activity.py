"""Machine-local union of every project's open sessions.

Stored at ``~/.quirq/watcher/activity/workspace.json``. Same schema
as each per-project presence snapshot; every ``open_sessions`` row
carries the activity schema's optional ``project_id`` field so the BFF
can group live sessions by project.

**Determinism (docs/syncplan.md §10, T24).** The rows are inherited
verbatim from the per-project snapshots, so this document inherits
their determinism — and their disorder. The per-project sink now sorts
its rows and omits (rather than invents) unknown timestamps; this
aggregate sorts again by ``(project_id, session_id)`` so the union is
independent of the order projects are visited in and of whatever row
order a snapshot written by an older build happens to have on disk.
The only time-varying field left is the top-level ``updated_at``, which
is what the ``volatile`` exclusion covers.

**Write-on-change (T26).** One of the five once-per-tick workspace writers.
The rows come from the per-project snapshots, which are themselves now
write-on-change, so an idle tick reads the same union and writes nothing.
The ``updated_at`` on disk therefore goes stale while the watcher is
perfectly healthy — by design; ``~/.quirq/watcher/heartbeat.json`` (T22)
is the liveness signal, and it is still written unconditionally.
"""

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
# whole document). Keyed by path because ``QUIRQ_STATE_ROOT`` is re-read on
# every call, so a root switch must not be answered from the old root's
# baseline. At most one live entry; the cap only bounds a process that walks
# many roots (a test suite).
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _previous.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_sort_key(row: dict) -> tuple[str, str, str]:
    """Total order over the union (T24).

    Mirrors :func:`services.cowork_agent.visualizer.sinks.activity._row_sort_key`
    with the project as the leading key, so the aggregate stays grouped
    by project the way the BFF renders it. The canonical JSON dump is a
    tiebreak, so two rows sharing a project and a session id still land
    in a fixed order.
    """
    return (
        str(row.get("project_id", "")),
        str(row.get("session_id", "")),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26).

    ``project_ids``: the tick-wide project list, resolved once by the
    watcher (docs/syncplan.md §10, T23). ``None`` walks the root."""
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
        # No baseline yet (first tick of the process), or the file was
        # removed underneath us — ``rm -rf ~/.quirq`` is a documented clean
        # reset (syncplan §4) and must repopulate on the next tick, not on
        # the next content change. The helper takes its baseline from disk.
        changed = write_json_atomic_if_changed(target, payload, ("updated_at",))
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = payload
    return changed
