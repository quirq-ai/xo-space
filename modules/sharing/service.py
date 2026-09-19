"""Router-facing facade for the relay, and the only surface other modules
call. Raises typed errors (each a :class:`services.errors.ServiceError`
carrying its own status); knows nothing about HTTP. This is the only relay
module ``routes.py`` imports, so route handlers stay free of os/pathlib
(BFF rule P2)."""
from __future__ import annotations

from services.cowork_agent.project_layout import project_dir, project_dir_exists, xo_projects_root
from services.errors import ServiceError

from services.swarm_api import project_sharing as swarm_client

from . import config, git_ops, poller, state, status
from .repo_identity import normalize_repo


class RelayError(ServiceError):
    """The class attributes are the defaults a subclass overrides; the
    instance carries the same three fields the app handler serves."""

    status = 500
    code = "relay_error"
    message = "Relay error."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(self.code, message or self.message, self.status)


class ProjectNotFound(RelayError):
    status, code, message = 404, "project_not_found", "Project not found."


class NoGitOrigin(RelayError):
    status, code, message = 404, "no_git_origin", "This project has no git origin — nothing to share or sync."


class WorkspaceUnconfigured(RelayError):
    status, code, message = 409, "workspace_unconfigured", "This workspace has no XO_SPACE_ID configured; sharing is disabled."


class ApplyFailed(RelayError):
    status, code, message = 409, "apply_failed", "Could not fast-forward: the branch has diverged or has local changes."


class SwarmError(RelayError):
    """A swarm 4xx passes through; anything else (network, 5xx, 0) is a 502."""

    def __init__(self, swarm_status: int, code: str, detail: str) -> None:
        ServiceError.__init__(
            self, code, detail or f"swarm returned {swarm_status}",
            swarm_status if 400 <= swarm_status < 500 else 502,
        )


def status_snapshot() -> dict:
    snap = status.snapshot()
    root = xo_projects_root()
    snap["own_workspace_id"] = config.workspace_id()
    snap["watch_branch"] = config.watch_branch()
    # "did XO Space clone this?" comes from the per-repo state file, so it is
    # still answerable after a restart when the in-memory event is gone.
    for repo, entry in snap.get("repos", {}).items():
        entry["auto_cloned_at"] = state.load_cloned_at(repo)
        entry["auto_clone_suppressed"] = state.is_removed(repo, root)
        project = entry.get("project")
        if entry["auto_clone_suppressed"] and project and not (root / project).is_dir():
            # A poll can still carry its previous local mapping when deletion
            # completes. Keep remote membership visible without a ghost clone.
            entry["project"] = None
            entry["available"] = bool(entry.get("shared"))
    # The UI builds the clone command for "shared with you" repos from this;
    # a clone anywhere else is invisible to the relay.
    snap["projects_root"] = str(root)
    # The page spec (pages/activity.json) reads a list, one row per repo,
    # with one word for the badge; the dict above stays for the legacy view.
    snap["parked"] = snap.get("cadence") != "running"
    snap["rows"] = [_row(repo, entry) for repo, entry in sorted(snap.get("repos", {}).items())]
    return snap


def _repo_state(entry: dict) -> str:
    """One phrase for a repo's row: what the relay is doing with it."""
    clone = entry.get("clone") or {}
    if clone.get("state") == "cloning":
        return "cloning"
    if clone.get("state"):
        return "clone failed"
    if entry.get("last_error"):
        return "error"
    if entry.get("available"):
        return "shared with you"
    if entry.get("shared"):
        return "in sync" if entry.get("project") else "shared"
    return "not shared"


def _row(repo: str, entry: dict) -> dict:
    return {"repo": repo, **entry, "state": _repo_state(entry),
            "last_synced": entry.get("last_fetch_at")}


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
            "behind": behind, "commits": commits, "path": str(d)}


async def apply(project_id: str) -> dict:
    """Fast-forward HEAD to origin/<branch>: the "Apply" button. The relay
    fetches on its own; this is the one step that was a copy-pasted command.
    Only ever --ff-only, so a diverged branch or dirty tree is refused by git
    and surfaces as apply_failed with git's own reason."""
    if not project_dir_exists(project_id):
        raise ProjectNotFound()
    d = project_dir(project_id)
    branch = config.watch_branch()
    behind = await git_ops.behind_count(d, branch)
    if behind is None:
        raise ApplyFailed(f"origin/{branch} is not known here yet — nothing has been fetched.")
    if behind == 0:
        return {"project_id": project_id, "branch": branch, "applied": 0,
                "head": await git_ops.head_sha(d)}
    ok, detail = await git_ops.apply_ff(d, branch)
    if not ok:
        raise ApplyFailed(detail or None)
    poller.nudge()   # the behind count in the next status snapshot drops to 0
    return {"project_id": project_id, "branch": branch, "applied": behind,
            "head": await git_ops.head_sha(d)}


def check_now() -> dict:
    """The "Check now" button: run the relay's next tick as soon as possible
    instead of waiting out the minute. Harmless while parked."""
    poller.nudge()
    return {"ok": True, "cadence": status.snapshot().get("cadence")}


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
