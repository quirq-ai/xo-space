"""A git worktree per thread, inside the project, where the backend keeps its own.

``create`` adds ``<project>/<subdir>/thread-<ts>`` on a new branch
``xo/thread-<6 hex>`` from the project's current HEAD, locks it while the
thread lives, and lists ``<subdir>/`` in the repo's local ``.git/info/exclude``
(never a tracked file), so the worktree does not show up in the project's
``git status``. ``subdir`` comes from the backend's ``coordinator``
capability (``capability.worktrees_subdir``): Claude Code's is
``.claude/worktrees``; the default is ``.xo-worktrees``. A project that is not a git repository has no
worktree: its thread runs in the project folder, which is logged.

``remove`` deletes a worktree only when it is clean and its branch has no
commits that are not on another branch, the same rule Claude Code's own
bridge uses; otherwise it is kept and the reason returned.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from services.cowork_agent import project_layout
from services.cowork_agent.project_sharing.git_ops import _run

log = logging.getLogger(__name__)

BRANCH_PREFIX = "xo/thread-"


@dataclass
class Worktree:
    path: Path
    branch: Optional[str]      # None when the project is not a git repo
    isolated: bool             # False: the thread runs in the project folder itself


def _subdir(subdir: str) -> Path:
    path = Path(subdir)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"worktrees folder must be relative to the project: {subdir!r}")
    return path


def path_for(project: str, thread_ts: str, subdir: str) -> Path:
    return project_layout.project_dir(project) / _subdir(subdir) / f"thread-{thread_ts}"


async def _git_dir(repo: Path) -> Optional[Path]:
    code, out, _ = await _run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out.strip()) if code == 0 and out.strip() else None


async def _exclude(git_dir: Path, subdir: str) -> None:
    line = "/" + _subdir(subdir).as_posix().strip("/") + "/"
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if line not in current.splitlines():
        with exclude.open("a", encoding="utf-8") as fp:
            if current and not current.endswith("\n"):
                fp.write("\n")
            fp.write(f"# XO Space thread worktrees\n{line}\n")


async def create(project: str, thread_ts: str, subdir: str) -> Worktree:
    """The thread's worktree, created if needed (idempotent)."""
    repo = project_layout.project_dir(project)
    target = path_for(project, thread_ts, subdir)
    git_dir = await _git_dir(repo)
    if git_dir is None:
        log.warning("projects chat: %s is not a git repository; thread %s runs in the project folder",
                    project, thread_ts)
        return Worktree(repo, None, False)
    await _exclude(git_dir, subdir)
    if (target / ".git").exists():
        code, out, _ = await _run(target, "rev-parse", "--abbrev-ref", "HEAD")
        return Worktree(target, out.strip() if code == 0 else None, True)
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = BRANCH_PREFIX + secrets.token_hex(3)
    code, _, err = await _run(repo, "worktree", "add", "-b", branch, str(target), "HEAD")
    if code != 0:
        raise RuntimeError(f"could not create the thread's worktree: {err.strip()[:300]}")
    await _run(repo, "worktree", "lock", "--reason", f"XO Space thread {thread_ts}", str(target))
    return Worktree(target, branch, True)


async def remove(project: str, thread_ts: str, subdir: str) -> tuple[bool, str]:
    """``(removed, why)``: removed only when clean and nothing would be lost."""
    repo = project_layout.project_dir(project)
    target = path_for(project, thread_ts, subdir)
    if not target.exists():
        return True, "already gone"
    code, status, _ = await _run(target, "status", "--porcelain")
    if code != 0:
        return False, "git status failed"
    if status.strip():
        return False, "uncommitted changes"
    code, branch, _ = await _run(target, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip()
    if code == 0 and branch.startswith(BRANCH_PREFIX):
        code, ahead, _ = await _run(repo, "rev-list", "--count", branch, "--not", "--exclude", branch, "--branches")
        if code == 0 and ahead.strip() not in ("", "0"):
            return False, f"{ahead.strip()} commit(s) only on {branch}"
    await _run(repo, "worktree", "unlock", str(target))
    code, _, err = await _run(repo, "worktree", "remove", str(target))
    if code != 0:
        return False, err.strip()[:200] or "git worktree remove failed"
    if branch.startswith(BRANCH_PREFIX):
        await _run(repo, "branch", "-d", branch)
    return True, "removed"
