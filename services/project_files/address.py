"""Where a project file is, and what Space lets the person do with it.

The one place the project-file rules live. An address is a project id plus
a project-relative path; nothing outside the project is ever reached, at the
path as written or as its links resolve.

**Places** (owner's decisions, 5 Nov 2026), by what a save may do:

* ``ordinary``: everything else, dotfiles included (``.env``, ``.gitignore``,
  ``.github/…``). Saved like any file.
* ``runs_code``: what git or an agent CLI runs on its own: anything under a
  ``.git`` folder (config, hooks, refs), and every agent's project config
  (``project_config_paths`` in the agent manifests, so this module names no
  agent), at the project's top or a thread worktree's. A save needs the
  person's confirmation (``confirm: "runs_code"``).
* ``read_only``: XO's records, ``.xo/`` outside the thread worktrees
  (``project.json``, ``todos.json``, ``attachments/``, ``made/``…), which
  have their own services, locks and screens; and a worktree's own ``.git``
  pointer file, which only tells git where the worktree's data is.

A path is judged at its written form and at its resolved form (links
followed), and the stricter place wins: a link in a worktree pointing at
``.git/hooks/pre-commit`` is ``runs_code``.

**Writes never follow a link that appears after the check.** ``parent_dir``
opens the resolved path's folders one at a time, each relative to the one
before with ``O_NOFOLLOW``, so a folder swapped for a link meanwhile fails
with ``ELOOP`` (``place_changed``) instead of writing through it.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from enum import IntEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterator

from services.cowork_agent.project_layout import project_dir, resolve_project_dirname
from services.cowork_agent.registry.agent_registry import all_agents
from services.errors import ServiceError
from services.xo_structure import WORKTREES_DIRNAME, XO_DIRNAME

#: The largest file the editor opens or saves, and the largest content a
#: save takes.
EDIT_MAX_BYTES = 1024 * 1024
#: A version token: the SHA-256 of a file's exact bytes.
VERSION_PREFIX = "sha256:"
#: What a save must carry to change a ``runs_code`` file.
RUNS_CODE_CONFIRM = "runs_code"

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_SUBDIR = _DIR | os.O_NOFOLLOW


class Place(IntEnum):
    """Ordered from least to most guarded: the stricter of two wins."""

    ORDINARY = 0
    RUNS_CODE = 1
    READ_ONLY = 2

    @property
    def label(self) -> str:
        return self.name.lower()


class FileRefused(ServiceError):
    """A project-file request Space won't carry out; ``code`` says why, and
    ``extra`` carries what the client needs to go on (a conflict's current
    version)."""

    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None) -> None:
        super().__init__(code, message, status)
        self.extra = extra or {}


def project_not_found() -> FileRefused:
    return FileRefused("project_not_found", "Project not found.", status=404)


def file_not_found() -> FileRefused:
    return FileRefused("file_not_found", "File not found in project.", status=404)


def invalid_path(why: str) -> FileRefused:
    return FileRefused("invalid_relative_path", f"relative_path is malformed: {why}.", status=400)


def place_changed() -> FileRefused:
    return FileRefused("place_changed", "A folder on the way to this file changed while saving (it became a "
                                        "link, or was removed). Nothing was written; try again.", status=409)


def version_of(data: bytes) -> str:
    return VERSION_PREFIX + hashlib.sha256(data).hexdigest()


# ── the address ───────────────────────────────────────────────────────────────


def clean_relative(relative_path: str | None) -> str:
    """A project-relative path, normalised to ``a/b/c``; ``FileRefused``
    (400) for an empty, absolute or traversing one, or one with a NUL."""
    rel = relative_path or ""
    if not rel:
        raise invalid_path("it is empty")
    if "\x00" in rel:
        raise invalid_path("it contains a null byte")
    if rel.startswith(("/", "\\")):
        raise invalid_path("it starts with a path separator")
    rel = rel.replace("\\", "/").strip("/")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise invalid_path("it contains '..', '.' or an empty segment")
    return rel


def project_root(project_id: str) -> tuple[str, Path]:
    """``(project id, resolved root)``; ``project_not_found`` when there is
    no such folder."""
    pid = resolve_project_dirname(project_id)
    root = project_dir(pid)
    if not root.is_dir():
        raise project_not_found()
    return pid, root.resolve()


def worktree_of(rel: str) -> str | None:
    """The thread worktree a project-relative path is inside, if any."""
    parts = rel.split("/")
    if len(parts) >= 4 and parts[0] == XO_DIRNAME and parts[1] == WORKTREES_DIRNAME:
        return parts[2]
    return None


@dataclass(frozen=True)
class Target:
    """One project file's address, checked: ``rel`` as asked, ``real_rel``
    with links resolved (both inside the project), and the stricter place
    of the two."""

    project_id: str
    root: Path
    rel: str
    real_rel: str
    place: Place

    @property
    def path(self) -> Path:
        return self.root / self.real_rel

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def worktree(self) -> str | None:
        return worktree_of(self.rel)


def locate(project_id: str, relative_path: str | None) -> Target:
    """Check one address. ``FileRefused``: 404 for a missing project, 400 for
    a malformed path, 403 ``outside_project`` when the path, or what its
    links point at, is outside the project. The file itself may not exist."""
    pid, root = project_root(project_id)
    rel = clean_relative(relative_path)
    real = Path(os.path.realpath(root / rel))
    if real == root or not real.is_relative_to(root):
        raise FileRefused("outside_project", "This path, or the file a link on it points to, is outside the "
                                             "project.", status=403)
    real_rel = real.relative_to(root).as_posix()
    return Target(pid, root, rel, real_rel, max(classify(rel), classify(real_rel)))


# ── places ────────────────────────────────────────────────────────────────────


def run_code_patterns() -> tuple[str, ...]:
    """Every agent's project config globs, as the manifests declare them."""
    return tuple(sorted({glob for manifest in all_agents() for glob in manifest.project_config_paths}))


def _glob_match(rel: str, pattern: str) -> bool:
    """``dir/**`` is the folder and everything in it; anything else is an
    fnmatch on the whole relative path (the policy engine's rule)."""
    if pattern.endswith("/**"):
        top = pattern[:-3]
        return rel == top or rel.startswith(top + "/")
    return fnmatchcase(rel, pattern)


def classify(rel: str) -> Place:
    """The place of one project-relative path (see the module docstring).
    Names are compared without case: on a case-insensitive disk (macOS's
    default) ``.GIT/hooks/x`` is ``.git/hooks/x``."""
    parts = rel.lower().split("/")
    if parts[0] == XO_DIRNAME:
        if len(parts) < 4 or parts[1] != WORKTREES_DIRNAME:
            return Place.READ_ONLY          # XO's records, or a worktree folder itself
        inner = parts[3:]
        if inner == [".git"]:
            return Place.READ_ONLY          # the worktree's pointer to its git data
        return _inner(inner)
    return _inner(parts)


def _inner(parts: list[str]) -> Place:
    """The place of a path inside a checkout (the project's or a worktree)."""
    if ".git" in parts:
        return Place.RUNS_CODE              # any repository's own files, a nested one's too
    rel = "/".join(parts)
    if any(_glob_match(rel, pattern.lower()) for pattern in run_code_patterns()):
        return Place.RUNS_CODE
    return Place.ORDINARY


# ── walking without following links ───────────────────────────────────────────


@contextlib.contextmanager
def parent_dir(target: Target) -> Iterator[int]:
    """A descriptor of the folder holding ``target.real_rel``, reached from
    the project root one folder at a time without following a link. 409
    ``place_changed`` when a folder on the way became a link or a file,
    404 ``folder_not_found`` when one is missing."""
    opened: list[int] = []
    try:
        try:
            opened.append(os.open(target.root, _DIR))
        except FileNotFoundError:
            raise project_not_found() from None
        for name in target.real_rel.split("/")[:-1]:
            try:
                opened.append(os.open(name, _SUBDIR, dir_fd=opened[-1]))
            except FileNotFoundError:
                raise FileRefused("folder_not_found", "The folder this file goes in doesn't exist.",
                                  status=404) from None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise place_changed() from None
                raise
        yield opened[-1]
    finally:
        for fd in reversed(opened):
            os.close(fd)


def open_file(dir_fd: int, name: str) -> int | None:
    """A read descriptor of the regular file ``name`` in ``dir_fd``, never
    through a link; None when there is nothing there. 409 ``place_changed``
    when it is a link (it resolved to a file a moment ago) or not a file."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise place_changed() from None
        if exc.errno == errno.EISDIR:
            raise FileRefused("not_a_file", "This path is a folder, not a file.", status=400) from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FileRefused("not_a_file", "This path isn't a regular file.", status=400)
    return fd


def read_fd(fd: int, limit: int) -> bytes:
    """Up to ``limit`` bytes from ``fd``'s start."""
    chunks, left = [], limit
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


# ── what nobody versions (the watcher's ignore set) ──────────────────────────

#: Folders whose files are rebuilt or installed, never written by a person:
#: no version is kept of them, the watcher skips them, and so does a walk
#: that stands in for git (``/paths`` and ``/search`` outside a repository).
NOISE_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache",
                        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".gradle", ".turbo", ".parcel-cache"})
#: Space's own temp files (``save._tmp_name``): ``.<name>.xo-save.<hex>.tmp``.
_SAVE_TMP_MIDDLE = ".xo-save."


def is_save_temp(name: str) -> bool:
    return name.startswith(".") and name.endswith(".tmp") and _SAVE_TMP_MIDDLE in name


def is_noise(rel: str) -> bool:
    """Whether a project-relative path is outside what Space versions: git's
    own files (``.git/`` anywhere, a worktree's ``.git`` pointer), XO's
    records (``.xo/`` except inside a thread worktree, and a worktree's own
    ``.xo/``), a worktree's folder itself, the build and install folders of
    ``NOISE_DIRS``, and Space's temp files."""
    parts = rel.split("/")
    if parts[0] == XO_DIRNAME:
        if len(parts) < 4 or parts[1] != WORKTREES_DIRNAME:
            return True
        parts = parts[3:]
        if parts[0] == XO_DIRNAME:
            return True
    if ".git" in parts or NOISE_DIRS.intersection(parts):
        return True
    return is_save_temp(parts[-1])


#: The newer routes' names (contract §4) for refusals the older ones name
#: otherwise: a malformed path, and anything missing on the way to a file
#: (every 404 they answer carries a code).
_CONTRACT_CODES = {"invalid_relative_path": "invalid_path", "file_not_found": "not_found",
                   "folder_not_found": "not_found", "directory_not_found": "not_found",
                   "worktree_not_found": "not_found"}


def noise_pathspecs() -> list[str]:
    """``is_noise`` as git exclude pathspecs, for a command run at the top
    of a checkout (the project's or a worktree's): XO's records there
    (``.xo/``; a worktree is its own root), ``NOISE_DIRS`` at any depth, and
    Space's temp files. git never lists its own ``.git/``."""
    return [f":(exclude){XO_DIRNAME}",
            *(f":(exclude,glob)**/{name}/**" for name in sorted(NOISE_DIRS)),
            f":(exclude,glob)**/.*{_SAVE_TMP_MIDDLE}*.tmp"]


def invalid_path_code(exc: FileRefused) -> FileRefused:
    """A refusal as the newer routes name it (``_CONTRACT_CODES``); every
    other refusal unchanged."""
    if exc.code in _CONTRACT_CODES:
        return FileRefused(_CONTRACT_CODES[exc.code], exc.message, status=exc.status, extra=exc.extra)
    return exc


def locate_entry(project_id: str, relative_path: str | None) -> Target:
    """Check the address of an entry to move, remove or create, which is
    never followed itself: its folder is resolved (and must be in the
    project), its own name is not, so a link is moved or removed as a link.
    ``place`` is the stricter of the path as written and as its folder
    resolves; a folder's contents are judged by the caller."""
    pid, root = project_root(project_id)
    rel = clean_relative(relative_path)
    parent, _, name = rel.rpartition("/")
    real_parent = Path(os.path.realpath(root / parent)) if parent else root
    if not (real_parent == root or real_parent.is_relative_to(root)):
        raise FileRefused("outside_project", "This path's folder, or the folder a link on it points to, is "
                                             "outside the project.", status=403)
    real_rel = (real_parent / name).relative_to(root).as_posix()
    return Target(pid, root, rel, real_rel, max(classify(rel), classify(real_rel)))
