"""The timeline's files: one log per project, one Space log.

* ``projects/<pid>/timeline.jsonl``  every line about that project (it
  carries the pid); rotated at :data:`ROTATE_BYTES`, :data:`KEEP` kept
* ``projects/timeline.jsonl``        the Space log: lines with no pid
  (``project.created`` for a folder that has none yet, Space-wide
  events); same rotation

The paths are the ones ``services.cowork_agent.project_layout`` resolves
(``runtime_dir(pid)`` and ``workspace_timeline_path()``), so the module
writes exactly where the readers of this release already look. Older
installs copied every project line into the Space log, tagged
``project_id``; :func:`modules.timeline.service.compact_space_log` drops
those copies once and the merged read dedupes the rest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from services.cowork_agent import project_layout
from services.storage.eventlog import EventLog
from services.storage.files import File

logger = logging.getLogger(__name__)

TIMELINE_FILE = "timeline.jsonl"
ROTATE_BYTES = 8 << 20
KEEP = 5

#: Every file this module writes (services/storage/files.py).
FILES = [
    File("projects/<pid>/timeline.jsonl", role="record", log=True, rotate="8 MB, keep 5",
         note="what happened in one project, newest last; every line carries the pid"),
    File("projects/timeline.jsonl", role="record", log=True, rotate="8 MB, keep 5",
         note="Space-level events, those with no pid"),
]


def _log(path: Path) -> EventLog:
    return EventLog(path, rotate_bytes=ROTATE_BYTES, keep=KEEP)


def project_log(pid: str) -> EventLog:
    """``projects/<pid>/timeline.jsonl``. Raises ``ValueError`` for a pid
    that is not a safe path segment (``project_layout.runtime_dir``)."""
    return _log(project_layout.runtime_dir(pid) / TIMELINE_FILE)


def space_log() -> EventLog:
    """``projects/timeline.jsonl``."""
    return _log(project_layout.workspace_timeline_path())


def project_logs() -> dict[str, EventLog]:
    """``{pid: log}`` for every project folder under the runtime home that
    holds a live ``timeline.jsonl``, sorted by pid. Files at the top level
    (the Space log, the watcher's offsets) are not project folders."""
    root = project_layout.xo_runtime_root()
    out: dict[str, EventLog] = {}
    if not root.is_dir():
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        logger.warning("timeline: could not list %s: %s", root, exc)
        return out
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink() or not (entry / TIMELINE_FILE).is_file():
            continue
        try:
            out[entry.name] = project_log(entry.name)
        except ValueError:
            logger.warning("timeline: skipping %s: not a safe runtime key", entry.name)
    return out
