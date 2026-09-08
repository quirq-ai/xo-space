"""``project.json`` sink — one-shot identity fill.

The bundled template ships ``project.json`` with ``_template: true``
and null identity fields. On first sight of a project (the first
``SessionFirstSeen`` event the watcher processes for it, or simply
on the first tick where the project is discovered) this sink:

* generates a UUID for ``pid`` if missing
* sets ``name`` to the project id (the user can rename via the UI
  later; the watcher doesn't override an explicit name)
* sets ``owner_user_id`` from the auth state (or ``"local"``)
* sets ``created_at`` to the current ISO timestamp
* removes ``_template`` so subsequent ticks no-op

It **owns exactly those five keys plus the removal of ``_template``**
(see docs/syncplan.md §5.1). Every other key in the document —
``display_name``/``description`` written by
``project_layout._upsert_metadata``, ``git`` written by the git
refresher, a manually curated ``category``, anything a future writer
adds — is carried forward untouched. Writing a fresh literal here
instead of merging destroyed those keys on every tick, for every
project. The merge is delegated to
:func:`~services.cowork_agent.visualizer.atomic_write.write_json_owned`
so the ownership set is declared once, in one place, rather than
re-implemented by hand.

Idempotent. Runs to completion or no-ops; never partially writes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

# The keys this sink owns (docs/syncplan.md §5.1). ``_template`` is owned
# and never supplied in ``values``: under ``write_json_owned`` an owned key
# that is omitted is *deleted*, which is how the template marker is dropped.
_OWNS: frozenset[str] = frozenset(
    {"schema", "pid", "name", "owner_user_id", "created_at", "_template"}
)

# Record schema version minted for a document that carries none. An existing
# ``schema`` is carried forward, never rewritten: a v1 document is
# structurally a valid v2 document (v2 only *declares* keys that were
# already being written), and silently renumbering someone else's record is
# not this sink's call to make.
_SCHEMA_VERSION = 2

# Paths already reported as unreadable, so a corrupt document doesn't emit a
# warning on every tick (the watcher polls once a second by default).
_UNREADABLE_WARNED: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_user_id() -> str:
    """Pull the local user id from the auth state, falling back to
    ``"local"`` (see docs/watcher-design.md §8.1).

    Imported lazily because ``routers.auth`` triggers FastAPI app
    construction at import time in some test paths.
    """
    try:
        from routers.auth.auth import get_auth_state
        return (get_auth_state().get("user_id") or "local")
    except Exception:
        return "local"


def _warn_unreadable_once(path: Path) -> None:
    key = str(path)
    if key in _UNREADABLE_WARNED:
        return
    _UNREADABLE_WARNED.add(key)
    logger.warning(
        "project.json at %s exists but is unreadable; refusing to mint a new "
        "pid over it. Repair or delete the file to let identity fill run.",
        path,
    )


def fill_identity(xo_dir: Path, project_id: str) -> bool:
    """Run the one-shot identity fill if needed.

    Returns ``True`` iff ``project.json`` was rewritten.
    """
    if not xo_dir.parent.is_dir():
        # The folder *is* the project (see
        # ``project_layout.resolve_project_dirname``). If the id doesn't
        # point at a directory that exists, writing project.json here would
        # mkdir -p a ghost project rather than describe a real one — an
        # empty folder that then shows up in the UI as its own project.
        return False

    path = xo_dir / "project.json"
    current = read_json(path)

    if not isinstance(current, dict):
        # ``read_json`` returns ``None`` both for "file absent" and for
        # "file present but unparseable" — and they are not the same thing.
        # Absent is safe to mint into. Present-but-unreadable is not: the
        # file may still hold a ``pid``, and the schema promises it is
        # "generated once on first boot, never regenerated"
        # (project.schema.json:14). Minting over it silently rewrites the
        # project's identity, so leave the file alone and no-op instead.
        if path.exists():
            _warn_unreadable_once(path)
            return False
        current = {}

    if not current.get("_template", False) and current.get("pid"):
        # Already filled — no-op.
        return False

    # Key-scoped merge (rule R-WRITE): ``or``-default only the five keys
    # this sink owns and let ``write_json_owned`` carry every other key
    # forward. ``_template`` is declared owned but never supplied, which is
    # how an owned key gets deleted — the one key deliberately dropped.
    values = {
        "schema": current.get("schema") or _SCHEMA_VERSION,
        "pid": current.get("pid") or str(uuid.uuid4()),
        "name": current.get("name") or project_id,
        "owner_user_id": current.get("owner_user_id") or _resolve_user_id(),
        "created_at": current.get("created_at") or _now_iso(),
    }

    try:
        # ``volatile=()``: nothing in this document is a per-tick timestamp,
        # so every difference is a real change.
        return write_json_owned(path, owns=_OWNS, values=values, volatile=())
    except CorruptDocumentError:
        # The file parsed a moment ago and does not now — another writer, or
        # a truncation, landed in between. Same refusal as above: never mint
        # a pid over a document that may still hold one.
        _warn_unreadable_once(path)
        return False
