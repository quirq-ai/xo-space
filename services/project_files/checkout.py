"""What git already holds: the watcher's rule for files that appear.

A file the version store has never seen, appearing with exactly the bytes
git holds for its path (in its repository's ``HEAD`` or index), was put
there by git: a new thread worktree, a clone, a checkout, a reset. Nobody
wrote anything new, and git gives those bytes back at any time
(``/versions``' ``head``), so the watcher records no version for it.
Anything else is recorded as before: new content, an edit, a file the
store already knew (a revert to the committed bytes is a change), a
repository git can't read, bytes a filter changed (line endings, LFS).
When in doubt, record.

**No threshold, no timing.** Which repository owns a path is read from the
disk (the nearest folder with a ``.git`` inside the project root); while
that repository's index is locked (git is writing the checkout), the check
waits for it (``LOCK_WAIT_S``), so a half-made worktree is compared with
what git ends up holding, not with what it has written so far.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Mapping

from services.git_safe import git_sync
from services.project_files.edits import Content

log = logging.getLogger(__name__)

#: How long the check waits for a repository's ``index.lock`` to go.
LOCK_WAIT_S = 30.0
LOCK_STEP_S = 0.1
GIT_TIMEOUT_S = 30.0


def held_by_git(root: Path, found: Mapping[str, Content],
                cache: dict[Path, dict[str, set[str]]] | None = None) -> set[str]:
    """The paths of ``found`` (root-relative, with their bytes now) whose
    bytes are exactly the blob git holds for that path. ``cache`` keeps each
    repository's blobs across the chunks of one batch."""
    cache = {} if cache is None else cache
    by_repo: dict[Path, list[str]] = {}
    tops: dict[Path, Path | None] = {}
    for rel in found:
        top = _repository(root, (root / rel).parent, tops)
        if top is not None:
            by_repo.setdefault(top, []).append(rel)
    held: set[str] = set()
    for top, rels in by_repo.items():
        if top not in cache:
            try:
                cache[top] = _blobs(top)
            except Exception:  # noqa: BLE001 - a repository git can't read: its files are recorded
                log.exception("project files: couldn't read what git holds in %s", top)
                cache[top] = {}
        blobs = cache[top]
        if not blobs:
            continue
        inside = os.path.relpath(top, root)
        for rel in rels:
            path = rel if inside == "." else rel[len(inside) + 1:]
            ids = blobs.get(path)
            if ids and _blob_id(found[rel], root / rel, len(next(iter(ids)))) in ids:
                held.add(rel)
    return held


def _repository(root: Path, folder: Path, seen: dict[Path, Path | None]) -> Path | None:
    """The nearest folder from ``folder`` up to ``root`` holding a ``.git``."""
    walked: list[Path] = []
    top: Path | None = None
    current = folder
    while True:
        if current in seen:
            top = seen[current]
            break
        walked.append(current)
        if os.path.lexists(current / ".git"):
            top = current
            break
        if current == root or root not in current.parents:
            break
        current = current.parent
    for each in walked:
        seen[each] = top
    return top


def _git_dir(top: Path) -> Path | None:
    dot = top / ".git"
    if dot.is_dir():
        return dot
    try:
        line = dot.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line[len("gitdir:"):].strip())
    return target if target.is_absolute() else (top / target)


def _wait_unlocked(git_dir: Path) -> None:
    deadline = time.monotonic() + LOCK_WAIT_S
    while (git_dir / "index.lock").exists() and time.monotonic() < deadline:
        time.sleep(LOCK_STEP_S)


def _blobs(top: Path) -> dict[str, set[str]]:
    """Each path's blob ids in the repository's ``HEAD`` and index."""
    git_dir = _git_dir(top)
    if git_dir is None:
        return {}
    _wait_unlocked(git_dir)
    blobs: dict[str, set[str]] = {}
    tree = git_sync(top, "ls-tree", "-r", "-z", "--full-tree", "HEAD", timeout=GIT_TIMEOUT_S, log_output=False)
    if tree.ok:
        for entry in tree.output.split("\0"):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if path and len(parts) == 3 and parts[1] == "blob":
                blobs.setdefault(path, set()).add(parts[2])
    index = git_sync(top, "ls-files", "-s", "-z", timeout=GIT_TIMEOUT_S, log_output=False)
    if index.ok:
        for entry in index.output.split("\0"):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if path and len(parts) == 3:
                blobs.setdefault(path, set()).add(parts[1])
    return blobs


def _blob_id(content: Content, path: Path, width: int) -> str | None:
    """git's id for these bytes (SHA-1, or SHA-256 in a SHA-256 repository);
    a file too large to have been held is read again, None if it can't be."""
    digest = hashlib.sha256() if width == 64 else hashlib.sha1()
    if content.data is not None:
        digest.update(b"blob %d\0" % len(content.data))
        digest.update(content.data)
        return digest.hexdigest()
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            digest.update(b"blob %d\0" % size)
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
