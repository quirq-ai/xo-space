"""Watcher-owned state directory.

Adapter sources that need to persist their own cursors (e.g. a
SQLite-polling source tracking the last-seen row id per session)
should put their files under :func:`watcher_state_dir`, not construct
the path themselves. This keeps the watcher's filesystem layout an
implementation detail the adapter doesn't need to know.

The dir is also where ``ingest.jsonl_tail.OffsetStore`` keeps its
file offsets (``offsets.json``), so adapter cursor files, the shared
offset store, and ephemeral live-presence snapshots live side by side
— one ``~/.quirq/watcher/`` to clean if you ever want to fully reset
watcher state.
"""

from __future__ import annotations

from pathlib import Path

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.local_state import legacy_state_dir, quirq_state_dir


def watcher_state_dir() -> Path:
    """Return the directory where watcher infrastructure (offsets,
    per-source cursors, etc.) persists its state. Created on first
    access by the callers that write into it; not pre-created here so
    a read-only deployment doesn't unnecessarily mkdir."""
    return quirq_state_dir() / "watcher"


def watcher_heartbeat_path() -> Path:
    """Return the watcher's once-per-tick liveness beat.

    The watcher rewrites this file at the end of every tick; readers
    (the Quirq catalog) use it to report *observed* liveness instead of
    inferring it from configuration. Like :func:`watcher_state_dir` this
    only computes the path — the directory is created by the watcher
    when it writes, so a read-only inspection never mkdirs.
    """
    return watcher_state_dir() / "heartbeat.json"


def legacy_watcher_state_dir() -> Path:
    """Return the former watcher root for read-only cursor migration."""
    return legacy_state_dir() / "watcher"


def watcher_activity_dir() -> Path:
    """Return the root for watcher-owned live-presence snapshots.

    Activity is machine-local and ephemeral, so it deliberately lives
    outside portable ``xo-projects/<id>/.xo/`` metadata.
    """
    return watcher_state_dir() / "activity"


def project_activity_path(project_id: str) -> Path:
    """Return the live-presence file for one project.

    Normalising the id keeps an untrusted route/project value from
    escaping the watcher state root.
    """
    safe_project_id = normalize_agent_id(project_id)
    return watcher_activity_dir() / "projects" / f"{safe_project_id}.json"


def workspace_activity_path() -> Path:
    """Return the workspace-wide union of project presence snapshots."""
    return watcher_activity_dir() / "workspace.json"
