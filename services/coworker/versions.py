"""One project file's exact bytes at one commit, for the coworker UI's
Changes view: a changed image shown before and after, a binary downloaded
as a commit had it.

The core's /file already gives a text file at a commit; this is the same
for any file, as bytes. Everything that decides what may be read is the
core's: the address (``address.locate``: nothing outside the project, at
the path or through its links), the repository that owns the place
(``history.owning_repository``: a thread worktree's for a path under
``.xo/worktrees/<name>/``, else the project's own), and how a client's
historical path is checked (``history.safe_repo_path``). git runs hardened
(``git_safe``: no hooks, fsmonitor or transports) and only reads objects;
the blob is written by ``git unpack-file`` into a private temp folder and
read back, so its bytes never pass through text decoding.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.git_safe import git_sync
from services.project_files.address import FileRefused, locate
from services.project_files.history import GIT_TIMEOUT_SECONDS, owning_repository, safe_repo_path

#: A commit as a client may name it: an abbreviated or full hash, or HEAD
#: (the checkout's own; a thread worktree's HEAD for a path in one).
REVISION_RE = re.compile(r"HEAD|[0-9a-fA-F]{4,40}")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Version:
    project_id: str
    relative_path: str
    name: str          # the file's name at that commit
    commit: str        # the full hash the request resolved to
    data: bytes
    immutable: bool    # named by a full hash: the same bytes forever


def not_found(message: str) -> FileRefused:
    return FileRefused("version_not_found", message, status=404)


def bytes_at_commit(project_id: str, relative_path: str, commit: str, *, commit_path: str | None = None,
                    max_bytes: int, too_large: FileRefused) -> Version:
    """``relative_path`` is the file as the project addresses it today (it may
    no longer exist: the "before" of a deletion); ``commit_path`` its name at
    that commit, relative to the owning repository (a rename's ``old_path``).
    ``FileRefused``: 400 ``invalid_version_request`` / ``invalid_relative_path``,
    403 ``outside_project``, 404 ``project_not_found`` / ``version_not_found``,
    and ``too_large`` past ``max_bytes``."""
    if not REVISION_RE.fullmatch(commit or ""):
        raise FileRefused("invalid_version_request", "commit must be a commit hash or HEAD.", status=400)
    target = locate(project_id, relative_path)
    root = target.root
    place = root / target.real_rel
    start = place.parent
    while not start.is_dir() and start != root:
        start = start.parent
    top = owning_repository(start, root)
    if top is None:
        raise not_found("No repository owns this file.")
    top = top.resolve()
    try:
        in_repo = place.relative_to(top).as_posix()
        path = safe_repo_path(commit_path) if commit_path is not None else in_repo
    except ValueError:
        raise FileRefused("invalid_version_request", "commit_path is malformed.", status=400) from None

    def out(*args: str) -> str | None:
        res = git_sync(top, *args, timeout=GIT_TIMEOUT_SECONDS, log_output=False, ceiling=root.parent)
        text = res.stdout.strip() if res.ok else ""
        return text or None

    sha = out("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if sha is None:
        raise not_found("This repository has no such commit.")
    blob = out("rev-parse", "--verify", "--quiet", f"{sha}:{path}")
    if blob is None or out("cat-file", "-t", blob) != "blob":
        raise not_found("This commit has no version of the file.")
    size = out("cat-file", "-s", blob)
    if size is None or not size.isdigit():
        raise not_found("This version of the file can't be read.")
    if int(size) > max_bytes:
        raise too_large
    git_dir = out("rev-parse", "--absolute-git-dir")
    if git_dir is None:
        raise not_found("No repository owns this file.")
    folder = tempfile.mkdtemp(prefix="xo-version-")
    try:
        res = git_sync(Path(folder), "unpack-file", blob, timeout=GIT_TIMEOUT_SECONDS, log_output=False,
                       env={"GIT_DIR": git_dir})
        made = res.stdout.strip() if res.ok else ""
        if not made or "/" in made or made in (".", ".."):
            raise not_found("This version of the file can't be read.")
        with open(os.path.join(folder, made), "rb") as handle:
            data = handle.read(max_bytes + 1)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if len(data) > max_bytes:
        raise too_large
    return Version(target.project_id, target.rel, path.rsplit("/", 1)[-1], sha, data,
                   bool(_FULL_SHA_RE.fullmatch(commit)))
