"""The pre-turn integrity check, on disk (design ch 16 §2; owner §11.2,
§0.72): what a session's folder holds at the paths that make a CLI or git
run code on their own, compared with the session's baseline. No store and
no engine here: ``integrity_watch`` runs it in the engine's life, and the
doctor can call ``inspect`` itself.

**Watched** (§2.1): every runtime's ``project_config_paths`` (their union,
so core names no CLI); ``.xo/**``, except the thread worktrees under
``.xo/worktrees/`` and the documents XO's own stores write there (identity,
todos, work items, peers: data, never code, rewritten by XO whenever a todo
moves, so watching them would report XO's own writes); and git's own: the
folder's ``.git`` entry, the common dir's ``config`` and ``hooks/``, and the
worktree's ``config.worktree``. A CLI's own home and XO's state root are
never watched: a session in the person's home folder would otherwise watch
a CLI's whole home, which is the person's own (§2.6).

**Links are never followed.** Every entry is read with ``lstat``; a symlink
at a watched path or at a folder above one is itself the entry (its target
is what is hashed), and nothing behind it is read. Files are opened with
``O_NOFOLLOW``.

**Baselines** (§2.2). In a thread's worktree: ``base_commit`` for every
watched path, so a path the commit doesn't have must not be there (an
untracked or ignored file counts), compared object by object: ``git
ls-tree`` of the commit against git's blob id of each file on disk, mode
included. git's own files against a hash snapshot. Anywhere else: a hash
snapshot of everything watched. Only hashes are kept (§0.83).

**Not ``git diff`` or ``git status``**, the design's cost line: both run
the repository's clean filters on changed files, and a filter is a command
in git's config, which is one of the files checked here, so an agent that
wrote one would have XO run it; and both trust the index, where
``update-index --assume-unchanged`` or ``--skip-worktree`` hides an edit.
``ls-tree`` and ``cat-file`` only read objects. The cost stays one git call
plus a hash of each watched file.
"""

from __future__ import annotations

import asyncio
import difflib
import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from services.cowork_agent.storm.redact import redact
from services.cowork_agent.storm.review.paths import glob_match
from services.cowork_agent.storm.worktrees.git import git
from services.errors import ServiceError
from services.xo_structure import CANONICAL_FILES, OPTIONAL_FILES, WORKTREES_DIRNAME, XO_DIRNAME, XO_GITIGNORE

#: The groups a watched path belongs to (§2.1), as an item shows them.
PROJECT_CONFIG, XO, GIT = "project_config", "xo", "git"
#: Core's own watched paths; the CLIs' come from their feature lists.
CORE_WATCHED = (f"{XO_DIRNAME}/**",)
#: Inside ``.xo/``, what is never watched: thread worktrees, and the
#: documents XO itself writes.
XO_UNWATCHED = (f"{XO_DIRNAME}/{WORKTREES_DIRNAME}/**",
                *(f"{XO_DIRNAME}/{name}" for name in (*CANONICAL_FILES, *OPTIONAL_FILES, XO_GITIGNORE)))
#: git's own files, by the names an item shows (the ``.git`` entry is shown
#: as itself: it is in the session's folder).
DOTGIT, GIT_CONFIG, GIT_WORKTREE_CONFIG, GIT_HOOKS = ".git", "git:config", "git:config.worktree", "git:hooks"
#: git's modes for what a tree entry holds.
FILE_MODE, EXEC_MODE, LINK_MODE, GITLINK_MODE = "100644", "100755", "120000", "160000"
#: A walk that meets more entries than this stops, and the check says it
#: can't run there (a bound on one check's work, not a cap on the person's
#: files: a watched folder that big is itself worth a look).
MAX_ENTRIES = 5_000
#: An item shows a diff for this many changed paths; the rest are listed.
MAX_DIFFED = 20
#: A diff is cut after this many characters.
MAX_DIFF_CHARS = 8_000
#: A version larger than this is described, not shown.
MAX_SHOWN_BYTES = 256 * 1024
#: The longest ``.git`` file or ``commondir`` read: one line naming a folder.
MAX_POINTER_BYTES = 4096
_CHUNK = 1 << 16
_NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


class CheckFailed(ServiceError):
    """The check couldn't run (the folder is gone, git is missing, the base
    commit can't be read): reported like a change, never raised past the
    watch."""

    def __init__(self, message: str) -> None:
        super().__init__("integrity_check_failed", message, status=500)


# ── what is watched ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Watch:
    """The paths a check looks at: every runtime's project config, core's
    ``.xo/**``, and the folders never watched (``skip``: the CLIs' homes and
    XO's state root, resolved)."""

    project_config: tuple[str, ...]
    skip: tuple[Path, ...] = ()

    @property
    def patterns(self) -> tuple[tuple[str, str], ...]:
        """(pattern, group), the CLIs' first."""
        return (*((p, PROJECT_CONFIG) for p in self.project_config), *((p, XO) for p in CORE_WATCHED))

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
        if any(glob_match(path, p) for p in XO_UNWATCHED):
            return None
        for pattern, group in self.patterns:
            if glob_match(path, pattern) or (link and _on_the_way(rel, _root(pattern))):
                return group
        return None

    def descends(self, rel: str) -> bool:
        """Whether a folder can hold a watched path: on the way to or under
        a pattern's root, outside ``.git`` (git's own are read apart) and
        outside what ``.xo/`` leaves out."""
        path = PurePosixPath(rel)
        if path.parts[:1] == (DOTGIT,) or any(glob_match(path, p) for p in XO_UNWATCHED):
            return False
        return any(_on_the_way(rel, _root(pattern)) for pattern, _group in self.patterns)

    def skipped(self, path: Path) -> bool:
        return any(path == s or s in path.parents for s in self.skip)


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


# ── what is there ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    """One watched entry as it is now. ``sha256`` is what a baseline keeps:
    over its kind, its mode and its content (a link's: its target)."""

    group: str
    kind: str                   # file | link | special | dir (git's own folders)
    mode: str                   # git's mode for a file or a link, "" otherwise
    blob: str | None            # git's object id of its content (file, link)
    sha256: str
    file: str                   # its absolute path, for the report


def scan(folder: Path, watch: Watch) -> tuple[dict[str, Entry], dict[str, Entry]]:
    """Blocking. The watched entries under ``folder`` (resolved, so the
    folders never watched compare) by their relative path, and git's own by
    their names (empty when it isn't a checkout)."""
    folder = Path(os.path.realpath(folder))
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            raise CheckFailed(f"{folder} is no longer a folder, so its config files can't be checked.")
    except FileNotFoundError:
        raise CheckFailed(f"{folder} is gone, so its config files can't be checked.") from None
    walk = _Walk(folder, watch)
    for root in watch.roots:
        walk.root(root)
    return walk.found, _git_own(folder, walk)


class _Walk:
    def __init__(self, folder: Path, watch: Watch) -> None:
        self.folder, self.watch = folder, watch
        self.found: dict[str, Entry] = {}
        self.seen = 0

    def root(self, root: str) -> None:
        """Walk ``root``, first checking each folder on the way to it: a
        link there is the entry, and nothing behind it is read."""
        if root == "":
            self.children("", self.folder)
            return
        parts = PurePosixPath(root).parts
        for depth in range(1, len(parts) + 1):
            rel = "/".join(parts[:depth])
            path = self.folder / rel
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return
            if depth == len(parts) or not stat.S_ISDIR(st.st_mode):
                self.visit(rel, path, st)
                return

    def visit(self, rel: str, path: Path, st: os.stat_result) -> None:
        self.count(rel)
        if self.watch.skipped(path):
            return
        if stat.S_ISDIR(st.st_mode):
            if self.watch.descends(rel):
                self.children(rel, path)
            return
        link = stat.S_ISLNK(st.st_mode)
        group = self.watch.group(rel, link=link)
        if group is not None:
            self.found[rel] = _link(path, group) if link else _file(path, st, group)

    def children(self, rel: str, path: Path) -> None:
        for entry in _listed(path):
            self.visit(f"{rel}/{entry.name}" if rel else entry.name, Path(entry.path),
                       entry.stat(follow_symlinks=False))

    def hooks(self, name: str, path: Path, st: os.stat_result, into: dict[str, "Entry"]) -> None:
        """git's hooks folder: everything in it is git's own."""
        self.count(name)
        if stat.S_ISDIR(st.st_mode):
            for entry in _listed(path):
                self.hooks(f"{name}/{entry.name}", Path(entry.path), entry.stat(follow_symlinks=False), into)
        else:
            into[name] = _link(path, GIT) if stat.S_ISLNK(st.st_mode) else _file(path, st, GIT)

    def count(self, rel: str) -> None:
        self.seen += 1
        if self.seen > MAX_ENTRIES:
            raise CheckFailed(f"There are more than {MAX_ENTRIES} entries under the watched paths in {self.folder} "
                              f"(at {rel}); XO doesn't check a tree that large. Look at what is there.")


def _listed(path: Path) -> list[os.DirEntry]:
    try:
        return sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as exc:
        raise CheckFailed(f"XO couldn't list {path} ({exc.strerror}), so its config files can't be "
                          "checked.") from exc


def _git_own(folder: Path, walk: _Walk) -> dict[str, Entry]:
    """git's own files for ``folder`` (§2.1). The ``.git`` entry is one of
    them: a ``gitdir:`` file pointed elsewhere moves the config git uses."""
    own: dict[str, Entry] = {}
    dotgit = folder / DOTGIT
    if _entry(own, DOTGIT, dotgit) is None:
        return own
    kind = own[DOTGIT].kind
    gitdir = dotgit if kind == "dir" else _pointer(dotgit, folder) if kind == "file" else None
    if gitdir is None:
        return own
    common = gitdir
    if os.path.lexists(gitdir / "commondir"):
        common = _pointer(gitdir / "commondir", gitdir) or gitdir
    for name, path in ((GIT_CONFIG, common / "config"), (GIT_WORKTREE_CONFIG, gitdir / "config.worktree")):
        _entry(own, name, path)
    hooks = common / "hooks"
    try:
        walk.hooks(GIT_HOOKS, hooks, os.lstat(hooks), own)
    except FileNotFoundError:
        pass
    return own


def _entry(into: dict[str, Entry], name: str, path: Path) -> Path | None:
    """Record git's file ``path`` as ``name``; returns ``path`` when
    something is there."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(st.st_mode):
        into[name] = Entry(GIT, "dir", "", None, hashlib.sha256(b"dir").hexdigest(), str(path))
    elif stat.S_ISLNK(st.st_mode):
        into[name] = _link(path, GIT)
    else:
        into[name] = _file(path, st, GIT)
    return path


def _pointer(path: Path, base: Path) -> Path | None:
    """The folder a ``.git`` file (``gitdir: <path>``) or a ``commondir``
    names, relative to ``base``; never resolved through links."""
    try:
        fd = os.open(path, _NO_FOLLOW)
    except OSError:
        return None
    try:
        text = os.read(fd, MAX_POINTER_BYTES).decode("utf-8", errors="replace").strip()
    finally:
        os.close(fd)
    text = text.removeprefix("gitdir:").strip()
    if not text:
        return None
    named = Path(os.path.normpath(text if os.path.isabs(text) else base / text))
    try:
        if not stat.S_ISDIR(os.lstat(named).st_mode):
            return None             # a link or a file in its place: its config is never read through it
    except FileNotFoundError:
        return None
    return named


def _file(path: Path, st: os.stat_result, group: str) -> Entry:
    if not stat.S_ISREG(st.st_mode):
        return _special(path, st, group)
    try:
        fd = os.open(path, _NO_FOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:        # made a link since the lstat
            return _link(path, group)
        raise CheckFailed(f"XO couldn't read {path} ({exc.strerror}), so it can't be checked.") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _special(path, st, group)
        mode = EXEC_MODE if st.st_mode & stat.S_IXUSR else FILE_MODE
        blob = hashlib.sha1(b"blob %d\0" % st.st_size, usedforsecurity=False)
        fingerprint = hashlib.sha256(f"file {mode}\0".encode())
        while chunk := os.read(fd, _CHUNK):
            blob.update(chunk)
            fingerprint.update(chunk)
    except OSError as exc:
        raise CheckFailed(f"XO couldn't read {path} ({exc.strerror}), so it can't be checked.") from exc
    finally:
        os.close(fd)
    return Entry(group, "file", mode, blob.hexdigest(), fingerprint.hexdigest(), str(path))


def _link(path: Path, group: str) -> Entry:
    try:
        target = os.fsencode(os.readlink(path))
    except OSError as exc:
        raise CheckFailed(f"XO couldn't read the link {path} ({exc.strerror}), so it can't be checked.") from exc
    blob = hashlib.sha1(b"blob %d\0" % len(target) + target, usedforsecurity=False).hexdigest()
    fingerprint = hashlib.sha256(f"link {LINK_MODE}\0".encode() + target).hexdigest()
    return Entry(group, "link", LINK_MODE, blob, fingerprint, str(path))


def _special(path: Path, st: os.stat_result, group: str) -> Entry:
    """A FIFO, socket or device: never opened."""
    return Entry(group, "special", "", None, hashlib.sha256(f"special {stat.S_IFMT(st.st_mode)}".encode()).hexdigest(),
                 str(path))


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
    filter, no index, nothing from the working tree)."""
    roots = [r for r in watch.roots if r]
    args = ["ls-tree", "-r", "-z", "-l", base_commit]
    if len(roots) == len(watch.roots):
        args += ["--", *roots]
    result = await git(folder, *args)
    if result.binary_missing:
        raise CheckFailed("git isn't installed (or isn't on PATH), so the thread's worktree can't be compared with "
                          "its base commit. Install git, then send again.")
    if not result.ok:
        why = (result.stderr or result.output).strip().splitlines()
        raise CheckFailed(f"git couldn't read the thread's base commit {base_commit[:12]} "
                          f"({why[-1] if why else 'no output'}), so its worktree can't be compared with it.")
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
    change: str                 # added | modified | deleted
    sha256: str | None          # what is there now; None: nothing
    against: str                # base_commit | snapshot
    file: str | None            # its absolute path, when known


def against_commit(files: Mapping[str, Entry], tree: Mapping[str, TreeEntry], folder: Path) -> list[Change]:
    """Each watched path whose mode or content differs from the base
    commit's, or that only one of them has. A submodule is its own
    repository: what is under it isn't compared."""
    submodules = [p for p, t in tree.items() if t.mode == GITLINK_MODE]
    changes = []
    for path in sorted({*files, *tree}):
        if any(_on_or_under(path, s) for s in submodules):
            continue
        now, then = files.get(path), tree.get(path)
        if now is None:
            changes.append(Change(path, then.group, "deleted", None, "base_commit", str(folder / path)))
        elif then is None:
            changes.append(Change(path, now.group, "added", now.sha256, "base_commit", now.file))
        elif (now.mode, now.blob) != (then.mode, then.blob):
            changes.append(Change(path, now.group, "modified", now.sha256, "base_commit", now.file))
    return changes


def against_snapshot(now: Mapping[str, Entry], baseline: Mapping[str, tuple[str, str]]) -> list[Change]:
    """Each entry whose hash differs from the baseline's (path → (sha256,
    group)), or that only one of them has."""
    changes = []
    for path in sorted({*now, *baseline}):
        entry, (was, group) = now.get(path), baseline.get(path, (None, None))
        if entry is None:
            changes.append(Change(path, group, "deleted", None, "snapshot", None))
        elif was is None:
            changes.append(Change(path, entry.group, "added", entry.sha256, "snapshot", entry.file))
        elif entry.sha256 != was:
            changes.append(Change(path, entry.group, "modified", entry.sha256, "snapshot", entry.file))
    return changes


@dataclass(frozen=True)
class Inspection:
    """What one check found: the changes; what is there now as a baseline
    would keep it (path → (sha256, group)); and, in a worktree, why it
    couldn't be compared with its base commit, if it couldn't (git's own
    files are still compared: a ``.git`` pointed elsewhere is one reason)."""

    changes: tuple[Change, ...]
    snapshot: dict[str, tuple[str, str]]
    tree: Mapping[str, TreeEntry] | None
    failed: str | None = None


async def inspect(folder: Path, watch: Watch, *, base_commit: str | None,
                  baseline: Mapping[str, tuple[str, str]] | None) -> Inspection:
    """The check, read-only: ``folder`` against its baseline. With
    ``base_commit`` (a thread's worktree) its watched files are compared with
    that commit and git's own files with ``baseline``; without, everything is
    compared with ``baseline``. ``baseline`` None: none was taken yet, so
    only the commit is compared, and ``snapshot`` is what to keep as one.
    ``CheckFailed`` when nothing can be compared (the folder is gone)."""
    files, own = await asyncio.to_thread(scan, folder, watch)
    tree, failed = None, None
    changes: list[Change] = []
    kept = own if base_commit is not None else {**files, **own}
    if base_commit is not None:
        try:
            if own.get(DOTGIT) is None:
                raise CheckFailed(f"{folder} is no longer a git checkout (its .git is gone), so it can't be "
                                  "compared with its base commit.")
            tree = await base_tree(folder, base_commit, watch)
        except CheckFailed as exc:
            failed = exc.message
        else:
            changes = against_commit(files, tree, folder)
    if baseline is not None:
        changes += against_snapshot(kept, baseline)
    return Inspection(tuple(sorted(changes, key=lambda ch: ch.path)),
                      {path: (entry.sha256, entry.group) for path, entry in kept.items()}, tree, failed)


# ── what an item shows ───────────────────────────────────────────────────


async def describe(folder: Path, changes: Sequence[Change], tree: Mapping[str, TreeEntry] | None) -> list[dict]:
    """Each change as an item's detail lists it, the first ``MAX_DIFFED``
    with a diff (redacted, then cut to ``MAX_DIFF_CHARS``). A path compared
    with a snapshot has no earlier version to show: only hashes are kept."""
    out = []
    for index, change in enumerate(changes):
        shown = {"path": change.path, "group": change.group, "change": change.change, "sha256": change.sha256,
                 "against": change.against, "file": change.file}
        if index < MAX_DIFFED:
            then = tree.get(change.path) if tree is not None else None
            shown["diff"] = _cut(redact(await _diff(folder, change, then)))
        out.append(shown)
    return out


async def _diff(folder: Path, change: Change, then: TreeEntry | None) -> str:
    before = await _base_version(folder, then) if then is not None else _Shown()
    after = await asyncio.to_thread(_version, Path(change.file)) if change.sha256 is not None and change.file \
        else _Shown()
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
    if then.size is None or then.size > MAX_SHOWN_BYTES:
        return _Shown(note=f"the base commit's version is {then.size} bytes, too large to show", mode=then.mode)
    result = await git(folder, "cat-file", "blob", then.blob)
    if not result.ok:
        return _Shown(note="git couldn't read the base commit's version", mode=then.mode)
    if then.mode == LINK_MODE:
        return _Shown(f"symlink to {result.output}\n", mode=then.mode)
    if "\0" in result.output or "\ufffd" in result.output:
        return _Shown(note=f"the base commit's version is binary ({then.size} bytes)", mode=then.mode)
    return _Shown(result.output, mode=then.mode)


def _version(path: Path) -> _Shown:
    """Blocking. What is at ``path`` now, never read through a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _Shown()
    if stat.S_ISLNK(st.st_mode):
        return _Shown(f"symlink to {os.readlink(path)}\n", mode=LINK_MODE)
    if not stat.S_ISREG(st.st_mode):
        return _Shown(note="now a special file, not shown")
    mode = EXEC_MODE if st.st_mode & stat.S_IXUSR else FILE_MODE
    if st.st_size > MAX_SHOWN_BYTES:
        return _Shown(note=f"now {st.st_size} bytes, too large to show", mode=mode)
    try:
        fd = os.open(path, _NO_FOLLOW)
    except OSError as exc:
        return _Shown(note=f"can't be read now ({exc.strerror})", mode=mode)
    try:
        data = os.read(fd, MAX_SHOWN_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return _Shown(data.decode("utf-8"), mode=mode)
    except UnicodeDecodeError:
        return _Shown(note=f"now binary ({len(data)} bytes)", mode=mode)


def _cut(text: str) -> str:
    if len(text) <= MAX_DIFF_CHARS:
        return text
    return text[:MAX_DIFF_CHARS] + f"\n… ({len(text) - MAX_DIFF_CHARS} more characters not shown)\n"
