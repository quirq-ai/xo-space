"""Clone a repo that was shared with this workspace into the projects root.

Called by the relay loop, one repo per tick, when the swarm reports a member
repo as `available` (shared, not cloned here). Pure "given identity, produce a
folder or a reason": no loop, no status writes; the poller records the result.

Safety rules:
- the target is always a direct child of the projects root, named through
  `project_layout.resolve_project_dirname` so an existing `Trip-Planner` is
  found for identity `…/trip-planner` instead of cloned twice;
- git works in a hidden `.<name>.cloning` folder and the finished clone is
  renamed into place, so no scanner ever sees a half clone as a project;
- credentials travel in an HTTP header via `-c`, never in the URL or on disk;
- nothing from the repo is executed.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent import project_layout

from . import config, git_ops, state
from .repo_identity import normalize_repo

CLONING_SUFFIX = ".cloning"

_AUTH_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "403",
    "permission denied",
    "invalid username or token",
)
_NOT_FOUND_MARKERS = ("repository not found", "not found")


@dataclass(frozen=True)
class CloneResult:
    state: str            # cloned | already | exists | needs_auth | error
    project: str | None   # folder name under the projects root
    detail: str = ""
    had_token: bool = False

    @property
    def ok(self) -> bool:
        return self.state in ("cloned", "already")


def temp_dir_for(root: Path, dirname: str) -> Path:
    return root / f".{dirname}{CLONING_SUFFIX}"


def cleanup_stale_temp_dirs(root: Path | None = None) -> list[str]:
    """Delete leftover `.<name>.cloning` folders (a clone interrupted by a
    restart or a kill). Called once when the loop starts."""
    root = root or project_layout.xo_projects_root()
    removed: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.is_dir() and entry.name.startswith(".") and entry.name.endswith(CLONING_SUFFIX):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


async def _github_auth():
    """(auth or None, had_token). Lazy import: the sync package pulls httpx
    and the connector; a unit test of the naming logic must not need them."""
    try:
        from services.cowork_agent.xo_projects_sync.github import AuthMissingError, resolve_auth
    except Exception:  # noqa: BLE001 — sync package unavailable == no token
        return None, False
    try:
        return await resolve_auth(), True
    except AuthMissingError:
        return None, False
    except Exception:  # noqa: BLE001
        return None, False


def _config_args(repo: str, auth) -> list[str]:
    if auth is None:
        return []
    host = repo.split("/", 1)[0]
    if host != "github.com":
        return []  # the connector token is a GitHub token; other hosts go anonymous
    from services.cowork_agent.xo_projects_sync.github import _git_extraheader
    return ["-c", f"http.https://github.com/.extraheader={_git_extraheader(auth)}"]


def classify_failure(stderr: str, had_token: bool) -> str:
    text = (stderr or "").lower()
    if any(m in text for m in _AUTH_MARKERS):
        return "needs_auth"
    if not had_token and any(m in text for m in _NOT_FOUND_MARKERS):
        # GitHub answers 404 for a private repo when unauthenticated
        return "needs_auth"
    return "error"


def _last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


async def clone_shared_repo(repo: str) -> CloneResult:
    root = project_layout.xo_projects_root()
    name = repo.rsplit("/", 1)[-1] or "project"
    dirname = project_layout.resolve_project_dirname(name)
    target = root / dirname

    if target.exists():
        origin = normalize_repo(await git_ops.origin_url(target)) if (target / ".git").is_dir() else None
        if origin == repo:
            return CloneResult("already", dirname, "already cloned here")
        return CloneResult("exists", dirname,
                           f"a folder named {dirname!r} already exists here and is not this repo")

    tmp = temp_dir_for(root, dirname)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    auth, had_token = await _github_auth()
    url = f"https://{repo}.git"
    ok, stderr, timed_out = await git_ops.clone(
        url, tmp, config_args=_config_args(repo, auth),
        cwd=root, timeout=config.clone_timeout(),
    )
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        if timed_out:
            return CloneResult("error", dirname, f"timed out after {int(config.clone_timeout())}s", had_token)
        return CloneResult(classify_failure(stderr, had_token), dirname,
                           _last_line(stderr) or "git clone failed", had_token)
    try:
        tmp.rename(target)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return CloneResult("error", dirname, f"could not move clone into place: {exc}", had_token)
    # Remember that XO Space, not the user, put this folder here (survives restarts).
    state.save_cloned_at(repo, datetime.now(timezone.utc).isoformat())
    return CloneResult("cloned", dirname, "", had_token)
