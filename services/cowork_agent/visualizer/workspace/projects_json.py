"""``<XO root>/.xo/projects.json`` — the projects registry (syncplan §5.2).

Replaces ``workspace.json``, whose payload was a bare array of directory
names. This document is authoritative for *which projects exist*, and
carries the two facts a peer needs about each one: the ``pid`` that
survives a rename, and where the folder came from (``git``).

**The map is keyed by directory name, not by pid.** That is the one
decision in this file worth defending, and it rests on three findings:

1. **A JSON object cannot have a null key, and every project is pid-less
   at birth.** ``scaffold_project()`` leaves ``pid: null`` present in
   ``project.json`` until the watcher's identity fill runs, and a bare
   folder — which ``list_project_ids()`` reports like any other — may
   never receive one at all.
2. **Duplicate pids are real and silent.** ``cp -r`` of a project gives
   both copies the identical UUID and ``fill_identity`` declines to
   re-mint on either, because both are already non-template with a pid.
   Restore is worse: ``xo_projects_sync/tarball.py`` excludes ``.git``
   but not ``.xo/project.json``, so a snapshot carries the origin's pid
   into the target folder. Keyed by pid, one of those two directories
   silently disappears from the registry.
3. **Every consumer key in the system is already the directory name** —
   routes, graph hubs (``p_<dirname>``), activity and timeline
   ``project_id``, the backup repository name. Nothing holds a pid. And
   a pid that arrived over sync is *untrusted input*: as an object key
   it would land in this file with no clamp at all.

``by_pid`` is therefore a **derived reverse index in the same file**, so
a pid lookup stays O(1) without a second source of truth. Its values are
**always arrays**: one pid in two folders is a reachable state and losing
a folder is not an option, so ``len(entry) > 1`` *is* the duplicate-pid
alarm rather than a crash. A null pid, or one that fails the key charset
clamp, is simply absent from the index.

**Cost.** This file is rewritten on every watcher tick, so nothing here
may shell out per tick. Two caches keep it honest:

* ``git_provenance()`` costs two ``git`` spawns per repository, so it is
  refreshed per project at most every ``XO_GIT_PROVENANCE_REFRESH_S``
  (default 300 s) — a remote URL changes about once in a project's life.
* ``pid``/``scaffolded`` come from ``<project>/.xo/project.json``, read
  only when its ``stat`` signature changes. Minting a pid rewrites that
  file, so the signature is exact, and the steady state is one ``stat``
  per project per tick instead of a read plus a JSON parse.

The write itself goes through :func:`write_json_atomic_if_changed`: this
sink owns the whole document (nothing else writes it), so an in-memory
baseline is sound and a corrupt file is repaired by overwriting rather
than merged into.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.project_layout import workspace_xo_dir, xo_projects_root
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.git_provenance import git_provenance, is_git_repo
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.sinks.project_json import refresh_git
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

#: The registry, and the file it replaces. Readers keep the fallback for
#: one release (``scopes.WorkspaceVisualizerScope.read_projects``).
FILENAME = "projects.json"
LEGACY_FILENAME = "workspace.json"

#: 1 was ``workspace.json``'s array-of-names shape.
SCHEMA = 2

#: A pid is only used as an object key in ``by_pid`` after passing this.
#: It admits a UUID and every sane opaque id, and excludes the shapes
#: that make a JSON key dangerous downstream — path separators, dots that
#: read as a traversal, control characters, the empty string.
_SAFE_PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")

_GIT_REFRESH_DEFAULT_S = 300.0

# Both caches are keyed on ``(resolved root, directory name)``, not on the
# name alone: ``XO_PROJECTS_ROOT`` is re-read on every call, so a root
# switch would otherwise serve one root's git block under another's
# project of the same name.
# key -> (monotonic stamp, git block)
_git_cache: dict[tuple[str, str], tuple[float, dict]] = {}
# key -> (project.json stat signature, pid)
_identity_cache: dict[tuple[str, str], tuple[tuple, Optional[str]]] = {}
# The payload this process last wrote, per target path — the write-on-change
# baseline (syncplan §3: held in memory rather than re-read, and sound because
# this sink owns the whole document). Keyed like the two caches above, and for
# the same reason: ``XO_PROJECTS_ROOT`` is re-read on every call, so a root
# switch must not be answered from the previous root's registry.
_previous: dict[str, dict] = {}
_PREVIOUS_MAX = 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path() -> Path:
    return workspace_xo_dir() / FILENAME


def legacy_path() -> Path:
    return workspace_xo_dir() / LEGACY_FILENAME


def git_refresh_seconds() -> float:
    """How long a project's git block is reused before it is re-read."""
    raw = os.getenv("XO_GIT_PROVENANCE_REFRESH_S", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else _GIT_REFRESH_DEFAULT_S
    except ValueError:
        return _GIT_REFRESH_DEFAULT_S


def reset_caches() -> None:
    """Drop every in-process cache. For tests, and for a root switch."""
    _git_cache.clear()
    _identity_cache.clear()
    _previous.clear()


def _identity(key: tuple[str, str], pdir: Path) -> tuple[Optional[str], bool]:
    """``(pid, scaffolded)`` for one project, from its ``project.json``.

    Cached on the metadata file's ``stat`` signature: the pid only ever
    changes by rewriting that file, so a matching signature means a
    matching pid, and the tick pays one ``stat`` instead of a read and a
    parse per project.
    """
    meta = pdir / ".xo" / "project.json"
    try:
        st = meta.stat()
    except OSError:
        # Not scaffolded, or unreadable — either way there is no identity
        # to report and no cache entry worth keeping.
        _identity_cache.pop(key, None)
        return None, False

    stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)
    cached = _identity_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1], True

    pid: Optional[str] = None
    doc = read_json(meta)
    if isinstance(doc, dict):
        raw = doc.get("pid")
        if isinstance(raw, str) and raw.strip():
            pid = raw.strip()
    _identity_cache[key] = (stamp, pid)
    return pid, True


def _git_block(key: tuple[str, str], pdir: Path) -> dict:
    """The ``git`` block for one project, refreshed on an interval.

    ``is_repo`` is answered from a single ``stat`` every time — it is what
    flips when a user runs ``git init`` — while the two fields that cost a
    subprocess are reused until the interval expires.

    The refresh path is also where the **durable** copy is written, into
    ``<project>/.xo/project.json`` (syncplan §5.1 assigns ``git`` there to
    "the git refresher"; only the cache half was ever wired). Hanging it
    off the cache miss is what keeps it free: no extra ``git`` spawn, and
    at most one key-scoped merge per project per TTL rather than one per
    tick. ``refresh_git`` declines to create the file, so an unscaffolded
    folder stays unscaffolded.
    """
    repo = is_git_repo(pdir)
    now = time.monotonic()
    ttl = git_refresh_seconds()
    cached = _git_cache.get(key)
    if (
        cached is not None
        and cached[1].get("is_repo") == repo
        and (now - cached[0]) < ttl
    ):
        return cached[1]

    if repo:
        prov = git_provenance(pdir)
        block = {
            "is_repo": True,
            "remote_url": prov.get("remote_url"),
            "default_branch": prov.get("default_branch"),
        }
    else:
        block = {"is_repo": False, "remote_url": None, "default_branch": None}
    _git_cache[key] = (now, block)
    try:
        refresh_git(pdir / ".xo", block["remote_url"], block["default_branch"])
    except Exception:  # pragma: no cover - the cache must not depend on it
        # The registry is the caller's product; a failed durable write must
        # not cost it its tick. ``refresh_git`` already swallows the corrupt
        # case, so reaching here means something rarer (a read-only tree).
        logger.warning("could not persist git provenance for %s", pdir, exc_info=True)
    return block


def build(project_ids: Sequence[str] | None = None) -> dict:
    """Assemble the registry payload without writing it."""
    root = xo_projects_root()
    ids = list(project_ids) if project_ids is not None else list_project_ids()
    names = sorted({str(name) for name in ids if str(name)})

    projects: dict[str, Any] = {}
    for name in names:
        key = (str(root), name)
        pdir = root / name
        pid, scaffolded = _identity(key, pdir)
        projects[name] = {
            "pid": pid,
            "scaffolded": scaffolded,
            "git": _git_block(key, pdir),
        }

    # Derived in the same pass, never edited independently. Arrays, always.
    by_pid: dict[str, list[str]] = {}
    for name, row in projects.items():
        pid = row["pid"]
        if isinstance(pid, str) and _SAFE_PID_RE.match(pid):
            by_pid.setdefault(pid, []).append(name)

    # Entries that vanished from the root — or belong to a root this
    # process has stopped watching — must not pin memory forever.
    live = {(str(root), name) for name in names}
    for cache in (_git_cache, _identity_cache):
        for stale in [key for key in cache if key not in live]:
            cache.pop(stale, None)

    return {
        "$schema": "xo/projects.schema.json",
        "schema": SCHEMA,
        "updated_at": _now_iso(),
        "projects_root": str(root),
        "projects": projects,
        "by_pid": {pid: sorted(folders) for pid, folders in sorted(by_pid.items())},
    }


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Refresh ``projects.json``. Returns ``True`` iff the file changed.

    ``project_ids`` lets the watcher pass the list it already resolved
    once for the whole tick (syncplan §10, T23); ``None`` keeps the
    standalone behaviour of walking the root.
    """
    payload = build(project_ids)
    target = path()
    key = str(target)
    if key in _previous and target.exists():
        # Steady state: one ``stat`` and a dict comparison, no read.
        changed = write_json_atomic_if_changed(target, payload, previous=_previous[key])
    else:
        # First write of this process: the baseline has to come off disk,
        # otherwise a restart rewrites an identical file. Same branch covers
        # the file being deleted underneath us — a cached baseline alone
        # would leave it missing until its content happened to change.
        changed = write_json_atomic_if_changed(target, payload)
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = payload
    return changed
