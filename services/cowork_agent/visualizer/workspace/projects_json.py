"""``<XO root>/.xo/projects.json`` — the projects registry (syncplan §5.2)."""

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
from services.cowork_agent.visualizer.atomic_write import ChangeGate
from services.cowork_agent.visualizer.git_provenance import git_provenance, is_git_repo
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.sinks.project_json import refresh_git
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

#: The registry, and the file it replaces. Readers keep the fallback for one
#: release (``scopes.WorkspaceVisualizerScope.read_projects``).
FILENAME = "projects.json"
LEGACY_FILENAME = "workspace.json"

#: 1 was ``workspace.json``'s array-of-names shape.
SCHEMA = 2

#: A pid is only used as an object key in ``by_pid`` after passing this.
_SAFE_PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")

_GIT_REFRESH_DEFAULT_S = 300.0

# Both caches are keyed on ``(resolved root, directory name)``, not on the name
# alone: ``XO_PROJECTS_ROOT`` is re-read on every call, so a root switch would
# otherwise serve one root's git block under another's project of the same
# name.
_git_cache: dict[tuple[str, str], tuple[float, dict]] = {}
# key -> (project.json stat signature, pid)
_identity_cache: dict[tuple[str, str], tuple[tuple, Optional[str]]] = {}
_gate = ChangeGate()


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
    _gate.reset()


def _identity(key: tuple[str, str], pdir: Path) -> tuple[Optional[str], bool]:
    """``(pid, scaffolded)`` for one project, from its ``project.json``."""
    meta = pdir / ".xo" / "project.json"
    try:
        st = meta.stat()
    except OSError:
        # Not scaffolded, or unreadable — either way there is no identity to
        # report and no cache entry worth keeping.
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
    """The ``git`` block for one project, refreshed on an interval."""
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
        # The registry is the caller's product; a failed durable write must not
        # cost it its tick.
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

    # Entries that vanished from the root — or belong to a root this process
    # has stopped watching — must not pin memory forever.
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
    """Refresh ``projects.json``. Returns ``True`` iff the file changed."""
    payload = build(project_ids)
    return _gate.publish(path(), payload)
