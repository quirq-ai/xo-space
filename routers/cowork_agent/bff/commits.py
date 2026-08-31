"""``/api/xo-projects/{id}/commits*`` — git history and commit snapshots.

Two questions the Space UI's snapshot view asks, both answered live from
the project's own repository via services/cowork_agent/git_snapshot:

- the commit list a project panel shows (newest first, bounded), and
- one commit's full tree — every file with its size, what the commit
  touched, and how many lines it changed there — which the UI renders as
  a clickable 3D citymap whose terrain height is the churn.

A project that is not its own git repository answers the list request
with ``{git: false, commits: []}`` (a truthful empty shape, matching the
activity endpoint's philosophy) and the snapshot request with 404: a
snapshot of a non-repo is not an empty thing, it is a thing that cannot
exist. Snapshots are immutable, so a small in-process cache serves
repeat views of the same commit without re-walking the tree.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.cowork_agent import git_snapshot
from services.cowork_agent.project_layout import project_dir, project_dir_exists

router = APIRouter()

_COMMITS_MAX = 100

# (project, sha) → snapshot. Immutable content, so no TTL — just a cap.
_snapshot_cache: dict[tuple[str, str], dict] = {}
_SNAPSHOT_CACHE_MAX = 8


class CommitListItem(BaseModel):
    sha: str
    short: str
    date: str
    subject: str
    files_changed: Optional[int] = None


class CommitListResponse(BaseModel):
    project_id: str
    git: bool
    commits: list[CommitListItem]


class SnapshotTreeEntry(BaseModel):
    path: str
    size: int


class FileChurn(BaseModel):
    """Lines this commit changed in one file; None for binary (uncountable)."""

    added: Optional[int] = None
    deleted: Optional[int] = None


class CommitSnapshotResponse(BaseModel):
    project_id: str
    commit: CommitListItem
    tree: list[SnapshotTreeEntry]
    touched: dict[str, str]
    churn: dict[str, FileChurn]
    deleted: list[str]
    truncated: bool
    total_files: int


def _require_project_dir(project_id: str):
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    return project_dir(project_id)


@router.get(
    "/api/xo-projects/{project_id}/commits",
    response_model=CommitListResponse,
)
def project_commits(
    project_id: str, limit: int = 40, day: Optional[str] = None
) -> CommitListResponse:
    """Newest-first commits for one project; empty shape for a non-repo.

    ``day`` (ISO date) narrows to the commits authored that day — the
    timeline's commit-day dots resolve to shas through this.
    """
    pdir = _require_project_dir(project_id)
    if day is not None:
        if not git_snapshot.valid_day(day):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD."},
            )
        commits = git_snapshot.commits_on_day(pdir, day)
    else:
        commits = git_snapshot.list_commits(pdir, limit=min(max(limit, 1), _COMMITS_MAX))
    if commits is None:
        return CommitListResponse(project_id=project_id, git=False, commits=[])
    return CommitListResponse(
        project_id=project_id,
        git=True,
        commits=[CommitListItem(**c) for c in commits],
    )


@router.get(
    "/api/xo-projects/{project_id}/commits/{sha}/snapshot",
    response_model=CommitSnapshotResponse,
)
def project_commit_snapshot(project_id: str, sha: str) -> CommitSnapshotResponse:
    """The full tree at one commit, sized and marked for the treemap."""
    pdir = _require_project_dir(project_id)
    ref = git_snapshot.normalize_sha(sha)
    if ref is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_sha", "message": "Not a commit id."},
        )
    key = (project_id, ref)
    snap = _snapshot_cache.get(key)
    if snap is None:
        snap = git_snapshot.commit_snapshot(pdir, ref)
        if snap is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "commit_not_found",
                    "message": "No such commit in this project's repository.",
                },
            )
        if len(_snapshot_cache) >= _SNAPSHOT_CACHE_MAX:
            _snapshot_cache.pop(next(iter(_snapshot_cache)))
        _snapshot_cache[key] = snap
    return CommitSnapshotResponse(
        project_id=project_id,
        commit=CommitListItem(**snap["commit"]),
        tree=[SnapshotTreeEntry(**e) for e in snap["tree"]],
        touched=snap["touched"],
        churn={p: FileChurn(**c) for p, c in snap["churn"].items()},
        deleted=snap["deleted"],
        truncated=snap["truncated"],
        total_files=snap["total_files"],
    )
