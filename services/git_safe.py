"""XO's own git commands, through the one executor (``utils.commands.run``),
and the parsers for what they print. Storm's worktrees and integrity check,
and Space's project files (history, changes), all run git through here; the
old ``storm/worktrees/git`` import path still resolves to this module.

Every command runs with the project's (or the tree's) parent as git's
ceiling, so a broken ``.git`` never makes git act on a repository above the
project, with hooks and fsmonitor off, and with no transport allowed: a
hook, an fsmonitor command, or a partial clone's transport
(``core.sshCommand``, ``remote.<r>.uploadpack``, run to fetch a missing
object) is code an agent may have written into the repository's config, and
XO's own git calls must never run it. ``GIT_NO_LAZY_FETCH`` (git 2.44) stops
the fetch before it starts; an older git ignores it, so
``GIT_ALLOW_PROTOCOL`` set to no protocol (git 2.6) refuses every
transport too, whatever the repository's ``protocol.<name>.allow`` says (the
environment wins over config; ``-c protocol.allow=never`` would not). None of
XO's own git commands needs a transport.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from services.errors import ServiceError
from utils.commands import CommandResult, run, run_sync

#: A local git command that takes longer than this has hung (a lock held by
#: a crashed git, a stuck filesystem).
GIT_TIMEOUT_S = 60.0
#: ``git worktree add --relative-paths`` exists from this version on.
RELATIVE_PATHS_SINCE = (2, 48)
#: ``--attr-source`` (what a project's changes are read with) exists from this version on.
ATTR_SOURCE_SINCE = (2, 40)
_SAFE = ("--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
#: The environment every XO git command runs with (beside its ceiling).
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}

_version: tuple[int, int] | None = None


class GitFailed(ServiceError):
    """A git command failed where nothing the person did explains it: worth
    retrying (the engine backs off and starts again)."""

    def __init__(self, what: str, result: CommandResult) -> None:
        why = (result.stderr or result.output).strip().splitlines()
        super().__init__("git_failed", f"git couldn't {what}: {why[-1] if why else 'no output'}. If this "
                                       "repeats, run that git command in the project to see why.", status=500)
        self.result = result


def _env(cwd: Path, extra: Mapping[str, str] | None) -> dict[str, str]:
    return {**os.environ, **_ENV, "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent), **(extra or {})}


async def git(cwd: Path, *args: str, env: Mapping[str, str] | None = None, input: bytes | None = None,
              timeout: float = GIT_TIMEOUT_S, log_output: bool = True) -> CommandResult:
    """Run ``git <args>`` in ``cwd``; the caller reads the result. ``env``
    adds variables (an index or object folder of the caller's own); the safe
    options come first on the command line, so it can't lift them.
    ``log_output`` False keeps a file's content (a diff, a version) out of
    the command log."""
    return await run(["git", *_SAFE, *args], cwd=cwd, timeout=timeout, env=_env(cwd, env), input=input,
                     separate_stderr=True, log_output=log_output)


def git_sync(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT_S, log_output: bool = True,
             ceiling: Path | None = None, env: Mapping[str, str] | None = None) -> CommandResult:
    """``git`` for synchronous callers (a route that runs in the threadpool).
    ``ceiling`` replaces ``cwd``'s parent as the folder git never looks in or
    above: a caller in a project's sub-folder passes the project's parent, so
    git still finds the project's own repository. ``env`` adds variables (a
    ``GIT_DIR`` of the caller's choosing), as for :func:`git`."""
    extra = {**({"GIT_CEILING_DIRECTORIES": str(ceiling)} if ceiling is not None else {}), **(env or {})} or None
    return run_sync(["git", *_SAFE, *args], cwd=cwd, timeout=timeout, env=_env(cwd, extra),
                    separate_stderr=True, log_output=log_output)


async def git_out(cwd: Path, what: str, *args: str) -> str:
    """``git <args>``'s output; ``GitFailed`` (saying it couldn't ``what``)
    when it fails."""
    result = await git(cwd, *args)
    if not result.ok:
        raise GitFailed(what, result)
    return result.output


async def version() -> tuple[int, int]:
    """git's (major, minor), read once per server."""
    global _version
    if _version is None:
        out = await git_out(Path.cwd(), "report its version", "version")
        # "git version 2.39.5 (Apple Git-154)": the number is not always last.
        match = re.search(r"\bversion (\d+)\.(\d+)", out)
        _version = (int(match[1]), int(match[2])) if match else (0, 0)
    return _version


async def relative_paths() -> bool:
    """Whether this git records a worktree's paths relative to each other,
    so the worktree survives a rename of the project folder (R-G §2)."""
    return await version() >= RELATIVE_PATHS_SINCE


async def default_branch(folder: Path) -> str | None:
    """The ref that holds the repository's default branch: ``origin/HEAD``'s
    branch, else ``main``, else ``master`` (each as a local branch, else its
    remote-tracking copy), else what the main checkout has checked out.
    None when none of them exists (an empty repository). Read only."""
    result = await git(folder, "for-each-ref", "--format=%(refname)\t%(symref)", "refs/remotes/origin/HEAD",
                       "refs/heads/main", "refs/heads/master", "refs/remotes/origin/main", "refs/remotes/origin/master")
    refs = dict(line.split("\t", 1) for line in result.output.splitlines() if "\t" in line) if result.ok else {}
    names = []
    origin_head = refs.get("refs/remotes/origin/HEAD") or ""
    if origin_head.startswith("refs/remotes/origin/"):
        names.append(origin_head.removeprefix("refs/remotes/origin/"))
    names += ["main", "master"]
    for name in names:
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if ref in refs or (await git(folder, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")).ok:
                return ref
    common = await git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.ok:
        head = await git(Path(common.output.strip()).parent, "symbolic-ref", "--quiet", "HEAD")
        if head.ok and head.output.strip():
            return head.output.strip()
    return None


@dataclass(frozen=True)
class Entry:
    """One worktree in ``git worktree list --porcelain``."""

    path: str
    branch: str | None = None           # without refs/heads/; None when detached
    locked: str | None = None           # the lock's reason ("" when locked without one)
    prunable: bool = False              # its folder is gone


def worktree_list(output: str) -> list[Entry]:
    """``git worktree list --porcelain -z``: records of NUL-ended
    ``key value`` fields, separated by an empty field. The main worktree is
    first."""
    entries: list[Entry] = []
    fields: dict[str, str] = {}
    for item in output.split("\0"):
        if item:
            key, _, value = item.partition(" ")
            fields[key] = value
            continue
        if "worktree" in fields:
            branch = fields.get("branch")
            entries.append(Entry(fields["worktree"], branch.removeprefix("refs/heads/") if branch else None,
                                 fields.get("locked"), "prunable" in fields))
        fields = {}
    return entries


@dataclass(frozen=True)
class Change:
    """One line of ``git status --porcelain``: ``code`` is ``??`` for an
    untracked file, ``!!`` for an ignored one, else the index and worktree
    states."""

    code: str
    path: str


def status(output: str) -> list[Change]:
    """``git status --porcelain=v1 -z``. A rename or copy carries its old
    path as the next field, which is skipped."""
    changes: list[Change] = []
    fields = iter(output.split("\0"))
    for item in fields:
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        changes.append(Change(code, path))
        if "R" in code or "C" in code:
            next(fields, None)
    return changes
