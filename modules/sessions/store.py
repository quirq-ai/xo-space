"""The sessions module's files: the per-project session index.

::

    projects/<pid>/sessions/sessionslist.d/<shard>.json   one row per session

Each shard holds one ``{composite_key: row}`` object, written whole and
atomically by the owning adapter (``sessions_io.write_session_row``) and,
for the ``purpose`` field, by this module. The row is the adapter's
(``sessionId``, ``nativeSessionId``, ``directory``, ``backend``,
``updatedAt``, ``usage`` and whatever else it keeps) plus ``purpose``. The
watcher's ``sessions-augment.json`` beside the shards is telemetry's, keyed
by the same composite key and disjoint in fields
(``services.storage.reader.merge_session_record`` refuses an overlap).

The shard name is a digest of the composite key (:func:`shard_path`), so a
row can be found without reading its neighbours; the merged read stays in
:mod:`sessions_io`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from services.cowork_agent import project_layout
from services.storage.files import File

from . import sessions_io

#: Every file this module writes (services/storage/files.py): the layout
#: test, the fixture README and the "delete it and you lose" column derive
#: from this table.
FILES = [
    File("projects/<pid>/sessions/sessionslist.d/<shard>.json", role="record",
         note="the adapter's index row plus purpose"),
]


def shards_dir(project_id: str) -> Optional[Path]:
    """``projects/<pid>/sessions/sessionslist.d/`` for a project folder name,
    or ``None`` when the project is unknown."""
    root = project_layout.runtime_dir_for_project(project_id)
    if root is None:
        return None
    return root / project_layout.RUNTIME_SESSION_SHARDS_SUBDIR


def shard_path(project_id: str, composite_key: str) -> Optional[Path]:
    """The shard that owns ``composite_key`` in ``project_id``'s index."""
    folder = shards_dir(project_id)
    if folder is None:
        return None
    return folder / sessions_io.shard_filename(composite_key)
