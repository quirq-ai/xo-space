"""What a thread's worktree changed, or the project's own checkout
(``GET /changes``), with git's own patch for each file.

**The comparison.** A worktree is compared, uncommitted edits and untracked
files included (ignored ones left out), with the commit its thread started
from (``_thread_base``; the merge base with the default branch when that is
newer, i.e. the thread merged it in): what this thread changed. The project's own
checkout (no ``worktree``) is compared with its ``HEAD``: what is
uncommitted there. Space's own ``.xo/`` (its records, which its stores
write all the time, and the thread worktrees in ``.xo/worktrees``) is never
part of the project's own list: a cloned repository's new ``.xo/`` is not
the person's change. In a thread's worktree it is: there only an agent
writes it, and the person should see that.

**Without running repository code, and without trusting the index.**
``git diff`` and ``git status`` would run the clean filters, textconv and
external diff drivers that config and attributes name (an agent can write
``.gitattributes``), and trust index bits (``assume-unchanged``,
``skip-worktree``) that hide an edit; ``git add`` would run a clean filter
that ``.git/info/attributes`` selects. So no command here ever converts a
file: every git call runs through ``services.git_safe`` (no hooks, no
fsmonitor, no transport) and

* the checkout's files are hashed as they are (``hash-object --no-filters``,
  links by their target) into **a private index and object folder** in
  Space's state root (``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY`` with the
  repository's objects as an alternate), built from ``HEAD``'s tree and
  what is on disk now, so the repository's own index and objects are never
  read for state or written. A stat cache of Space's own (inode, size,
  mtime, ctime: an agent can set a time but not ctime) spares rehashing
  unchanged files;
* the diff runs with ``--attr-source`` set to the empty tree,
  ``--no-ext-diff --no-textconv --ignore-submodules=none``, and the config
  that changes what the output looks like pinned (prefixes, quoting,
  renames, context).

**Bounds.** At most ``FILES_MAX`` files (``truncated``, ``total_files``),
each patch at most ``PATCH_FILE_MAX`` (``PATCH_ONE_MAX`` when one ``path`` is
asked for), all patches together at most ``PATCH_BUDGET``; a file over
``PATCH_SOURCE_MAX`` on either side isn't diffed. Where a patch isn't sent,
``patch_omitted`` says why: ``too_large``, ``binary``, ``submodule``,
``response_budget`` or ``unavailable``.

**The patch** is the file's whole ``git diff -p`` block, from its ``diff
--git`` line (what @pierre/diffs parses), with rename, mode and ``\\ No
newline at end of file`` lines kept.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services import git_safe
from services.git_safe import ATTR_SOURCE_SINCE, default_branch, git, worktree_list
from services.project_files import edits
from services.project_files.address import EDIT_MAX_BYTES, FileRefused, project_root, version_of
from services.timestamps import now_iso
from services.xo_structure import WORKTREE_MARKER, WORKTREES_DIRNAME, XO_DIRNAME

FILES_MAX = 500
PATCH_FILE_MAX = 256 * 1024
PATCH_ONE_MAX = 1024 * 1024
PATCH_BUDGET = 4 * 1024 * 1024
PATCH_SOURCE_MAX = 2 * 1024 * 1024
#: A first build hashes the whole worktree; later ones only what changed.
TIMEOUT_S = 180.0
#: The private object folder is dropped (and rebuilt on demand) past this.
CACHE_MAX_BYTES = 256 * 1024 * 1024

GITLINK = "160000"
SYMLINK = "120000"
ABSENT = "000000"
_STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "added", "T": "type_changed"}

#: Config pinned for every call: what the output looks like, and nothing
#: that would convert or skip content.
_PINNED = (
    "-c", "core.attributesFile=/dev/null", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
    "-c", "core.safecrlf=false", "-c", "core.untrackedCache=false", "-c", "core.splitIndex=false",
    "-c", "index.sparse=false", "-c", "core.trustctime=true", "-c", "core.checkStat=default",
    "-c", "core.quotePath=false", "-c", "core.bigFileThreshold=512m", "-c", "add.ignoreErrors=true",
    "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false",
    "-c", "diff.renames=true", "-c", "diff.algorithm=histogram", "-c", "diff.submodule=short",
    "-c", "diff.ignoreSubmodules=none", "-c", "diff.suppressBlankEmpty=false", "-c", "color.ui=false",
    "-c", "diff.renameLimit=1000",
)
_DIFF = ("--no-ext-diff", "--no-textconv", "--no-color", "-M", "--no-relative", "--ignore-submodules=none")
_PATCH = ("-p", "-U3", "--inter-hunk-context=0", "--src-prefix=a/", "--dst-prefix=b/")

_locks: dict[str, asyncio.Lock] = {}


def not_found(code: str, message: str) -> FileRefused:
    return FileRefused(code, message, status=404)


@dataclass
class Change:
    status: str
    path: str
    old_path: str | None
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    additions: int | None = None
    deletions: int | None = None
    patch: str | None = None
    patch_omitted: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def binary(self) -> bool:
        return self.additions is None and GITLINK not in (self.old_mode, self.new_mode)


# ── git, the safe way ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Repo:
    folder: Path
    empty_tree: str
    env: dict[str, str]

    async def run(self, *args: str, input: bytes | None = None, check: str | None = None) -> str:
        result = await git(self.folder, f"--attr-source={self.empty_tree}", *_PINNED, *args, env=self.env,
                           input=input, timeout=TIMEOUT_S, log_output=False)
        if check is not None and not result.ok:
            why = (result.stderr or result.output).strip().splitlines()
            raise FileRefused("changes_unavailable", f"git couldn't {check}: {why[-1] if why else 'no output'}.",
                              status=500)
        return result.output if result.ok else ""


async def _out(folder: Path, *args: str) -> str | None:
    result = await git(folder, *args)
    return result.output.strip() if result.ok and result.output.strip() else None


# ── the route's service ───────────────────────────────────────────────────────


async def changes(project_id: str, worktree: str | None = None, path: str | None = None) -> dict[str, Any]:
    pid, root = project_root(project_id)
    if (have := await git_safe.version()) < ATTR_SOURCE_SINCE:
        need = ".".join(map(str, ATTR_SOURCE_SINCE))
        raise FileRefused("git_too_old", f"Showing changes needs git {need} or newer; this server has "
                                         f"{'.'.join(map(str, have)) if have != (0, 0) else 'an unknown version'}. "
                                         "Update git and try again.", status=501)
    folder, root_rel = await _folder(root, worktree)
    head = await _out(folder, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    empty_tree = await _out(folder, "hash-object", "-t", "tree", "--stdin")      # stdin is empty: nothing written
    if empty_tree is None:
        raise FileRefused("changes_unavailable", "git couldn't name the empty tree here.", status=500)
    base, base_ref, base_source = head, ("HEAD" if head else None), "head"
    if worktree is not None and head is not None:
        base, base_ref, base_source = await _thread_base(folder, head)
    common = await _out(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        raise not_found("not_a_repository", "This folder isn't a git repository.")

    key = f"{pid}:{worktree or ''}"
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cache = edits.state_dir(pid) / "changes" / ("worktrees/" + worktree if worktree else "project")
        # The project's own checkout leaves out Space's .xo/; a worktree only the other threads' checkouts.
        kept = f"{XO_DIRNAME}/" if worktree is None else f"{XO_DIRNAME}/{WORKTREES_DIRNAME}/"
        repo = await _prepare(cache, folder, Path(common) / "objects", head, empty_tree, kept)
        found = await _changed(repo, base or empty_tree)
        if path is not None:
            found = [c for c in found if path in (c.path, c.old_path)]
        total = len(found)
        listed = found[:FILES_MAX]
        await _patches(repo, base or empty_tree, listed, PATCH_ONE_MAX if path is not None else PATCH_FILE_MAX,
                       whole=path is None and total <= FILES_MAX)
        await asyncio.to_thread(_trim, cache)
    versions = await asyncio.to_thread(lambda: [_version(folder, change) for change in listed])
    for change, version in zip(listed, versions):
        change.extra["version"] = version
    return {
        "project_id": pid,
        "worktree": worktree,
        "root": root_rel,
        "base_ref": base_ref,
        "base_commit": base,
        "base_source": base_source,
        "head_commit": head,
        "computed_at": now_iso(),
        "truncated": total > len(listed),
        "total_files": total,
        "files": [_shape(c) for c in listed],
    }


#: Reflog entries that move a branch onto other work (not the thread's own
#: commits): where the thread's own work starts again.
_JUMPS = ("reset: moving to", "branch: Reset to", "rebase (start): checkout", "rebase -i (start): checkout")
#: What a rebase writes in the branch's own reflog (the ``(start)`` entry is
#: only in HEAD's): ``rebase (finish): refs/heads/b onto <sha>``. The entry's
#: sha is the rebased tip; where the thread's work starts again is ``onto``.
_REBASED = re.compile(r"(?:rebase|pull --rebase)\b.*?(?:\(finish\)|finished): \S+ onto ([0-9a-f]{40}(?:[0-9a-f]{24})?)$")
#: How many reflog entries are read (a bound on the git calls, newest kept).
REFLOG_MAX = 200


async def _thread_base(folder: Path, head: str) -> tuple[str, str | None, str]:
    """What a worktree's changes are measured from: ``(commit, ref, source)``.

    A thread's work is what it did since it started, so the base is where
    its branch started: XO's marker for the worktree (``thread_start``; in
    git's admin folder, which agents can't write), or the reflog entry that
    created the branch checked out now, when that is newer or the marker
    isn't on this branch's history (``branch_created``); then moved by the
    branch's own reflog to any point it was reset or rebased onto that isn't
    part of its history so far, or fast-forwarded to before its first own
    commit (``branch_moved``: an agent that put its branch on another line,
    as threads do right after they start). A fast-forward after the thread's
    own commits, and work merged in beside them, stay in: they are part of
    what the branch would bring.
    Resetting onto its own earlier commit (undoing one) doesn't move it.
    The merge base with the default branch (``merge_base``) is the base
    only when there is no start, or when it is newer (the thread merged
    the default branch in since: not its own work)."""
    entries = await _reflog(folder)
    start, source = await _marker_start(folder), "thread_start"
    created = await _commit_on_head(folder, entries[0][0]) \
        if entries and entries[0][1].startswith("branch: Created from") else None
    # The branch checked out now may not be the one XO made (an agent switched to a branch of its
    # own): the newer of the two starts is where this branch's work begins.
    if created is not None and created != start and (
            start is None or (await git(folder, "merge-base", "--is-ancestor", start, created)).ok
            or not (await git(folder, "merge-base", "--is-ancestor", created, start)).ok):
        start, source = created, "branch_created"
    if start is not None:
        seen, own = [start], False
        for sha, subject in entries:
            fast_forward = _fast_forward(subject)
            if (rebased := _REBASED.match(subject)) is not None:
                onto = rebased.group(1)
                if await _within(folder, onto, seen):
                    own = True                  # rewrote its own commits (a squash, a reword)
                else:
                    # Put on another line; the commits replayed on it are its own.
                    start, source, own = onto, "branch_moved", sha != onto
            elif (subject.startswith(_JUMPS) or fast_forward and not own) and not await _within(folder, sha, seen):
                start, source, own = sha, "branch_moved", False
            elif not fast_forward and not subject.startswith(("branch:", "reset:")):
                own = True              # a commit, a merge, a cherry-pick: the thread's own history begins
            seen.append(sha)
        start = await _commit_on_head(folder, start)
    ref = await default_branch(folder)
    merged = await _out(folder, "merge-base", "HEAD", ref) if ref else None
    name = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/") if ref else None
    if start is None:
        return (merged, name, "merge_base") if merged else (head, "HEAD", "head")
    if merged and merged != start and (await git(folder, "merge-base", "--is-ancestor", start, merged)).ok:
        return merged, name, "merge_base"
    return start, None, source


def _fast_forward(subject: str) -> bool:
    """``merge x: Fast-forward`` / ``pull: Fast-forward``."""
    return subject.startswith(("merge ", "pull")) and subject.endswith("Fast-forward")


async def _reflog(folder: Path) -> list[tuple[str, str]]:
    """The checked-out branch's reflog, oldest first: ``(sha, subject)``."""
    branch = await _out(folder, "symbolic-ref", "--quiet", "HEAD")
    if branch is None:
        return []
    result = await git(folder, "reflog", "show", f"-n{REFLOG_MAX}", "--format=%H%x09%gs", branch, "--")
    out = []
    for line in reversed(result.output.splitlines() if result.ok else []):
        sha, _, subject = line.partition("\t")
        if sha:
            out.append((sha, subject))
    return out


async def _within(folder: Path, sha: str, tips: list[str]) -> bool:
    """Whether ``sha`` is one of ``tips`` or an ancestor of one."""
    if sha in tips:
        return True
    result = await git(folder, "rev-list", "--max-count=1", sha, "--not", *dict.fromkeys(tips), "--")
    return result.ok and not result.output.strip()


async def _marker_start(folder: Path) -> str | None:
    admin = await _out(folder, "rev-parse", "--absolute-git-dir")
    if admin is None:
        return None
    try:
        with open(Path(admin) / WORKTREE_MARKER, "rb") as handle:
            marker = json.loads(handle.read(64 * 1024))
    except (OSError, ValueError):
        return None
    sha = marker.get("base_commit") if isinstance(marker, dict) else None
    return await _commit_on_head(folder, sha) if isinstance(sha, str) else None


async def _commit_on_head(folder: Path, sha: str) -> str | None:
    """``sha`` when it names a commit ``HEAD`` descends from, else None."""
    if not sha or not all(c in "0123456789abcdef" for c in sha.lower()):
        return None
    full = await _out(folder, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if full and (await git(folder, "merge-base", "--is-ancestor", full, "HEAD")).ok:
        return full
    return None


async def _folder(root: Path, worktree: str | None) -> tuple[Path, str]:
    """The checkout to compare, and its project-relative root."""
    if worktree is None:
        top = await _out(root, "rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != root:
            raise not_found("not_a_repository", "This project isn't a git repository.")
        return root, ""
    if not worktree or "/" in worktree or "\\" in worktree or worktree in (".", "..") or "\x00" in worktree:
        raise not_found("worktree_not_found", "There is no such worktree in this project.")
    folder = root / XO_DIRNAME / WORKTREES_DIRNAME / worktree
    try:
        st = os.lstat(folder)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise not_found("worktree_not_found", "There is no such worktree in this project (it may have been "
                                              "removed when its thread finished).")
    listed = await git(root, "worktree", "list", "--porcelain", "-z")
    known = {os.path.realpath(e.path) for e in worktree_list(listed.output)} if listed.ok else set()
    if os.path.realpath(folder) not in known:
        raise not_found("worktree_not_found", "That folder isn't one of this project's git worktrees.")
    return folder, f"{XO_DIRNAME}/{WORKTREES_DIRNAME}/{worktree}"


async def _prepare(cache: Path, folder: Path, objects: Path, head: str | None, empty_tree: str,
                   kept: str) -> _Repo:
    """A private index holding the checkout's files as they are now (see the
    module docstring): ``HEAD``'s tree, then each tracked and untracked
    (not ignored) file hashed without filters, a missing one removed.
    Everything under the ``kept`` prefix stays as committed (it is never
    listed as a change)."""
    (cache / "objects").mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("index", "index.lock"):             # rebuilt each time; a crash's leftover lock is ours
        (cache / name).unlink(missing_ok=True)
    repo = _Repo(folder, empty_tree, {
        "GIT_INDEX_FILE": str(cache / "index"),
        "GIT_OBJECT_DIRECTORY": str(cache / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
    })
    await repo.run("read-tree", head or empty_tree, check="read the checkout's commit")
    staged = await repo.run("ls-files", "-z", "--stage", check="list the checkout's files")
    tracked: dict[str, tuple[str, str]] = {}
    for record in filter(None, staged.split("\0")):
        meta, _, rel = record.partition("\t")
        mode, sha, _stage = meta.split(" ")
        tracked[rel] = (mode, sha)
    others = await repo.run("ls-files", "-z", "--others", "--exclude-standard", check="list the checkout's files")
    untracked = [rel for rel in others.split("\0")
                 if rel and not rel.endswith("/") and not rel.startswith(kept) and rel not in tracked]
    seen = await asyncio.to_thread(_stat_all, folder, [*tracked, *untracked])
    known = _load_stats(cache)
    entries: dict[str, tuple[str, str] | None] = {}
    to_hash: list[str] = []
    links: list[str] = []
    for rel, st in seen.items():
        if rel.startswith(kept):
            entries[rel] = tracked.get(rel)                          # Space's own, or other threads' checkouts
        elif st is None or stat.S_ISDIR(st.st_mode) and tracked.get(rel, ("",))[0] != GITLINK:
            entries[rel] = None                                     # gone, or a folder now: deleted
        elif tracked.get(rel, ("",))[0] == GITLINK:
            entries[rel] = (GITLINK, await _submodule_head(folder / rel) or tracked[rel][1])
        elif stat.S_ISLNK(st.st_mode):
            links.append(rel)
        elif stat.S_ISREG(st.st_mode):
            mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
            cached = known.get(rel)
            if cached is not None and cached[:-1] == _stamp(st):
                entries[rel] = (mode, cached[-1])
            else:
                to_hash.append(rel)
        else:
            entries[rel] = None                                     # a socket or a fifo: not content
    stamps: dict[str, list] = {}
    hashed = await _hash_files(repo, to_hash)
    for rel in to_hash:
        st = seen[rel]
        if rel in hashed:
            entries[rel] = ("100755" if st.st_mode & stat.S_IXUSR else "100644", hashed[rel])
            stamps[rel] = [*_stamp(st), hashed[rel]]
        else:
            entries[rel] = tracked.get(rel)                          # unreadable: left as committed
    for rel in links:
        try:
            target = os.readlink(folder / rel)
        except OSError:
            entries[rel] = tracked.get(rel)
            continue
        sha = (await repo.run("hash-object", "-w", "--no-filters", "--stdin", input=os.fsencode(target))).strip()
        entries[rel] = (SYMLINK, sha) if sha else tracked.get(rel)
    for rel, st in seen.items():
        if rel not in stamps and rel in known and entries.get(rel) and st is not None:
            stamps[rel] = known[rel]
    await asyncio.to_thread(_save_stats, cache, stamps)
    lines = []
    for rel, entry in entries.items():
        if entry == tracked.get(rel):
            continue
        mode, sha = entry if entry is not None else ("0", "0" * len(empty_tree))
        lines.append(f"{mode} {sha}\t{rel}")
    if lines:
        await repo.run("update-index", "-z", "--index-info", input="\0".join(lines).encode() + b"\0",
                       check="record the checkout's files")
    return repo


def _stamp(st: os.stat_result) -> list:
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode]


def _stat_all(folder: Path, rels: list[str]) -> dict[str, os.stat_result | None]:
    out: dict[str, os.stat_result | None] = {}
    for rel in rels:
        try:
            out[rel] = os.lstat(folder / rel)
        except OSError:
            out[rel] = None
    return out


def _load_stats(cache: Path) -> dict[str, list]:
    try:
        data = json.loads((cache / "stat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_stats(cache: Path, stamps: dict[str, list]) -> None:
    tmp = cache / "stat.json.tmp"
    tmp.write_text(json.dumps(stamps, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, cache / "stat.json")


async def _hash_files(repo: _Repo, rels: list[str]) -> dict[str, str]:
    """Each file's blob id, written to the private objects, as it is (no
    filter); a file that can't be read is left out."""
    out: dict[str, str] = {}
    # --stdin-paths reads a line per path and unquotes one that starts with a
    # double quote (a file named "a.md" would be hashed as a.md): those, and
    # names with a newline, go one at a time.
    special = [rel for rel in rels if "\n" in rel or rel.startswith('"')]
    batch = [rel for rel in rels if not ("\n" in rel or rel.startswith('"'))]
    if batch:
        shas = await _hash_batch(repo, batch)
        if shas is None:
            # One file that went away or can't be read fails the whole batch:
            # try once more without those (a run per file can be thousands).
            readable = [rel for rel in batch if os.access(repo.folder / rel, os.R_OK)]
            if 0 < len(readable) < len(batch) and (shas := await _hash_batch(repo, readable)) is not None:
                batch = readable
            else:
                special += batch
        if shas is not None:
            out.update(zip(batch, shas))
    for rel in special:
        if sha := (await repo.run("hash-object", "-w", "--no-filters", "--", rel)).strip():
            out[rel] = sha
    return out


async def _hash_batch(repo: _Repo, rels: list[str]) -> list[str] | None:
    """The blob ids of ``rels`` in one git run, or None when it failed."""
    result = await git(repo.folder, f"--attr-source={repo.empty_tree}", *_PINNED, "hash-object", "-w",
                       "--no-filters", "--stdin-paths", env=repo.env, input="\n".join(rels).encode(),
                       timeout=TIMEOUT_S, log_output=False)
    shas = result.output.split() if result.ok else []
    return shas if len(shas) == len(rels) else None


async def _submodule_head(path: Path) -> str | None:
    """A submodule's checked-out commit, read in its own folder only."""
    if not (path / ".git").exists():
        return None
    return await _out(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")


async def _changed(repo: _Repo, base: str) -> list[Change]:
    # --no-abbrev: the blob ids are looked up by their full names (_blob_sizes).
    out = await repo.run("diff", "--cached", *_DIFF, "--raw", "--no-abbrev", "--numstat", "-z", base, "--",
                         check="compare the checkout with its base")
    tokens = out.split("\0")
    found: list[Change] = []
    i = 0
    while i < len(tokens) and tokens[i].startswith(":"):
        meta = tokens[i][1:].split(" ")
        if len(meta) < 5:
            break
        old_mode, new_mode, old_blob, new_blob, letter = meta[0], meta[1], meta[2], meta[3], meta[4][:1]
        if letter in ("R", "C"):
            old, new = tokens[i + 1], tokens[i + 2]
            i += 3
        else:
            old, new = None, tokens[i + 1]
            i += 2
        found.append(Change(_STATUS.get(letter, "modified"), new, old if letter == "R" else None,
                            old_mode, new_mode, old_blob, new_blob))
    for change in found:                               # numstat records, in the same order
        if i >= len(tokens):
            break
        counts = tokens[i].split("\t")
        i += 3 if len(counts) == 3 and counts[2] == "" else 1
        if len(counts) >= 2 and counts[0].isdigit() and counts[1].isdigit():
            change.additions, change.deletions = int(counts[0]), int(counts[1])
    return found


async def _patches(repo: _Repo, base: str, listed: list[Change], cap: int, *, whole: bool) -> None:
    sizes = await _blob_sizes(repo, listed)
    wanted: list[Change] = []
    for change in listed:
        if GITLINK in (change.old_mode, change.new_mode):
            change.patch_omitted = "submodule"
        elif change.status == "type_changed":
            change.patch_omitted = "type_changed"   # git prints it as a deletion and an addition
        elif change.binary:
            change.patch_omitted = "binary"
        elif max(sizes.get(change.old_blob, 0), sizes.get(change.new_blob, 0)) > PATCH_SOURCE_MAX:
            change.patch_omitted = "too_large"
        else:
            wanted.append(change)
    if not wanted:
        return
    # The whole diff when every file is wanted (renames found exactly as listed); else just those paths
    # (at most 2 × FILES_MAX literal paths: well inside the argument limit).
    specs = [] if whole and len(wanted) == len(listed) else \
        [f":(literal,top){p}" for c in wanted for p in (c.old_path, c.path) if p]
    out = await repo.run("diff", "--cached", *_DIFF, *_PATCH, base, "--", *specs)
    blocks = await asyncio.to_thread(_blocks, out)          # megabytes of patch: off the loop
    budget = PATCH_BUDGET
    for change in wanted:
        block = blocks.get((change.old_path or change.path, change.path))
        if block is None:
            change.patch_omitted = "unavailable"
            continue
        size = len(block.encode("utf-8", errors="replace"))
        if size > cap:
            change.patch_omitted = "too_large"
        elif size > budget:
            change.patch_omitted = "response_budget"
        else:
            change.patch, budget = block, budget - size


def _blocks(out: str) -> dict[tuple[str, str], str]:
    """Each ``diff --git`` block by its ``(old path, new path)``. Lines are
    split on ``\\n`` only (a content line may hold any other control
    character), and a content line never starts with ``diff --git``: patch
    lines start with a space, ``+``, ``-`` or ``\\``."""
    pieces: list[list[str]] = []
    for line in out.split("\n"):
        if line.startswith("diff --git "):
            pieces.append([line])
        elif pieces:
            pieces[-1].append(line)
    # Joined once per block (appending to a str would copy it per line).
    blocks = ["\n".join(lines) + "\n" for lines in pieces]
    if blocks:
        blocks[-1] = blocks[-1][:-1]                # the split's own last, empty piece
    found: dict[tuple[str, str], str] = {}
    for block in blocks:
        paths = _block_paths(block)
        if paths is not None and paths not in found:
            found[paths] = block
        elif paths in found:
            del found[paths]                        # two blocks for one pair (a type change): neither is sent
    return found


def _block_paths(block: str) -> tuple[str, str] | None:
    """A block's ``(old path, new path)``, from its rename lines, or its
    header when the paths are the same (the only way it can be split
    unambiguously); git quotes a path with special characters C-style."""
    lines = block.split("\n")
    renamed = {key: _unquote(line[len(key) + 1:]) for line in lines[1:8]
               for key in ("rename from", "rename to") if line.startswith(key + " ")}
    if "rename from" in renamed and "rename to" in renamed:
        return renamed["rename from"], renamed["rename to"]
    header = lines[0][len("diff --git "):]
    if header.startswith('"'):
        first, rest = _quoted(header)
        second, _ = _quoted(rest.lstrip(" "))
        if first.startswith("a/") and second.startswith("b/"):
            return first[2:], second[2:]
        return None
    if (len(header) - 5) % 2:
        return None
    half = (len(header) - 5) // 2
    old, new = header[2:2 + half], header[half + 5:]
    if header.startswith("a/") and header[2 + half:half + 5] == " b/" and old == new:
        return old, new
    return None


_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _quoted(text: str) -> tuple[str, str]:
    """One C-quoted token at the start of ``text``: ``(value, the rest)``."""
    raw = bytearray()
    i = 1
    while i < len(text) and text[i] != '"':
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in "01234567" and i + 3 < len(text):
                raw.append(int(text[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            raw += _ESCAPES.get(nxt, nxt).encode()
            i += 2
            continue
        raw += char.encode()
        i += 1
    return raw.decode("utf-8", errors="replace"), text[i + 1:]


def _unquote(value: str) -> str:
    return _quoted(value)[0] if value.startswith('"') else value


async def _blob_sizes(repo: _Repo, listed: list[Change]) -> dict[str, int]:
    shas = sorted({b for c in listed for b in (c.old_blob, c.new_blob) if b.strip("0")}
                  - {b for c in listed for b, m in ((c.old_blob, c.old_mode), (c.new_blob, c.new_mode))
                     if m == GITLINK})
    if not shas:
        return {}
    out = await repo.run("cat-file", "--batch-check=%(objectname) %(objectsize)", input="\n".join(shas).encode())
    sizes = {}
    for line in out.splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[1].isdigit():
            sizes[parts[0]] = int(parts[1])
    return sizes


def _version(folder: Path, change: Change) -> str | None:
    """The new side's version on disk, for an edit started from the diff."""
    if change.status == "deleted" or change.new_mode in (SYMLINK, GITLINK, ABSENT):
        return None
    try:
        fd = os.open(folder / change.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > EDIT_MAX_BYTES:
            return None
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    return version_of(data) if len(data) <= EDIT_MAX_BYTES else None


def _shape(change: Change) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": change.path,
        "status": change.status,
        "additions": change.additions,
        "deletions": change.deletions,
        "binary": change.binary,
        "version": change.extra.get("version"),
        "patch": change.patch,
    }
    if change.old_path is not None:
        out["old_path"] = change.old_path
    if change.old_mode != change.new_mode and ABSENT not in (change.old_mode, change.new_mode):
        out.update(old_mode=change.old_mode, new_mode=change.new_mode)
    if change.patch is None:
        out["patch_omitted"] = change.patch_omitted or "unavailable"
    return out


def _trim(cache: Path) -> None:
    """Drop the private objects when they grow past ``CACHE_MAX_BYTES``
    (the next call rebuilds the index)."""
    total = 0
    for dirpath, _dirs, files in os.walk(cache / "objects"):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            if total > CACHE_MAX_BYTES:
                shutil.rmtree(cache, ignore_errors=True)
                return
