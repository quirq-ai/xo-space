"""A thread's git worktree on disk, its whole life (design ch 13 §1–§6).

* ``place``: the folder a thread session runs in, decided when the thread is
  created: ``<project>/.xo/worktrees/thread-<ts>`` in a git project, the
  project folder itself (with a note) otherwise (§2.4). Cheap: one ``lstat``.
* ``ensure``: create the tree, or find and reuse it (idempotent), or bring it
  back at the same path after it was removed (§4.4).
* ``find``, ``loss``, ``remove``: what removing a tree would lose (ignored
  files counted, §4.2) and the removal itself, safe (§4.3) or forced by the
  person.
* ``survey`` and ``cli_worktrees``: the project's other worktrees, for project
  removal (§7.4) and for the ones a CLI made on its own (§6).

What makes a tree XO's (§2.3): the folder ``thread-<ts>`` under
``.xo/worktrees/``, a branch ``xo/thread-<6 hex>``, a lock whose reason is
``XO Space thread <ts>`` (no pid, so no CLI's own sweep takes it for its
own), and the marker ``XO_THREAD`` ``{thread, chat, base_commit}`` in git's
admin folder for it. XO acts on a tree only when the marker and the thread's
record agree; the caller holds the record.

``ensure``, removal and the sweep take the tree's lock (``lock_for``), so a
thread reopened while its tree is being removed waits, then brings it back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.cowork_agent.storm.worktrees.git import (
    Entry,
    GitFailed,
    git,
    git_out,
    relative_paths,
    status,
    worktree_list,
)
from services.errors import ServiceError
from services.storage.atomic_write import create_file_exclusive, write_json_atomic
from services.xo_structure import WORKTREES_DIRNAME, XO_DIRNAME, XO_GITIGNORE

log = logging.getLogger(__name__)

TREE_PREFIX = "thread-"
BRANCH_PREFIX = "xo/thread-"
#: The branch's random part: 3 bytes, 6 hex digits.
BRANCH_HEX_BYTES = 3
#: New hex for a branch name that is taken, this many times at most (a bound
#: on a retry loop: with 16 million names, more than one clash is a bug).
BRANCH_TRIES = 8
MARKER = "XO_THREAD"
#: ``git rev-parse --verify --quiet``'s exit when the ref doesn't exist (any
#: other failure is git's own).
NO_SUCH_REF = 1
LOCK_REASON = "XO Space thread {ts}"
EXCLUDE_LINE = f"/{XO_DIRNAME}/{WORKTREES_DIRNAME}/"
EXCLUDE_COMMENT = "# XO Space thread worktrees"
GITIGNORE_TEXT = f"{WORKTREES_DIRNAME}/\n"
#: Folders a build or install recreates: ignored files in them never keep a
#: tree (§4.2). An ignored ``.env`` or data file does.
REBUILDABLE = frozenset({"node_modules", ".venv", "__pycache__", ".pytest_cache", "dist", "build", "target", ".next"})
#: How many paths of each kind a loss lists (the counts are always whole).
LISTED_PATHS = 50
#: The hex digits of the confirmation the person repeats to force a removal.
CONFIRM_HEX = 12

NON_GIT_NOTE = "This project isn't a git repository, so this thread works in the project folder directly."
FROM_BRANCH_NOTE = ("Your worktree was removed while this thread was idle; it is back at the same path, on its "
                    "branch {branch} as it was committed. Uncommitted changes from before are gone.")
FROM_HEAD_NOTE = ("Your worktree was removed while this thread was idle, and its branch {old} is gone or in use "
                  "elsewhere; it is back at the same path on a new branch {branch}, from the project's current HEAD "
                  "({head}).")
_TS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class WorktreeRefused(ServiceError):
    """Nothing XO can retry: the person has to act (a symlinked ``.xo``, a
    folder in the way, a repository without a commit)."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(code, message, status=status)


class Kept(ServiceError):
    """git itself refused to remove the tree: it is kept, and why."""

    def __init__(self, reason: str) -> None:
        super().__init__("removal_refused", f"Kept: {reason}", status=409)
        self.reason = reason


# ── where ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Place:
    """Where a thread session runs: ``cwd``; ``worktree_path`` when that is
    an XO worktree (None in a non-git project); ``note`` for the thread when
    it works in the project folder itself."""

    cwd: str
    worktree_path: str | None
    note: str | None


def place(project_folder: Path, thread_ts: str) -> Place:
    folder = Path(project_folder)
    if not folder.is_absolute():
        raise ValueError(f"a project folder is an absolute path, not {project_folder!r}")
    if not os.path.lexists(folder / ".git"):
        return Place(str(folder), None, NON_GIT_NOTE)
    path = str(tree_path(folder, thread_ts))
    return Place(path, path, None)


def worktrees_root(project_folder: Path) -> Path:
    return Path(project_folder) / XO_DIRNAME / WORKTREES_DIRNAME


def tree_path(project_folder: Path, thread_ts: str) -> Path:
    if not isinstance(thread_ts, str) or not _TS.fullmatch(thread_ts):
        raise WorktreeRefused("bad_thread_ts", f"{thread_ts!r} can't name a worktree folder: a thread's ts is "
                                               "letters, digits, '.', '_' and '-'.", status=400)
    return worktrees_root(project_folder) / f"{TREE_PREFIX}{thread_ts}"


def thread_of(path: Path) -> tuple[Path, str] | None:
    """``(project folder, thread ts)`` when ``path`` is an XO thread tree's
    folder (``<project>/.xo/worktrees/thread-<ts>``), else None."""
    path = Path(path)
    if (path.parent.name != WORKTREES_DIRNAME or path.parent.parent.name != XO_DIRNAME
            or not path.name.startswith(TREE_PREFIX)):
        return None
    ts = path.name[len(TREE_PREFIX):]
    return (path.parent.parent.parent, ts) if _TS.fullmatch(ts) else None


# ── one lock per tree ────────────────────────────────────────────────────

_tree_locks: dict[str, asyncio.Lock] = {}
_project_locks: dict[str, asyncio.Lock] = {}


def lock_for(path: Path) -> asyncio.Lock:
    """The tree's lock: ``ensure``, removal and the sweep hold it."""
    return _tree_locks.setdefault(os.path.normpath(str(path)), asyncio.Lock())


def _project_lock(project_folder: Path) -> asyncio.Lock:
    return _project_locks.setdefault(os.path.normpath(str(project_folder)), asyncio.Lock())


# ── what git and the marker say ──────────────────────────────────────────


@dataclass(frozen=True)
class Found:
    """A tree as git and XO's marker see it now."""

    path: Path
    exists: bool                        # something is at its path
    checkout: bool                      # ... with a ``.git`` entry: a checkout
    empty: bool                         # ... an empty folder
    entry: Entry | None                 # its registration, if git has one
    marker: dict[str, Any] | None       # XO's marker, if its admin folder has one

    @property
    def registered(self) -> bool:
        return self.entry is not None

    def ours(self, chat_id: str, thread_ts: str) -> bool:
        """The marker names this thread: XO may act on it."""
        return (self.entry is not None and self.marker is not None
                and self.marker.get("thread") == thread_ts and self.marker.get("chat") == chat_id)


@dataclass(frozen=True)
class Snapshot:
    """A repository's worktrees at one moment, and git's admin folder for
    each (read from the admin folders' ``gitdir`` files, so a tree whose
    folder is gone is still found)."""

    project: Path
    common: Path
    entries: tuple[Entry, ...]
    admins: dict[str, Path] = field(default_factory=dict)       # realpath of a tree → its admin folder

    def entry(self, path: Path) -> Entry | None:
        key = os.path.realpath(path)
        return next((e for e in self.entries[1:] if os.path.realpath(e.path) == key), None)

    def admin(self, path: Path) -> Path | None:
        return self.admins.get(os.path.realpath(path))

    def found(self, path: Path) -> Found:
        """Blocking (it looks at the folder and reads the marker)."""
        path = Path(path)
        admin = self.admin(path)
        exists = os.path.lexists(path)
        empty = exists and path.is_dir() and not path.is_symlink() and next(os.scandir(path), None) is None
        return Found(path, exists, os.path.lexists(path / ".git"), empty, self.entry(path),
                     _read_marker(admin) if admin is not None else None)


async def snapshot(project_folder: Path) -> Snapshot:
    folder = Path(project_folder)
    common = Path((await git_out(folder, "find the repository", "rev-parse", "--path-format=absolute",
                                 "--git-common-dir")).strip())
    entries = worktree_list(await git_out(folder, "list its worktrees", "worktree", "list", "--porcelain", "-z"))
    return Snapshot(folder, common, tuple(entries), await asyncio.to_thread(_admin_folders, common))


async def find(project_folder: Path, path: Path) -> Found:
    snap = await snapshot(project_folder)
    return await asyncio.to_thread(snap.found, path)


def _admin_folders(common: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    try:
        names = os.listdir(common / "worktrees")
    except OSError:
        return found
    for name in names:
        admin = common / "worktrees" / name
        try:
            gitdir = (admin / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        gitfile = Path(gitdir) if os.path.isabs(gitdir) else Path(os.path.normpath(admin / gitdir))
        found[os.path.realpath(gitfile.parent)] = admin
    return found


def _read_marker(admin: Path) -> dict[str, Any] | None:
    try:
        marker = json.loads((admin / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


# ── creating and reusing ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Tree:
    """A thread's tree, ready: its folder, the XO branch, the commit it was
    made from, and a note for the agent when it came back after a removal."""

    path: Path
    branch: str
    base_commit: str
    note: str | None = None


async def ensure(project_folder: Path, thread_ts: str, *, chat_id: str, branch: str | None = None,
                 base_commit: str | None = None) -> Tree:
    """The thread's tree, created or reused (§2.2–§2.3), or brought back at
    the same path after a removal (§4.4): from ``branch`` (the thread's
    recorded branch) if it still exists, else from ``HEAD`` on a new branch.
    ``base_commit`` is the thread's recorded one, kept when the tree comes
    back on its branch. Raises ``WorktreeRefused`` when the person must act,
    ``GitFailed`` when git failed (worth retrying)."""
    folder = Path(project_folder)
    path = tree_path(folder, thread_ts)
    async with lock_for(path):
        await asyncio.to_thread(_check_places, folder, path)
        snap = await snapshot(folder)
        found = await asyncio.to_thread(snap.found, path)
        if found.marker is not None and not found.ours(chat_id, thread_ts):
            raise WorktreeRefused("worktree_not_this_threads", f"{path} is the worktree of another thread "
                                  f"({found.marker.get('chat')}, {found.marker.get('thread')}); XO leaves it alone.")
        if found.registered and found.checkout:
            if found.marker is not None:
                return await _reuse(snap, found, thread_ts, branch)
            await _clear_interrupted(folder, found, thread_ts)
        elif found.exists and not found.empty:
            raise _in_the_way(path)
        elif found.registered:
            if found.marker is None and found.entry.locked != LOCK_REASON.format(ts=thread_ts):
                raise _in_the_way(path)             # someone else's registration: XO leaves it alone
            await remove(folder, found, None)       # its folder is gone: that one registration goes, branch kept
        return await _create(snap, path, thread_ts, chat_id, branch, base_commit)


def _check_places(folder: Path, path: Path) -> None:
    """Refuse a symlinked ``.xo`` or worktrees folder (a tree written through
    it would land outside the project), a project that is no longer a git
    repository, and a file or a symlink in the tree's place."""
    if not os.path.lexists(folder / ".git"):
        raise WorktreeRefused("not_a_repository", f"{folder} is no longer a git repository, so this thread's "
                                                  "worktree can't be made. Restore its .git, then send again.")
    for place_ in (folder / XO_DIRNAME, folder / XO_DIRNAME / WORKTREES_DIRNAME):
        if place_.is_symlink() or (place_.exists() and not place_.is_dir()):
            raise WorktreeRefused("unsafe_worktree_folder", f"{place_} is a symlink or not a folder, so XO won't "
                                  "put a worktree there. Make it a plain folder inside the project, then send again.")
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise _in_the_way(path)


def xo_branch(branch: str | None) -> str | None:
    """``branch`` when it is an XO thread branch: the only kind XO deletes."""
    return branch if branch and branch.startswith(BRANCH_PREFIX) else None


def _in_the_way(path: Path) -> WorktreeRefused:
    return WorktreeRefused("worktree_folder_taken", f"{path} already exists and isn't an XO worktree; move it away, "
                                                    "then send again.")


async def _reuse(snap: Snapshot, found: Found, thread_ts: str, branch: str | None) -> Tree:
    if found.entry.locked is None:
        await git_out(snap.project, "lock the worktree", "worktree", "lock", "--reason",
                      LOCK_REASON.format(ts=thread_ts), str(found.path))
    await _keep_out_of_git(snap.project, snap.common)
    return Tree(found.path, branch or found.entry.branch or "", str(found.marker.get("base_commit") or ""))


async def _clear_interrupted(folder: Path, found: Found, thread_ts: str) -> None:
    """A tree with XO's lock but no marker: a create stopped between git's
    ``worktree add`` and the marker. Cleared when nothing would be lost;
    refused otherwise."""
    if found.entry.locked != LOCK_REASON.format(ts=thread_ts):
        raise _in_the_way(found.path)
    branch = xo_branch(found.entry.branch)
    lost = await loss(found, branch)
    if not lost.empty:
        raise WorktreeRefused("worktree_folder_taken", f"{found.path} was left half-made by an interrupted start "
                                                       f"and holds work ({lost.summary()}); move it away, then send "
                                                       "again.")
    log.warning("storm worktrees: clearing %s, left without its marker by an interrupted start", found.path)
    await remove(folder, found, branch)


async def _create(snap: Snapshot, path: Path, thread_ts: str, chat_id: str, branch: str | None,
                  base_commit: str | None) -> Tree:
    folder = snap.project
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await _keep_out_of_git(folder, snap.common)
    paths = ["--relative-paths"] if await relative_paths() else []
    if not paths:
        log.warning("storm worktrees: this git can't record relative paths; %s breaks if %s moves (run "
                    "`git worktree repair` there after a move)", path, folder)
    lock = ["--lock", "--reason", LOCK_REASON.format(ts=thread_ts)]
    tree: Tree | None = None
    if branch and await _branch_exists(folder, branch):
        added = await git(folder, "worktree", "add", *paths, *lock, str(path), branch)
        if added.ok:
            base = base_commit or (await git_out(folder, "read the branch", "rev-parse", branch)).strip()
            tree = Tree(path, branch, base, FROM_BRANCH_NOTE.format(branch=branch))
        else:
            log.warning("storm worktrees: %s can't come back on %s (%s); it starts from HEAD", path, branch,
                        (added.stderr or added.output).strip())
    if tree is None:
        head = await _head(folder)
        new = await _add_new_branch(folder, path, head, [*paths, *lock])
        tree = Tree(path, new, head, FROM_HEAD_NOTE.format(old=branch, branch=new, head=head[:12]) if branch else None)
    try:
        admin = Path((await git_out(path, "find the worktree's admin folder", "rev-parse",
                                    "--absolute-git-dir")).strip())
        await asyncio.to_thread(write_json_atomic, admin / MARKER,
                                {"thread": thread_ts, "chat": chat_id, "base_commit": tree.base_commit})
    except BaseException:
        # Unmarked, the tree would be refused as someone else's: undo it whole.
        await asyncio.shield(_undo(folder, path, tree.branch if tree.branch != branch else None))
        raise
    return tree


async def _head(folder: Path) -> str:
    result = await git(folder, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not result.ok and result.returncode != NO_SUCH_REF:
        raise GitFailed("read the project's HEAD", result)
    if not result.ok:
        raise WorktreeRefused("no_commit", f"{folder}'s repository has no commit yet, so a thread's worktree has "
                                           "nothing to start from. Commit once, then send again.")
    return result.output.strip()


async def _add_new_branch(folder: Path, path: Path, head: str, options: list[str]) -> str:
    """``-b``, never ``-B``: an existing branch is never reset; a name that
    is taken already gets new hex. A failed add (an unwritable folder, a
    full disk) may leave the branch it made behind: that branch, which
    points at ``head`` and nothing has checked out, is deleted, and the
    failure is retried later (``GitFailed``)."""
    for _ in range(BRANCH_TRIES):
        name = BRANCH_PREFIX + secrets.token_hex(BRANCH_HEX_BYTES)
        if await _branch_exists(folder, name):
            continue
        added = await git(folder, "worktree", "add", *options, "-b", name, str(path), head)
        if added.ok:
            return name
        made = await git(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        if made.ok and made.output.strip() == head:
            await git(folder, "branch", "-D", name)        # git refuses it if anything has it checked out
        raise GitFailed("add the thread's worktree", added)
    raise WorktreeRefused("branch_names_taken", f"{BRANCH_TRIES} new xo/thread-* branch names in a row were "
                                                "taken in this repository; delete unused xo/thread-* branches, "
                                                "then send again.")


async def _branch_exists(folder: Path, branch: str) -> bool:
    return (await git(folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")).ok


async def _undo(folder: Path, path: Path, new_branch: str | None) -> None:
    await git(folder, "worktree", "unlock", str(path))
    await git(folder, "worktree", "remove", "--force", str(path))
    if new_branch:
        await git(folder, "branch", "-D", new_branch)


async def _keep_out_of_git(folder: Path, common: Path) -> None:
    """``.xo/.gitignore`` holding ``worktrees/``, made only if the project
    has none; ``/.xo/worktrees/`` in the repository's own exclude file, once
    (§1)."""
    async with _project_lock(folder):
        await asyncio.to_thread(_write_ignores, folder, common)


def _write_ignores(folder: Path, common: Path) -> None:
    xo = folder / XO_DIRNAME
    xo.mkdir(exist_ok=True)
    create_file_exclusive(xo / XO_GITIGNORE, GITIGNORE_TEXT.encode("utf-8"))
    exclude = common / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if EXCLUDE_LINE in current.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fp:
        fp.write(("\n" if current and not current.endswith("\n") else "") + f"{EXCLUDE_COMMENT}\n{EXCLUDE_LINE}\n")


# ── what removal would lose, and the removal ─────────────────────────────


@dataclass(frozen=True)
class Loss:
    """What removing a tree would lose: changed tracked files, untracked
    files, ignored files outside the rebuildable folders, and commits found
    nowhere else (not on another branch or a remote)."""

    changed: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    commits: int = 0
    branch: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.changed or self.untracked or self.ignored or self.commits)

    def summary(self) -> str:
        parts = []
        if self.changed:
            parts.append(_count(len(self.changed), "uncommitted file"))
        if self.untracked:
            parts.append(_count(len(self.untracked), "untracked file"))
        if self.ignored:
            names = ", ".join(self.ignored[:3]) + (", …" if len(self.ignored) > 3 else "")
            parts.append(f"an ignored file ({names})" if len(self.ignored) == 1
                         else f"{len(self.ignored)} ignored files ({names})")
        if self.commits:
            where = f"only on {self.branch}" if self.branch else "on no branch"
            parts.append(f"{_count(self.commits, 'commit')} {where}")
        return ", ".join(parts)

    def what(self) -> dict[str, Any]:
        """The person's view of it: the summary, each kind's count and its
        first ``LISTED_PATHS`` paths."""
        return {"summary": self.summary(),
                **{kind: {"count": len(paths), "paths": list(paths[:LISTED_PATHS])}
                   for kind, paths in (("changed", self.changed), ("untracked", self.untracked),
                                       ("ignored", self.ignored))},
                "commits": self.commits, "branch": self.branch}


def confirmation(what: dict[str, Any]) -> str:
    """A short hash of exactly what the person was shown: forcing a removal
    takes it back, so a tree that changed since is refused again."""
    return hashlib.sha256(json.dumps(what, sort_keys=True).encode("utf-8")).hexdigest()[:CONFIRM_HEX]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


async def loss(found: Found, branch: str | None) -> Loss:
    """§4.2's checks 1 and 2 for a tree whose folder is there: ``git status``
    counting ignored files (an ignored folder is one entry; only ignored
    entries under ``REBUILDABLE`` folders are let go), and the commits
    reachable from the tree's ``HEAD`` or its XO ``branch`` that no other
    branch or remote has."""
    path = found.path
    out = await git_out(path, "read the worktree's status", "status", "--porcelain=v1", "-z",
                        "--untracked-files=all", "--ignored=matching")
    changed, untracked, ignored = [], [], []
    for change in status(out):
        if change.code == "!!":
            if not REBUILDABLE.intersection(Path(change.path.rstrip("/")).parts):
                ignored.append(change.path)
        elif change.code == "??":            # untracked: never git's to rebuild, whatever folder it is in
            untracked.append(change.path)
        else:
            changed.append(change.path)
    tips = ["HEAD"]
    if branch and await _branch_exists(path, branch):
        tips.append(branch)
    exclude = [f"--exclude={branch}"] if branch else []
    count = await git_out(path, "count the worktree's own commits", "rev-list", "--count", *tips, "--not",
                          *exclude, "--branches", "--remotes")
    return Loss(tuple(changed), tuple(untracked), tuple(ignored), int(count.strip() or 0), branch)


async def remove(project_folder: Path, found: Found, branch: str | None, *, force: bool = False) -> str | None:
    """Remove a tree (§4.3): unlock, ``git worktree remove`` (``--force``
    only when the person forces it), then delete its XO branch with ``-d``
    (``-D`` when forced; ``branch`` None keeps every branch). For a tree
    whose folder is gone, ``git worktree remove`` drops that one
    registration: ``git worktree prune`` is repo-wide and would drop the
    person's own stale entries too (checked on git 2.55). Raises ``Kept``
    when git refuses (the tree stays locked as it was). Returns a note when
    the branch was kept."""
    folder = Path(project_folder)
    path = str(found.path)
    if found.entry is not None and found.entry.locked is not None:
        await git_out(folder, "unlock the worktree", "worktree", "unlock", path)
    removed = await git(folder, "worktree", "remove", *(["--force"] if force else []), path)
    if not removed.ok:
        if found.entry is not None and found.entry.locked is not None:
            await git(folder, "worktree", "lock", "--reason", found.entry.locked, path)
        raise Kept(_said(removed) or "git refused to remove it")
    branch = xo_branch(branch)
    if branch is None or not await _branch_exists(folder, branch):
        return None
    deleted = await git(folder, "branch", "-D" if force else "-d", branch)
    if deleted.ok:
        return None
    note = f"its branch {branch} was kept ({_said(deleted) or 'git refused to delete it'})"
    log.info("storm worktrees: removed %s; %s", path, note)
    return note


def _said(result) -> str:
    """The first line git printed about a failure."""
    lines = (result.stderr or result.output).strip().splitlines()
    return lines[0].removeprefix("fatal: ").removeprefix("error: ") if lines else ""


# ── the project's other worktrees ────────────────────────────────────────


def _is_xo_folder(snap: Snapshot, entry: Entry) -> bool:
    return os.path.realpath(Path(entry.path).parent) == os.path.realpath(worktrees_root(snap.project))


async def cli_worktrees(project_folder: Path) -> list[str]:
    """The project's linked worktrees that aren't XO's (§6): every one but
    the main checkout and the folders under ``.xo/worktrees/``, sorted."""
    snap = await snapshot(project_folder)
    return sorted(e.path for e in snap.entries[1:] if not _is_xo_folder(snap, e))


async def survey(project_folder: Path) -> dict[str, list[dict[str, Any]]]:
    """For project removal (§7.4): XO's trees that can go now, each with its
    thread, offered first; then what blocks: XO trees that hold work, and
    every worktree that isn't XO's."""
    folder = Path(project_folder)
    snap = await snapshot(folder)
    removable: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for entry in snap.entries[1:]:
        found = await asyncio.to_thread(snap.found, Path(entry.path))
        shown = _shown(folder, entry.path)
        marker = found.marker if _is_xo_folder(snap, entry) else None
        if marker is None:
            blocking.append({"path": shown, "branch": entry.branch, "xo": False,
                             "reason": "A worktree XO didn't make; remove it with `git worktree remove`."})
            continue
        row = {"path": shown, "branch": entry.branch, "xo": True, "chat_id": marker.get("chat"),
               "thread_ts": marker.get("thread")}
        lost = await loss(found, xo_branch(entry.branch)) if found.checkout else Loss()
        if lost.empty:
            removable.append(row)
        else:
            blocking.append({**row, "reason": f"Kept: {lost.summary()}"})
    return {"removable": removable, "blocking": blocking}


def _shown(folder: Path, path: str) -> str:
    """A path as the person reads it: relative to the project when inside it."""
    try:
        return Path(os.path.realpath(path)).relative_to(os.path.realpath(folder)).as_posix()
    except ValueError:
        return path
