"""The pre-turn integrity check, on disk (design ch 16 §2; owner §11.2,
§0.72): what a session's folder holds at the paths that make a CLI or git
run code on their own, compared with the session's baseline. No store and
no engine here: ``integrity_watch`` runs it in the engine's life, and the
doctor reads it through ``integrity_watch.findings``.

**Watched** (§2.1): every agent's ``project_config_paths``, as its runtime
and its manifest declare them (their union, whichever CLI runs, so core
names no CLI); ``.xo/**`` except the thread worktrees under
``.xo/worktrees/``; and git's own: the folder's ``.git`` entry, the common
dir's ``config`` and ``hooks/``, and the worktree's ``config.worktree``. For
a thread's worktree, also what a runtime reads in the project's main
checkout while it runs there (``main_checkout_config_paths``: a CLI may
load the main checkout's local settings, live S16-1), named
``main:<path>``. Where XO's own services write the same folder as the
session (a non-git thread or a plain session in a project folder, or a
session at the projects root), the documents they rewrite all the time are
left out, with their temp siblings: watched, they would report XO's own
writes. A CLI's own home and XO's state root are never watched: a session
in the person's home folder would otherwise watch a CLI's whole home, which
is the person's own (§2.6).

**Links are never followed.** The walk holds a folder open and reads each
entry relative to it: ``lstat``, sub-folders opened ``O_NOFOLLOW |
O_DIRECTORY``, files ``O_NOFOLLOW``. A symlink at a watched path or at a
folder above one is itself the entry (its target is what is hashed), and a
folder swapped for a link mid-check is never read through. A file that
vanishes while the check runs (an atomic write's temp file) wasn't there.

**What can't be read is a change**, never a blind spot: an entry the walk
can't read (a file, a folder it can't list, an entry it can't stat) is
reported as ``unreadable`` by its path, with why, and the rest is still
hashed; what lies under a folder it can't list isn't taken for deleted.
**Over ``MAX_ENTRIES``** the walk stops, and a second walk keeps checking
the config files that matter most (``Watch.priority``: a file a pattern
names exactly; right in a pattern's folder, its ``settings*``, ``hooks*``
and ``config*`` files and everything under its ``hooks/``; and git's own
files); the rest is reported as too many to check.

**Baselines** (§2.2). In a thread's worktree: ``base_commit`` for every
watched path, so a path the commit doesn't have must not be there (an
untracked or ignored file counts), compared object by object: ``git
ls-tree`` of the commit against git's blob id of each file on disk, mode
included. git's own files and the main checkout's against a hash snapshot.
Anywhere else: a hash snapshot of everything watched. Only hashes are kept
(§0.83).

**Not ``git diff`` or ``git status``**, the design's cost line: both run
the repository's clean filters on changed files, and a filter is a command
in git's config, which is one of the files checked here, so an agent that
wrote one would have XO run it; and both trust the index, where
``update-index --assume-unchanged`` or ``--skip-worktree`` hides an edit.
``ls-tree`` and ``cat-file`` only read objects, and every XO git call has
lazy fetching off and no transport allowed (``worktrees/git.py``), so a
partial-clone config can't make them run a transport command either.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence

from services.cowork_agent.storm.redact import redact
from services.cowork_agent.storm.review.paths import glob_match
from services.cowork_agent.storm.worktrees.git import git
from services.errors import ServiceError
from services.xo_structure import TODOS, WORKITEMS, WORKTREES_DIRNAME, XO_DIRNAME

#: The groups a watched path belongs to (§2.1), as an item shows them.
PROJECT_CONFIG, XO, GIT = "project_config", "xo", "git"
#: Core's own watched paths; the CLIs' come from their feature lists.
CORE_WATCHED = (f"{XO_DIRNAME}/**",)
#: Never watched: the thread worktrees inside ``.xo/``.
WORKTREES_UNWATCHED = (f"{XO_DIRNAME}/{WORKTREES_DIRNAME}/**",)
#: What XO's own services rewrite in a ``.xo/`` it shares with a session:
#: a project's todos and work items (on every change), and, at the projects
#: root, the workspace's records (``space.json`` hourly; ``projects.json``,
#: ``xo.json``, ``workspace.json`` and the legacy ``inbox.json``).
XO_REWRITTEN = (TODOS, WORKITEMS, "space.json", "projects.json", "xo.json", "workspace.json", "inbox.json")
#: Left out where XO writes the session's own folder (snapshot mode): the
#: documents above and the temp siblings XO's atomic writes leave for an
#: instant (``services/storage/atomic_write.py``'s three namings).
SNAPSHOT_UNWATCHED = (*WORKTREES_UNWATCHED, *(f"{XO_DIRNAME}/{pattern}" for name in XO_REWRITTEN for pattern in (
    name, f"{name}.tmp", f".{name}.tmp", f".{name}.*.tmp")))
#: The names of what a worktree's check reads in the project's main checkout.
MAIN = "main:"
#: git's own files, by the names an item shows (the ``.git`` entry is shown
#: as itself: it is in the session's folder).
DOTGIT, GIT_CONFIG, GIT_WORKTREE_CONFIG, GIT_HOOKS = ".git", "git:config", "git:config.worktree", "git:hooks"
#: git's modes for what a tree entry holds.
FILE_MODE, EXEC_MODE, LINK_MODE, GITLINK_MODE = "100644", "100755", "120000", "160000"
#: An entry the walk couldn't read: its kind, its change, and the one hash
#: that stands for it (so it is reported once while it stays unreadable).
UNREADABLE_ENTRY = "unreadable"
UNREADABLE_SHA256 = hashlib.sha256(b"unreadable").hexdigest()
#: Right in a project-config pattern's folder, the files that matter most
#: (kept checked over ``MAX_ENTRIES``), and the folder all of whose files do.
PRIORITY_NAMES = ("settings*", "hooks*", "config*")
PRIORITY_FOLDER = "hooks"
#: Bounds on one check's work (not caps on the person's files: a watched
#: folder this big is itself worth a look, and the check says so): the
#: entries a walk meets, and the bytes it hashes.
MAX_ENTRIES = 5_000
MAX_HASHED_BYTES = 64 * 1024 * 1024
#: An item lists this many changes (the rest are counted), and shows a diff
#: for the first ``MAX_DIFFED`` of them, each cut after ``MAX_DIFF_CHARS``,
#: until the item's diffs reach ``MAX_ITEM_DIFF_CHARS``.
MAX_LISTED = 200
MAX_DIFFED = 20
MAX_DIFF_CHARS = 8_000
MAX_ITEM_DIFF_CHARS = 64_000
#: A version larger than this is described, not shown.
MAX_SHOWN_BYTES = 256 * 1024
#: The longest ``.git`` file or ``commondir`` read: one line naming a folder.
MAX_POINTER_BYTES = 4096
_CHUNK = 1 << 16
_NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_SUBDIR = _DIR | os.O_NOFOLLOW
#: What a read meets when the entry changed under it: gone, a folder on the
#: way gone or made a file, or made a link (``O_NOFOLLOW``).
_CHANGED_MEANWHILE = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)

#: Why a check couldn't run, as a reason code: a failure is reported once
#: per code until a check runs without it.
FOLDER_GONE, NOT_A_FOLDER, UNREADABLE, TOO_MANY, TOO_LARGE = (
    "folder_gone", "not_a_folder", "unreadable", "too_many_entries", "too_large")
GIT_MISSING, BASE_UNREADABLE, NOT_A_CHECKOUT = "git_missing", "base_unreadable", "not_a_checkout"


class CheckFailed(ServiceError):
    """The check couldn't run (the folder is gone, git is missing, the base
    commit can't be read): reported like a change, never raised past the
    watch. ``reason`` is its code; the message says what to do."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__("integrity_check_failed", message, status=500)
        self.reason = reason


# ── what is watched ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Watch:
    """The paths a check looks at: every agent's project config, core's
    (``.xo/**``; none in a project's main checkout, whose ``.xo/`` is XO's
    own), what is left out (``unwatched``: ``SNAPSHOT_UNWATCHED`` where XO
    writes the same folder), and the folders never watched (``skip``: the
    CLIs' homes and XO's state root, resolved)."""

    project_config: tuple[str, ...]
    skip: tuple[Path, ...] = ()
    core: tuple[str, ...] = CORE_WATCHED
    unwatched: tuple[str, ...] = WORKTREES_UNWATCHED

    @property
    def patterns(self) -> tuple[tuple[str, str], ...]:
        """(pattern, group), the CLIs' first."""
        return (*((p, PROJECT_CONFIG) for p in self.project_config), *((p, XO) for p in self.core))

    @property
    def roots(self) -> tuple[str, ...]:
        """Where a walk starts: each pattern's folders up to its first
        wildcard ("" when it starts with one: the whole folder), none inside
        another."""
        roots = sorted({_root(p) for p, _group in self.patterns})
        return tuple(r for r in roots if not any(_on_or_under(r, other) for other in roots if other != r))

    def group(self, rel: str, *, link: bool = False) -> str | None:
        """The group that watches ``rel``, or None. A link counts on the way
        to a watched path too (a symlinked folder above it)."""
        path = PurePosixPath(rel)
        if any(glob_match(path, p) for p in self.unwatched):
            return None
        for pattern, group in self.patterns:
            if glob_match(path, pattern) or (link and _on_the_way(rel, _root(pattern))):
                return group
        return None

    def descends(self, rel: str) -> bool:
        """Whether a folder can hold a watched path: on the way to or under
        a pattern's root, outside ``.git`` (git's own are read apart) and
        outside what is left out."""
        path = PurePosixPath(rel)
        if path.parts[:1] == (DOTGIT,) or any(glob_match(path, p) for p in self.unwatched):
            return False
        return any(_on_the_way(rel, _root(pattern)) for pattern, _group in self.patterns)

    def skipped(self, path: Path) -> bool:
        return any(path == s or s in path.parents for s in self.skip)

    def priority(self, rel: str) -> bool:
        """Whether ``rel`` is a project config file that matters most: one a
        pattern names exactly, or, right in a pattern's folder, a
        ``settings*``, ``hooks*`` or ``config*`` file, or anything under its
        ``hooks/``."""
        for pattern in self.project_config:
            root = _root(pattern)
            if root == pattern:
                if rel == pattern:
                    return True
            elif root and rel.startswith(root + "/"):
                inside = PurePosixPath(rel[len(root) + 1:]).parts
                if inside[0] == PRIORITY_FOLDER and len(inside) > 1 or len(inside) == 1 and any(
                        fnmatchcase(inside[0], name) for name in PRIORITY_NAMES):
                    return True
        return False

    def priority_descends(self, rel: str) -> bool:
        """Whether the priority walk opens a folder: on the way to a project
        config pattern's folder, that folder, or its ``hooks/`` and below."""
        for pattern in self.project_config:
            root = _root(pattern)
            if root and (root.startswith(rel + "/") or rel == root or rel == f"{root}/{PRIORITY_FOLDER}"
                         or rel.startswith(f"{root}/{PRIORITY_FOLDER}/")):
                return True
        return False


def _root(pattern: str) -> str:
    parts = []
    for part in PurePosixPath(pattern).parts:
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def _on_or_under(rel: str, root: str) -> bool:
    return root == "" or rel == root or rel.startswith(root + "/")


def _on_the_way(rel: str, root: str) -> bool:
    return _on_or_under(rel, root) or root.startswith(rel + "/")


def _join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


# ── what is there ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    """One watched entry as it is now. ``sha256`` is what a baseline keeps:
    over its kind, its mode and its content (a link's: its target). ``base``
    and ``rel`` say where it is: the folder the walk held, and the path in
    it."""

    group: str
    kind: str                   # file | link | special | dir (git's own folders) | unreadable
    mode: str                   # git's mode for a file or a link, "" otherwise
    blob: str | None            # git's object id of its content (file, link)
    sha256: str
    base: str
    rel: str
    why: str | None = None      # why an unreadable entry couldn't be read

    @property
    def file(self) -> str:
        return os.path.join(self.base, self.rel)


@dataclass(frozen=True)
class Scan:
    """One walk's findings: the watched entries by their relative path,
    git's own by their names, and, when the folder held more than
    ``MAX_ENTRIES``, why only the files that matter most were checked."""

    found: dict[str, Entry]
    own: dict[str, Entry]
    partial: CheckFailed | None = None


def scan(folder: Path, watch: Watch, *, git_own: bool = True) -> Scan:
    """Blocking. The watched entries under ``folder`` (resolved, so the
    folders never watched compare), and git's own (none when it isn't a
    checkout, or ``git_own`` is False). Over ``MAX_ENTRIES``, a second walk
    keeps to the files that matter most (``Watch.priority``) and git's own."""
    folder = Path(os.path.realpath(folder))
    try:
        fd = os.open(folder, _DIR)
    except FileNotFoundError:
        raise CheckFailed(FOLDER_GONE, f"{folder} is gone, so its config files can't be checked.") from None
    except NotADirectoryError:
        raise CheckFailed(NOT_A_FOLDER, f"{folder} is no longer a folder, so its config files can't be "
                                        "checked.") from None
    except OSError as exc:
        raise _unreadable(folder, exc) from exc
    try:
        walk = _Walk(folder, watch)
        try:
            walk.walk(fd, git_own)
            return Scan(walk.found, walk.own)
        except _TooMany as over:
            first = over.rel
        walk = _Walk(folder, watch, priority_only=True)
        try:
            walk.walk(fd, git_own)
        except _TooMany as over:
            raise CheckFailed(TOO_MANY, f"There are more than {MAX_ENTRIES} entries under the watched paths in "
                                        f"{folder}, even among the config files that matter most (at {over.rel}); "
                                        "XO doesn't check a tree that large. Look at what is there.") from None
        return Scan(walk.found, walk.own, CheckFailed(
            TOO_MANY, f"There are more than {MAX_ENTRIES} entries under the watched paths in {folder} (at {first}): "
                      "too many to check them all. XO still checks the config files that matter most (each CLI's "
                      "settings, hooks and config files, the files named exactly, and git's own config and hooks); "
                      "the rest isn't checked. Look at what is there."))
    finally:
        os.close(fd)


class _TooMany(Exception):
    """A walk met more than ``MAX_ENTRIES`` entries, at ``rel``."""

    def __init__(self, rel: str) -> None:
        super().__init__(rel)
        self.rel = rel


class _Walk:
    """One check's walk: every read relative to a folder it holds open. An
    entry it can't read is kept as an ``unreadable`` entry, never raised.
    ``priority_only``: only the files that matter most (``Watch.priority``),
    and only they count toward the bound."""

    def __init__(self, folder: Path, watch: Watch, *, priority_only: bool = False) -> None:
        self.folder, self.watch, self.priority_only = folder, watch, priority_only
        self.found: dict[str, Entry] = {}
        self.own: dict[str, Entry] = {}
        self.seen = 0
        self.hashed = 0

    def walk(self, fd: int, git_own: bool) -> None:
        for root in self.watch.roots:
            self.root(fd, root)
        if git_own:
            self.own = self.git_own(fd)

    def root(self, fd: int, root: str) -> None:
        """Walk ``root``, down from the folder one sub-folder at a time: a
        link on the way is the entry, and nothing behind it is read."""
        if root == "":
            self.children(fd, "")
            return
        self.down(fd, "", PurePosixPath(root).parts)

    def down(self, fd: int, rel: str, parts: Sequence[str]) -> None:
        name, here = parts[0], _join(rel, parts[0])
        try:
            st = _lstat(fd, name)
        except OSError as exc:
            self.unread(here, exc)
            return
        if st is None:
            return
        if len(parts) == 1 or not stat.S_ISDIR(st.st_mode):
            self.visit(fd, name, here, st)
            return
        with _subdir(fd, name) as sub:
            if isinstance(sub, OSError):
                self.unread(here, sub)
            elif sub is not None:
                self.down(sub, here, parts[1:])

    def visit(self, fd: int, name: str, rel: str, st: os.stat_result) -> None:
        folder, link = stat.S_ISDIR(st.st_mode), stat.S_ISLNK(st.st_mode)
        if self.priority_only and not self.matters(rel, folder=folder, link=link):
            return
        self.count(rel)
        if self.watch.skipped(self.folder / rel):
            return
        if folder:
            if self.watch.descends(rel):
                with _subdir(fd, name) as sub:
                    if isinstance(sub, OSError):
                        self.unread(rel, sub)
                    elif sub is not None:
                        self.children(sub, rel)
            return
        group = self.watch.group(rel, link=link)
        if group is None:
            return
        entry = self.read(fd, name, group, str(self.folder), rel, link=link)
        if entry is not None:
            self.found[rel] = entry

    def children(self, fd: int, rel: str) -> None:
        try:
            listed = _listed(fd)
        except OSError as exc:
            if rel == "":
                raise _unreadable(self.folder, exc) from exc
            self.unread(rel, exc)
            return
        for name, st in listed:
            here = _join(rel, name)
            if isinstance(st, OSError):
                self.unread(here, st)
            else:
                self.visit(fd, name, here, st)

    def unread(self, rel: str, exc: OSError) -> None:
        """Keep ``rel``, which the walk couldn't read, as an unreadable entry
        when it is watched or on the way to a watched path."""
        group = self.watch.group(rel, link=True)
        if group is None or self.watch.skipped(self.folder / rel) or (
                self.priority_only and not (self.watch.priority(rel) or self.watch.priority_descends(rel))):
            return
        self.count(rel)
        self.found[rel] = _unread(group, str(self.folder), rel, exc)

    def matters(self, rel: str, *, folder: bool, link: bool) -> bool:
        """For the priority walk: a folder it opens, a file that matters
        most, or a link where either would be."""
        if folder:
            return self.watch.priority_descends(rel)
        return self.watch.priority(rel) or link and self.watch.priority_descends(rel)

    def git_own(self, fd: int) -> dict[str, Entry]:
        """git's own files for the folder (§2.1). The ``.git`` entry is one
        of them: a ``gitdir:`` file pointed elsewhere moves the config git
        uses. Only the folders named by a ``.git`` file and a ``commondir``
        are opened by path (their last component never through a link)."""
        own: dict[str, Entry] = {}
        base = str(self.folder)
        try:
            st = _lstat(fd, DOTGIT)
        except OSError as exc:
            return {DOTGIT: _unread(GIT, base, DOTGIT, exc)}
        if st is None:
            return own
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            if (entry := self.read(fd, DOTGIT, GIT, base, DOTGIT, link=stat.S_ISLNK(st.st_mode))) is not None:
                own[DOTGIT] = entry
            return own
        with contextlib.ExitStack() as stack:
            if stat.S_ISDIR(st.st_mode):
                gitdir, gitdir_fd = self.folder / DOTGIT, stack.enter_context(_subdir(fd, DOTGIT))
                if isinstance(gitdir_fd, OSError):
                    return {DOTGIT: _unread(GIT, base, DOTGIT, gitdir_fd)}
                own[DOTGIT] = _dir_entry(base, DOTGIT)
            else:
                if (entry := self.read(fd, DOTGIT, GIT, base, DOTGIT)) is None:
                    return own
                own[DOTGIT] = entry
                gitdir = _pointer(fd, DOTGIT, self.folder) if entry.kind != UNREADABLE_ENTRY else None
                gitdir_fd = stack.enter_context(_opened(gitdir)) if gitdir is not None else None
            if isinstance(gitdir_fd, OSError):
                own[GIT_CONFIG] = _unread(GIT, str(gitdir), "config", gitdir_fd)
                return own
            if gitdir_fd is None:
                return own
            common, common_fd = gitdir, gitdir_fd
            if (named := _pointer(gitdir_fd, "commondir", gitdir)) is not None:
                common, common_fd = named, stack.enter_context(_opened(named))
                if isinstance(common_fd, OSError):
                    own[GIT_CONFIG] = _unread(GIT, str(common), "config", common_fd)
                    return own
                if common_fd is None:
                    return own
            for key, at, where, name in ((GIT_CONFIG, common_fd, common, "config"),
                                         (GIT_WORKTREE_CONFIG, gitdir_fd, gitdir, "config.worktree")):
                try:
                    st = _lstat(at, name)
                except OSError as exc:
                    own[key] = _unread(GIT, str(where), name, exc)
                    continue
                if st is not None and (entry := self.read(at, name, GIT, str(where), name,
                                                          link=stat.S_ISLNK(st.st_mode))) is not None:
                    own[key] = entry
            try:
                st = _lstat(common_fd, "hooks")
            except OSError as exc:
                own[GIT_HOOKS] = _unread(GIT, str(common), "hooks", exc)
            else:
                if st is not None:
                    self.hooks(common_fd, "hooks", GIT_HOOKS, str(common), st, own)
        return own

    def hooks(self, fd: int, name: str, key: str, base: str, st: os.stat_result, into: dict[str, Entry]) -> None:
        """git's hooks folder: everything in it is git's own."""
        self.count(key)
        rel = "/".join(["hooks", *key.split("/")[1:]])
        if stat.S_ISDIR(st.st_mode):
            with _subdir(fd, name) as sub:
                if isinstance(sub, OSError):
                    into[key] = _unread(GIT, base, rel, sub)
                elif sub is not None:
                    try:
                        listed = _listed(sub)
                    except OSError as exc:
                        into[key] = _unread(GIT, base, rel, exc)
                        return
                    for child, child_st in listed:
                        if isinstance(child_st, OSError):
                            into[f"{key}/{child}"] = _unread(GIT, base, f"{rel}/{child}", child_st)
                        else:
                            self.hooks(sub, child, f"{key}/{child}", base, child_st, into)
            return
        if (entry := self.read(fd, name, GIT, base, rel, link=stat.S_ISLNK(st.st_mode))) is not None:
            into[key] = entry

    # ── reads, relative to a folder held open ────────────────────────────

    def read(self, fd: int, name: str, group: str, base: str, rel: str, *, link: bool = False) -> Entry | None:
        """The entry ``name`` in the open folder, hashed (an unreadable
        entry when it can't be read); None when it vanished meanwhile."""
        if link:
            return self.link(fd, name, group, base, rel)
        try:
            file_fd = os.open(name, _NO_FOLLOW, dir_fd=fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:            # made a link since its lstat
                return self.link(fd, name, group, base, rel)
            if exc.errno in _CHANGED_MEANWHILE:
                return None
            return _unread(group, base, rel, exc)
        try:
            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode):
                return Entry(group, "special", "", None, hashlib.sha256(
                    f"special {stat.S_IFMT(st.st_mode)}".encode()).hexdigest(), base, rel)
            mode = EXEC_MODE if st.st_mode & stat.S_IXUSR else FILE_MODE
            blob = hashlib.sha1(b"blob %d\0" % st.st_size, usedforsecurity=False)
            fingerprint = hashlib.sha256(f"file {mode}\0".encode())
            while chunk := os.read(file_fd, _CHUNK):
                self.hashed += len(chunk)
                if self.hashed > MAX_HASHED_BYTES:
                    raise CheckFailed(TOO_LARGE, f"The watched files in {self.folder} hold more than "
                                                 f"{MAX_HASHED_BYTES // (1024 * 1024)} MiB (at {rel}); XO doesn't "
                                                 "hash that much in one check. Look at what is there.")
                blob.update(chunk)
                fingerprint.update(chunk)
        except OSError as exc:
            return _unread(group, base, rel, exc)
        finally:
            os.close(file_fd)
        return Entry(group, "file", mode, blob.hexdigest(), fingerprint.hexdigest(), base, rel)

    def link(self, fd: int, name: str, group: str, base: str, rel: str) -> Entry | None:
        try:
            target = os.fsencode(os.readlink(name, dir_fd=fd))
        except OSError as exc:
            if exc.errno in (*_CHANGED_MEANWHILE, errno.EINVAL):   # gone, or no longer a link
                return None
            return _unread(group, base, rel, exc)
        blob = hashlib.sha1(b"blob %d\0" % len(target) + target, usedforsecurity=False).hexdigest()
        fingerprint = hashlib.sha256(f"link {LINK_MODE}\0".encode() + target).hexdigest()
        return Entry(group, "link", LINK_MODE, blob, fingerprint, base, rel)

    def count(self, rel: str) -> None:
        self.seen += 1
        if self.seen > MAX_ENTRIES:
            raise _TooMany(rel)


def _lstat(fd: int, name: str) -> os.stat_result | None:
    """``name`` in the open folder, never followed; None when it isn't there."""
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _listed(fd: int) -> list[tuple[str, os.stat_result | OSError]]:
    """An open folder's entries, sorted, each with its ``lstat`` or why it
    failed; one gone before its ``lstat`` wasn't there."""
    out: list[tuple[str, os.stat_result | OSError]] = []
    with os.scandir(fd) as entries:
        for entry in entries:
            try:
                out.append((entry.name, entry.stat(follow_symlinks=False)))
            except FileNotFoundError:
                continue
            except OSError as exc:
                out.append((entry.name, exc))
    return sorted(out, key=lambda item: item[0])


@contextlib.contextmanager
def _subdir(fd: int, name: str) -> Iterator[int | OSError | None]:
    """The sub-folder ``name`` of an open folder, never through a link;
    None when it changed meanwhile; why, when it can't be opened."""
    try:
        sub = os.open(name, _SUBDIR, dir_fd=fd)
    except OSError as exc:
        yield None if exc.errno in _CHANGED_MEANWHILE else exc
        return
    try:
        yield sub
    finally:
        os.close(sub)


@contextlib.contextmanager
def _opened(path: Path) -> Iterator[int | OSError | None]:
    """A git admin folder named by a pointer file; None when it isn't a
    folder (its last component is never followed); why, when it can't be
    opened."""
    try:
        fd = os.open(path, _SUBDIR)
    except OSError as exc:
        yield None if exc.errno in (*_CHANGED_MEANWHILE, errno.ENOTDIR) else exc
        return
    try:
        yield fd
    finally:
        os.close(fd)


def _pointer(fd: int, name: str, base: Path) -> Path | None:
    """The folder a ``.git`` file (``gitdir: <path>``) or a ``commondir``
    in the open folder names, relative to ``base``."""
    try:
        file_fd = os.open(name, _NO_FOLLOW, dir_fd=fd)
    except OSError:
        return None
    try:
        text = os.read(file_fd, MAX_POINTER_BYTES).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    finally:
        os.close(file_fd)
    text = text.removeprefix("gitdir:").strip()
    return Path(os.path.normpath(text if os.path.isabs(text) else base / text)) if text else None


def _dir_entry(base: str, rel: str) -> Entry:
    return Entry(GIT, "dir", "", None, hashlib.sha256(b"dir").hexdigest(), base, rel)


def _unread(group: str, base: str, rel: str, exc: OSError) -> Entry:
    return Entry(group, UNREADABLE_ENTRY, "", None, UNREADABLE_SHA256, base, rel,
                 why=exc.strerror or type(exc).__name__)


def _unreadable(path: Path, exc: OSError) -> CheckFailed:
    return CheckFailed(UNREADABLE, f"XO couldn't read {path} ({exc.strerror}), so the config files there can't be "
                                   "checked.")


# ── the base commit ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreeEntry:
    """A watched path in the base commit."""

    group: str
    mode: str
    blob: str
    size: int | None            # None for a submodule


async def base_tree(folder: Path, base_commit: str, watch: Watch) -> dict[str, TreeEntry]:
    """The base commit's watched paths, read from git's objects only (no
    filter, no index, nothing from the working tree, nothing fetched)."""
    roots = [r for r in watch.roots if r]
    args = ["ls-tree", "-r", "-z", "-l", base_commit]
    if len(roots) == len(watch.roots):
        args += ["--", *roots]
    result = await git(folder, *args)
    if result.binary_missing:
        raise CheckFailed(GIT_MISSING, "git isn't installed (or isn't on PATH), so the thread's worktree can't be "
                                       "compared with its base commit. Install git, then send again.")
    if not result.ok:
        why = (result.stderr or result.output).strip().splitlines()
        raise CheckFailed(BASE_UNREADABLE, f"git couldn't read the thread's base commit {base_commit[:12]} "
                                           f"({why[-1] if why else 'no output'}), so its worktree can't be "
                                           "compared with it.")
    tree: dict[str, TreeEntry] = {}
    for record in result.output.split("\0"):
        head, tab, path = record.partition("\t")
        fields = head.split()
        if not tab or len(fields) != 4:
            continue
        mode, _kind, blob, size = fields
        group = watch.group(path, link=mode == LINK_MODE)
        if group is not None and not watch.skipped(folder / path):
            tree[path] = TreeEntry(group, mode, blob, int(size) if size.isdigit() else None)
    return tree


# ── comparing ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    path: str
    group: str
    change: str                 # added | modified | deleted | unreadable
    sha256: str | None          # what is there now; None: nothing
    against: str                # base_commit | snapshot
    base: str | None = None     # where it is now (what is shown is read from there)
    rel: str | None = None
    why: str | None = None      # why an unreadable one couldn't be read

    @property
    def file(self) -> str | None:
        return os.path.join(self.base, self.rel) if self.base and self.rel else None


def against_commit(files: Mapping[str, Entry], tree: Mapping[str, TreeEntry], folder: Path) -> list[Change]:
    """Each watched path whose mode or content differs from the base
    commit's, or that only one of them has, and each that can't be read. A
    submodule is its own repository, and what is under a folder that can't
    be read is unknown: what is under either isn't compared."""
    hidden = [p for p, t in tree.items() if t.mode == GITLINK_MODE] + _unread_folders(files)
    changes = []
    for path in sorted({*files, *tree}):
        now, then = files.get(path), tree.get(path)
        if now is not None and now.kind == UNREADABLE_ENTRY:
            changes.append(_unreadable_change(path, now, "base_commit"))
            continue
        if any(_on_or_under(path, h) for h in hidden):
            continue
        if now is None:
            changes.append(Change(path, then.group, "deleted", None, "base_commit"))
        elif then is None:
            changes.append(Change(path, now.group, "added", now.sha256, "base_commit", now.base, now.rel))
        elif (now.mode, now.blob) != (then.mode, then.blob):
            changes.append(Change(path, now.group, "modified", now.sha256, "base_commit", now.base, now.rel))
    return changes


def against_snapshot(now: Mapping[str, Entry], baseline: Mapping[str, tuple[str, str]]) -> list[Change]:
    """Each entry whose hash differs from the baseline's (path → (sha256,
    group)), or that only one of them has, and each that can't be read
    (what is under a folder that can't be read isn't compared)."""
    hidden = _unread_folders(now)
    changes = []
    for path in sorted({*now, *baseline}):
        entry, (was, group) = now.get(path), baseline.get(path, (None, None))
        if entry is not None and entry.kind == UNREADABLE_ENTRY:
            changes.append(_unreadable_change(path, entry, "snapshot"))
            continue
        if any(_on_or_under(path, h) for h in hidden):
            continue
        if entry is None:
            changes.append(Change(path, group, "deleted", None, "snapshot"))
        elif was is None:
            changes.append(Change(path, entry.group, "added", entry.sha256, "snapshot", entry.base, entry.rel))
        elif entry.sha256 != was:
            changes.append(Change(path, entry.group, "modified", entry.sha256, "snapshot", entry.base, entry.rel))
    return changes


def _unread_folders(entries: Mapping[str, Entry]) -> list[str]:
    """The unreadable entries: what is under one (if it is a folder) is
    unknown."""
    return [path for path, entry in entries.items() if entry.kind == UNREADABLE_ENTRY]


def _unreadable_change(path: str, entry: Entry, against: str) -> Change:
    return Change(path, entry.group, UNREADABLE_ENTRY, entry.sha256, against, entry.base, entry.rel, why=entry.why)


@dataclass(frozen=True)
class Inspection:
    """What one check found: the changes; what is there now as a baseline
    would keep it (path → (sha256, group); never an entry it couldn't read);
    in a worktree, why it couldn't be compared with its base commit, if it
    couldn't (git's own files are still compared: a ``.git`` pointed
    elsewhere is one reason); and why only the files that matter most were
    checked, when the folder held too many entries (``partial``)."""

    changes: tuple[Change, ...]
    snapshot: dict[str, tuple[str, str]]
    tree: Mapping[str, TreeEntry] | None
    failed: CheckFailed | None = None
    partial: CheckFailed | None = None

    @property
    def failures(self) -> list[CheckFailed]:
        return [f for f in (self.failed, self.partial) if f is not None]

    @property
    def complete(self) -> bool:
        """Every watched path was compared: nothing failed, nothing was
        left unread."""
        return not self.failures and all(ch.change != UNREADABLE_ENTRY for ch in self.changes)


async def inspect(folder: Path, watch: Watch, *, base_commit: str | None,
                  baseline: Mapping[str, tuple[str, str]] | None,
                  main: tuple[Path, Watch] | None = None) -> Inspection:
    """The check, read-only: ``folder`` against its baseline. With
    ``base_commit`` (a thread's worktree) its watched files are compared with
    that commit, and git's own files and what ``main`` names (the project's
    main checkout, and what is watched there) with ``baseline``; without,
    everything is compared with ``baseline``. ``baseline`` None: none was
    taken yet, so only the commit is compared, and ``snapshot`` is what to
    keep as one. Over ``MAX_ENTRIES``, only the files that matter most are
    compared (``partial`` says so). ``CheckFailed`` when nothing can be
    compared: the folder is gone (also when it went during the check), or a
    bound was hit even so."""
    found = await asyncio.to_thread(scan, folder, watch)
    files, own, partial = found.found, found.own, found.partial
    if main is not None:
        main_found = await asyncio.to_thread(scan, *main, git_own=False)
        own = {**own, **{MAIN + path: entry for path, entry in main_found.found.items()}}
        partial = partial or main_found.partial
    if partial is not None and baseline is not None:
        baseline = {path: was for path, was in baseline.items() if _git_own(path) or watch.priority(path)}
    tree, failed = None, None
    changes: list[Change] = []
    kept = own if base_commit is not None else {**files, **own}
    if base_commit is not None:
        try:
            if own.get(DOTGIT) is None:
                raise CheckFailed(NOT_A_CHECKOUT, f"{folder} is no longer a git checkout (its .git is gone), so it "
                                                  "can't be compared with its base commit.")
            tree = await base_tree(folder, base_commit, watch)
        except CheckFailed as exc:
            failed = exc
        else:
            if partial is not None:
                tree = {path: entry for path, entry in tree.items() if watch.priority(path)}
            changes = against_commit(files, tree, folder)
    if not await asyncio.to_thread(os.path.isdir, folder):
        raise CheckFailed(FOLDER_GONE, f"{folder} is gone, so its config files can't be checked.")
    if baseline is not None:
        changes += against_snapshot(kept, baseline)
    else:               # none to compare with yet: what can't be read is said all the same
        changes += [_unreadable_change(path, entry, "snapshot") for path, entry in kept.items()
                    if entry.kind == UNREADABLE_ENTRY]
    return Inspection(tuple(sorted(changes, key=lambda ch: ch.path)),
                      {path: (entry.sha256, entry.group) for path, entry in kept.items()
                       if entry.kind != UNREADABLE_ENTRY}, tree, failed, partial)


def _git_own(path: str) -> bool:
    """A baseline's name for git's own files or the main checkout's."""
    return path == DOTGIT or path.startswith(("git:", MAIN))


# ── what an item shows ───────────────────────────────────────────────────


async def describe(folder: Path, changes: Sequence[Change], tree: Mapping[str, TreeEntry] | None) -> list[dict]:
    """Each change as an item's detail lists it, the first ``MAX_DIFFED``
    with a diff (redacted, then cut to ``MAX_DIFF_CHARS``) while the item's
    diffs stay under ``MAX_ITEM_DIFF_CHARS``. A path compared with a
    snapshot has no earlier version to show: only hashes are kept. What can't
    be read now is said in the diff, never raised: the change still shows."""
    out, budget = [], MAX_ITEM_DIFF_CHARS
    for index, change in enumerate(changes):
        shown = {"path": change.path, "group": change.group, "change": change.change, "sha256": change.sha256,
                 "against": change.against, "file": change.file, **({"why": change.why} if change.why else {})}
        if index < MAX_DIFFED and budget > 0:
            then = tree.get(change.path) if tree is not None else None
            shown["diff"] = _cut(redact(await _diff(folder, change, then)), min(MAX_DIFF_CHARS, budget))
            budget -= len(shown["diff"])
        out.append(shown)
    return out


async def _diff(folder: Path, change: Change, then: TreeEntry | None) -> str:
    if change.change == UNREADABLE_ENTRY:
        return f"(XO can't read it ({change.why}), so what it holds now isn't known.)\n"
    before = await _base_version(folder, then) if then is not None else _Shown()
    after = await asyncio.to_thread(_version, change.base, change.rel) if change.sha256 is not None \
        and change.base and change.rel else _Shown()
    lines = []
    if change.against == "snapshot" and change.change != "added":
        lines.append("(XO keeps only a hash of the earlier version, so it can't be shown.)\n")
    if before.mode and after.mode and before.mode != after.mode:
        lines.append(f"(mode {before.mode} → {after.mode})\n")
    lines += [f"({shown.note})\n" for shown in (before, after) if shown.note]
    old = before.text.splitlines(keepends=True) if before.text is not None else []
    new = after.text.splitlines(keepends=True) if after.text is not None else []
    lines += [line if line.endswith("\n") else line + "\n" for line in difflib.unified_diff(
        old, new, fromfile=f"a/{change.path}" if change.change != "added" else "/dev/null",
        tofile=f"b/{change.path}" if change.sha256 is not None else "/dev/null")]
    return "".join(lines)


@dataclass(frozen=True)
class _Shown:
    """One version of a path as a diff shows it: its text, or a note saying
    why it isn't shown; and its git mode."""

    text: str | None = None
    note: str | None = None
    mode: str | None = None


async def _base_version(folder: Path, then: TreeEntry) -> _Shown:
    if then.size is None:               # git couldn't read the object (never fetched: lazy fetching is off)
        return _Shown(note="git couldn't read the base commit's version", mode=then.mode)
    if then.size > MAX_SHOWN_BYTES:
        return _Shown(note=f"the base commit's version is {then.size} bytes, too large to show", mode=then.mode)
    result = await git(folder, "cat-file", "blob", then.blob)
    if not result.ok:
        return _Shown(note="git couldn't read the base commit's version", mode=then.mode)
    if then.mode == LINK_MODE:
        return _Shown(f"symlink to {result.output}\n", mode=then.mode)
    if "\0" in result.output or "�" in result.output:
        return _Shown(note=f"the base commit's version is binary ({then.size} bytes)", mode=then.mode)
    return _Shown(result.output, mode=then.mode)


def _version(base: str, rel: str) -> _Shown:
    """Blocking. What is at ``rel`` under ``base`` now, opened one folder at
    a time from ``base``, never through a link (a folder made a link since
    the scan shows nothing)."""
    parts = PurePosixPath(rel).parts
    try:
        with contextlib.ExitStack() as stack:
            fd = os.open(base, _DIR)
            stack.callback(os.close, fd)
            for part in parts[:-1]:
                fd = os.open(part, _SUBDIR, dir_fd=fd)
                stack.callback(os.close, fd)
            return _version_at(fd, parts[-1])
    except OSError as exc:
        if exc.errno in _CHANGED_MEANWHILE:
            return _Shown(note="changed again since the check, not shown")
        return _Shown(note=f"can't be read now ({exc.strerror})")


def _version_at(fd: int, name: str) -> _Shown:
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        return _Shown(f"symlink to {os.readlink(name, dir_fd=fd)}\n", mode=LINK_MODE)
    if not stat.S_ISREG(st.st_mode):
        return _Shown(note="now a special file, not shown")
    mode = EXEC_MODE if st.st_mode & stat.S_IXUSR else FILE_MODE
    if st.st_size > MAX_SHOWN_BYTES:
        return _Shown(note=f"now {st.st_size} bytes, too large to show", mode=mode)
    file_fd = os.open(name, _NO_FOLLOW, dir_fd=fd)
    try:
        data = os.read(file_fd, MAX_SHOWN_BYTES + 1)
    finally:
        os.close(file_fd)
    try:
        return _Shown(data.decode("utf-8"), mode=mode)
    except UnicodeDecodeError:
        return _Shown(note=f"now binary ({len(data)} bytes)", mode=mode)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… ({len(text) - limit} more characters not shown)\n"
