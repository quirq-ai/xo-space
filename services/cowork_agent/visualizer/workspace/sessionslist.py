"""``~/.quirq/workspace/sessions/sessionslist.json`` — union of every
project's adapter-written session index.

Each row already carries ``directory`` (absolute path of the
project) so the workspace tier doesn't need a separate ``project_id``
field — the BFF route derives ``projectId`` from the directory or
from the row's enclosing scope.

Watcher-written at this tier; adapters don't touch workspace-level
files. Composite-key collisions across projects are not expected
(the adapter generates an 8-hex suffix) but if they happened, the
later project would win — same last-write-wins behaviour any union
has.

Runtime tier since T20 — the union of files T19 had already made
machine-local. The path itself is unchanged here because
``project_layout.workspace_sessions_dir()`` is the chokepoint that moved; the
sole reason this module names it at all is so the join is not hand-built.
Unlike the per-project index this stays ONE file: it has exactly one writer
(this sink, once a tick), so R-CONTEND's partitioning buys nothing.

**Write-on-change (T26).** One of the five once-per-tick workspace writers.
The document is a bare map with **no timestamp of its own**, so the
comparison excludes nothing: ``volatile=()``. An idle tick merges the same
adapter-written rows and skips the write entirely.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.project_layout import workspace_sessions_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.workspace_index import list_project_ids


# The payload this process last wrote, per target path — the write-on-change
# baseline (syncplan §3: held in memory rather than re-read, and sound because
# this sink owns the whole document). Keyed by path because
# ``XO_PROJECTS_ROOT`` / ``QUIRQ_STATE_ROOT`` are re-read on every call.
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _previous.clear()


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Rebuild the union. Returns ``True`` iff the file changed (T26).

    ``project_ids``: the tick-wide project list, resolved once by the
    watcher (docs/syncplan.md §10, T23). ``None`` walks the root."""
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
        # No baseline yet (first tick of the process), or the file was
        # removed underneath us — ``rm -rf ~/.quirq`` is a documented clean
        # reset (syncplan §4) and must repopulate on the next tick, not on
        # the next content change. The helper takes its baseline from disk.
        changed = write_json_atomic_if_changed(target, merged, ())
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = merged
    return changed
