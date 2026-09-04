"""Router-facing facade for the relay. Raises typed errors; knows nothing
about HTTP. This is the only relay module the BFF imports, so route handlers
stay free of os/pathlib (BFF rule P2)."""
from __future__ import annotations

from services.cowork_agent.project_layout import project_dir, project_dir_exists, xo_projects_root

from services.swarm_api import project_sharing as swarm_client

from . import config, git_ops, poller, status
from .repo_identity import normalize_repo


class RelayError(Exception):
    status = 500
    code = "relay_error"
    message = "Relay error."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message


class ProjectNotFound(RelayError):
    status, code, message = 404, "project_not_found", "Project not found."


class NoGitOrigin(RelayError):
    status, code, message = 404, "no_git_origin", "This project has no git origin — nothing to share or sync."


class WorkspaceUnconfigured(RelayError):
    status, code, message = 409, "workspace_unconfigured", "This workspace has no XO_PROJECT_ID configured; sharing is disabled."


class SwarmError(RelayError):
    """A swarm 4xx passes through; anything else (network, 5xx, 0) is a 502."""

    def __init__(self, swarm_status: int, code: str, detail: str) -> None:
        self.status = swarm_status if 400 <= swarm_status < 500 else 502
        self.code = code
        super().__init__(detail or f"swarm returned {swarm_status}")


def status_snapshot() -> dict:
    snap = status.snapshot()
    snap["own_workspace_id"] = config.workspace_id()
    snap["watch_branch"] = config.watch_branch()
    # The UI builds the clone command for "shared with you" repos from this;
    # a clone anywhere else is invisible to the relay.
    snap["projects_root"] = str(xo_projects_root())
    return snap


async def _resolve_repo(project_id: str) -> str:
    if not project_dir_exists(project_id):
        raise ProjectNotFound()
    repo = normalize_repo(await git_ops.origin_url(project_dir(project_id)))
    if repo is None:
        raise NoGitOrigin()
    return repo


def _require_workspace_id() -> str:
    ws = config.workspace_id()
    if not ws:
        raise WorkspaceUnconfigured()
    return ws


async def project_commits(project_id: str, limit: int) -> dict:
    if not project_dir_exists(project_id):
        raise ProjectNotFound()
    d = project_dir(project_id)
    branch = config.watch_branch()
    commits, source = await git_ops.recent_commits(d, branch, limit)
    behind = await git_ops.behind_count(d, branch)
    return {"project_id": project_id, "branch": branch, "source": source,
            "behind": behind, "commits": commits}


async def members(project_id: str) -> dict:
    repo = await _resolve_repo(project_id)
    ok, code, payload = await swarm_client.members(repo)
    if not ok:
        raise SwarmError(code, "swarm_error", str(payload))
    return {"project_id": project_id, "repo": repo,
            "own_workspace_id": config.workspace_id(),
            "members": payload.get("members", [])}


async def share(project_id: str, workspace_id: str) -> dict:
    ws = _require_workspace_id()
    repo = await _resolve_repo(project_id)
    ok, code, detail = await swarm_client.share(repo, ws, workspace_id)
    if not ok:
        raise SwarmError(code, "share_failed", detail)
    poller.nudge()   # our own status flips to "shared" within a second
    return {"ok": True, "repo": repo}


async def revoke(project_id: str, workspace_id: str) -> dict:
    _require_workspace_id()
    repo = await _resolve_repo(project_id)
    ok, code, detail = await swarm_client.revoke(repo, workspace_id)
    if not ok:
        raise SwarmError(code, "revoke_failed", detail)
    poller.nudge()
    return {"ok": True, "repo": repo}
