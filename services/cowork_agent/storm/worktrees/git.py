"""The git commands storm's worktrees run, through the one executor
(``utils.commands.run``), and the parsers for what they print.

Every command runs with the project's (or the tree's) parent as git's
ceiling, so a broken ``.git`` never makes git act on a repository above the
project, and with hooks and fsmonitor off: a hook or an fsmonitor command in
a repository is code an agent may have written, and XO's own git calls must
never run it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from services.errors import ServiceError
from utils.commands import CommandResult, run

#: A local git command that takes longer than this has hung (a lock held by
#: a crashed git, a stuck filesystem).
GIT_TIMEOUT_S = 60.0
#: ``git worktree add --relative-paths`` exists from this version on.
RELATIVE_PATHS_SINCE = (2, 48)
_SAFE = ("--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

_version: tuple[int, int] | None = None


class GitFailed(ServiceError):
    """A git command failed where nothing the person did explains it: worth
    retrying (the engine backs off and starts again)."""

    def __init__(self, what: str, result: CommandResult) -> None:
        why = (result.stderr or result.output).strip().splitlines()
        super().__init__("git_failed", f"git couldn't {what}: {why[-1] if why else 'no output'}. If this "
                                       "repeats, run that git command in the project to see why.", status=500)
        self.result = result


async def git(cwd: Path, *args: str) -> CommandResult:
    """Run ``git <args>`` in ``cwd``; the caller reads the result."""
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent), "GIT_TERMINAL_PROMPT": "0"}
    return await run(["git", *_SAFE, *args], cwd=cwd, timeout=GIT_TIMEOUT_S, env=env, separate_stderr=True)


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
        numbers = out.split()[-1].split(".") if out.split() else []
        _version = (int(numbers[0]), int(numbers[1])) if len(numbers) >= 2 and all(
            n.isdigit() for n in numbers[:2]) else (0, 0)
    return _version


async def relative_paths() -> bool:
    """Whether this git records a worktree's paths relative to each other,
    so the worktree survives a rename of the project folder (R-G §2)."""
    return await version() >= RELATIVE_PATHS_SINCE


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
