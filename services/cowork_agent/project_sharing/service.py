"""Router-facing facade for the relay. Raises typed errors; knows nothing
about HTTP. This is the only relay module the BFF imports, so route handlers
stay free of os/pathlib (BFF rule P2)."""
from __future__ import annotations

from services.cowork_agent.project_layout import project_dir, project_dir_exists, xo_projects_root

from services.swarm_api import project_sharing as swarm_client

from . import config, git_ops, poller, state, status
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
    status, code, message = 409, "workspace_unconfigured", "This workspace has no XO_SPACE_ID configured; sharing is disabled."


class ApplyFailed(RelayError):
    status, code, message = 409, "apply_failed", "Could not fast-forward: the branch has diverged or has local changes."


class BadSha(RelayError):
    status, code, message = 422, "bad_sha", "That is not a commit hash."


class BadPath(RelayError):
    status, code, message = 422, "bad_path", "That file is not part of this commit."


class CommitNotFound(RelayError):
    status, code, message = 404, "commit_not_found", "That commit is not on the shared branch here."


class ChangesUnreadable(RelayError):
    status, code, message = 409, "changes_unreadable", "Git could not read this commit's changes."


class SwarmError(RelayError):
    """A swarm 4xx passes through; anything else (network, 5xx, 0) is a 502."""

    def __init__(self, swarm_status: int, code: str, detail: str) -> None:
        self.status = swarm_status if 400 <= swarm_status < 500 else 502
        self.code = code
        super().__init__(detail or f"swarm returned {swarm_status}")


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
            "behind": behind, "commits": commits, "path": str(d)}


MAX_FILES = 200
MAX_DIFF_LINES = 400
MAX_DIFF_BYTES = 64 * 1024


def _cap_diff(text: str) -> tuple[str, bool]:
    """Hunks only (the file headers repeat what the UI already shows), cut at
    MAX_DIFF_LINES lines or MAX_DIFF_BYTES bytes, whichever comes first."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    body = lines[start:]
    cut = len(body) > MAX_DIFF_LINES
    out = "\n".join(body[:MAX_DIFF_LINES])
    raw = out.encode("utf-8")
    if len(raw) > MAX_DIFF_BYTES:
        out, cut = raw[:MAX_DIFF_BYTES].decode("utf-8", "ignore"), True
    return out, cut


async def commit_changes(project_id: str, sha: str, path: str | None = None) -> dict:
    """Files and one file's diff for a commit on origin/<branch>: the Sharing
    page's "What changed". Read-only. Only commits the relay fetched onto the
    watched branch are readable, and only paths that commit touched."""
    if not project_dir_exists(project_id):
        raise ProjectNotFound()
    sha = (sha or "").strip().lower()
    if not git_ops.SHA_RE.fullmatch(sha):
        raise BadSha()
    d = project_dir(project_id)
    branch = config.watch_branch()
    full = await git_ops.resolve_commit(d, sha)
    if not full or not await git_ops.on_branch(d, full, branch):
        raise CommitNotFound()
    parent = await git_ops.first_parent(d, full)
    files = await git_ops.commit_files(d, full, parent)
    if files is None:
        raise ChangesUnreadable()
    by_path = {f["path"]: f for f in files}
    if path and path not in by_path:
        raise BadPath()
    target = by_path[path] if path else next((f for f in files if not f["binary"]), None)
    diff = None
    if target is not None and target["binary"]:
        diff = {"path": target["path"], "text": "", "truncated": False, "binary": True}
    elif target is not None:
        raw = await git_ops.commit_diff(d, full, parent, target["path"])
        if raw is None:
            raise ChangesUnreadable()
        text, cut = _cap_diff(raw)
        diff = {"path": target["path"], "text": text, "truncated": cut, "binary": False}
    return {"project_id": project_id, "hash": full, "branch": branch,
            "files": files[:MAX_FILES], "files_truncated": len(files) > MAX_FILES, "diff": diff}


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
