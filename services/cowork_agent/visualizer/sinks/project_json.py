"""``project.json`` sink — one-shot identity fill."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent import coder_identity
from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

# The keys this sink owns (docs/syncplan.md §5.1).
_OWNS: frozenset[str] = frozenset(
    {"schema", "pid", "name", "owner_user_id", "created_at", "_template"}
)

# Record schema version minted for a document that carries none.
_SCHEMA_VERSION = 2

# Paths already reported as unreadable, so a corrupt document doesn't emit a
# warning on every tick (the watcher polls once a second by default).
_UNREADABLE_WARNED: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_user_id() -> str:
    """
    The owning user: auth state, else the Coder workspace owner, else
    ``"local"``. Resolution lives in
    :mod:`services.cowork_agent.coder_identity` so all three records that carry
    an identity agree on it.
    """
    return coder_identity.resolve_user_id()


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


def fill_identity(
    xo_dir: Path, project_id: str, *, upgrade_placeholder_owner: bool = True
) -> bool:
    """Run the one-shot identity fill if needed."""
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
        # ``read_json`` returns ``None`` both for "file absent" and for "file
        # present but unparseable" — and they are not the same thing. Absent is
        # safe to mint into.
        if path.exists():
            _warn_unreadable_once(path)
            return False
        current = {}

    # Resolved once: the guard below needs it, and so does ``values``.
    resolved_owner = _resolve_user_id()
    owner_is_upgradable = (
        upgrade_placeholder_owner
        and coder_identity.is_placeholder_user_id(current.get("owner_user_id"))
        and not coder_identity.is_placeholder_user_id(resolved_owner)
    )

    if not current.get("_template", False) and current.get("pid"):
        # Already filled — no-op, with one exception.
        if not owner_is_upgradable:
            return False

    # Key-scoped merge (rule R-WRITE): ``or``-default only the five keys this
    # sink owns and let ``write_json_owned`` carry every other key forward.
    values = {
        "schema": current.get("schema") or _SCHEMA_VERSION,
        "pid": current.get("pid") or str(uuid.uuid4()),
        "name": current.get("name") or project_id,
        # A stored owner is never overwritten — that would silently take
        # someone else's project.
        "owner_user_id": (
            resolved_owner
            if owner_is_upgradable
            else (current.get("owner_user_id") or resolved_owner)
        ),
        "created_at": current.get("created_at") or _now_iso(),
    }

    try:
        # ``volatile=()``: nothing in this document is a per-tick timestamp, so
        # every difference is a real change.
        return write_json_owned(path, owns=_OWNS, values=values, volatile=())
    except CorruptDocumentError:
        # The file parsed a moment ago and does not now — another writer, or a
        # truncation, landed in between.
        _warn_unreadable_once(path)
        return False


# ── The git block (docs/syncplan.md §5.1: "the git refresher owns ``git``") ──

_GIT_OWNS: frozenset[str] = frozenset({"git"})


def refresh_git(
    xo_dir: Path, remote_url: str | None, default_branch: str | None
) -> bool:
    """Persist git provenance into ``project.json``. ``True`` iff written."""
    if remote_url is None and default_branch is None:
        return False
    path = xo_dir / "project.json"
    if not path.exists():
        return False
    try:
        return write_json_owned(
            path,
            owns=_GIT_OWNS,
            values={"git": {
                "remote_url": remote_url,
                "default_branch": default_branch,
            }},
            volatile=(),
        )
    except CorruptDocumentError:
        # Same refusal as fill_identity: never write through a document that
        # may still hold identity we cannot see.
        _warn_unreadable_once(path)
        return False
