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

This module also carries the second of ``project.json``'s three writers,
:func:`refresh_git`, which owns the ``git`` block and nothing else (see
docs/syncplan.md §5.1). The two are kept in one module because they write
one file; their ownership sets are disjoint and each merges key-scoped.

``fill_identity`` **owns exactly those five keys plus the removal of
``_template``**. Every other key in the document —
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

from services.cowork_agent import coder_identity
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
    """The owning user: auth state, else the Coder workspace owner, else
    ``"local"``. Resolution lives in :mod:`services.cowork_agent.coder_identity`
    so all three records that carry an identity agree on it.
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
    """Run the one-shot identity fill if needed.

    Returns ``True`` iff ``project.json`` was rewritten.

    ``upgrade_placeholder_owner=False`` restores the strict "no-op once the
    pid is minted" behaviour. ``visualizer/migrate.py`` needs it: it calls
    this only to guarantee a pid exists before it has a stable runtime key
    to move files to, and its contract is that the **synced tree is left
    untouched** (``test_migrate.test_the_synced_contract_is_left_alone``).
    A layout migration quietly rewriting identity as a side effect is
    exactly the kind of unasked-for write that contract exists to forbid.
    The upgrade still lands — on the watcher's next tick, which calls this
    for every project anyway.
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

    # Resolved once: the guard below needs it, and so does ``values``.
    resolved_owner = _resolve_user_id()
    owner_is_upgradable = (
        upgrade_placeholder_owner
        and coder_identity.is_placeholder_user_id(current.get("owner_user_id"))
        and not coder_identity.is_placeholder_user_id(resolved_owner)
    )

    if not current.get("_template", False) and current.get("pid"):
        # Already filled — no-op, with one exception. Every project minted
        # before an identity was available carries ``owner_user_id: "local"``
        # frozen in, and this early return is what would keep it there
        # forever: the fill never runs again on a project that has a pid. So
        # the placeholder gets one upgrade, and only when there is a real
        # answer to replace it with (off Coder and unauthenticated there is
        # not, and this stays a no-op rather than rewriting "local" over
        # "local" on every tick).
        if not owner_is_upgradable:
            return False

    # Key-scoped merge (rule R-WRITE): ``or``-default only the five keys
    # this sink owns and let ``write_json_owned`` carry every other key
    # forward. ``_template`` is declared owned but never supplied, which is
    # how an owned key gets deleted — the one key deliberately dropped.
    values = {
        "schema": current.get("schema") or _SCHEMA_VERSION,
        "pid": current.get("pid") or str(uuid.uuid4()),
        "name": current.get("name") or project_id,
        # A stored owner is never overwritten — that would silently take
        # someone else's project. ``"local"`` is the one exception: it
        # identifies nobody and every unauthenticated Space wrote the same
        # value, so it is treated as unset and upgraded once (2026-09-08).
        "owner_user_id": (
            resolved_owner
            if owner_is_upgradable
            else (current.get("owner_user_id") or resolved_owner)
        ),
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


# ── The git block (docs/syncplan.md §5.1: "the git refresher owns ``git``") ──

_GIT_OWNS: frozenset[str] = frozenset({"git"})


def refresh_git(
    xo_dir: Path, remote_url: str | None, default_branch: str | None
) -> bool:
    """Persist git provenance into ``project.json``. ``True`` iff written.

    This is the **durable** half of the provenance pair. The other half,
    ``projects.json:git``, is a machine-local cache rebuilt from disk on
    every tick and excluded from every snapshot; this one travels, and it
    is the only copy that survives the operation it exists to describe:
    ``xo_projects_sync/tarball.py`` excludes ``.git`` from every archive
    but **includes** ``.xo/project.json``, so a restore destroys the
    repository the URL was read from while preserving this file.

    Which is exactly why a null block is never written over a stored one.
    On a freshly restored project ``.git`` is absent, so the refresher
    reads "not a repo" — and writing that answer through would erase the
    remote URL at the one moment it is the only record left. So:

    * both fields ``None`` → **no write**, whatever is on disk stays;
    * otherwise → the block is replaced.

    The cost of that choice is a remote *deletion* going unrecorded here
    (the folder stops being a repo, the durable block keeps the last URL
    it saw). That is the right way round: a stale URL is recoverable
    information, an erased one is not, and ``projects.json:git.is_repo``
    still reports the live answer for anything that needs it.

    Never creates the file. An absent ``project.json`` means the project
    is not scaffolded, and minting one here would write a document with a
    ``git`` block and no identity — plus ``mkdir -p`` a ghost project, the
    same hazard :func:`fill_identity` guards at its own entry.
    """
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
        # Same refusal as fill_identity: never write through a document
        # that may still hold identity we cannot see.
        _warn_unreadable_once(path)
        return False
