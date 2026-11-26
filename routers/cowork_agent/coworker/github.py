"""The coworker's Issues & PRs page: a project's pull requests, and one issue
or pull request as a person opens it. Served at the same
``/api/xo-projects/{id}/github/…`` URLs as the core's issue list, which the
page already calls; the logic is ``services.coworker.github``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from routers.cowork_agent.bff.errors import refused_response
from services.coworker import github
from services.errors import ServiceError

router = APIRouter()

_REFRESH = Query(default=False, description="Read GitHub now instead of the copy Space keeps.")


@router.get("/api/xo-projects/{project_id}/github/pulls")
async def project_github_pulls(project_id: str, refresh: bool = _REFRESH) -> Any:
    """``{project_id, repo, fetched_at?, open_total?, error?, pulls: [...]}``
    from the poll's mirror; a key whose value is unknown is left out."""
    try:
        return await github.pulls(project_id, refresh=refresh)
    except ServiceError as exc:
        return refused_response(exc)


@router.get("/api/xo-projects/{project_id}/github/issues/{number}")
async def project_github_issue(project_id: str, number: int, refresh: bool = _REFRESH) -> Any:
    """``{number, body, labels, comments: [{author, body, created_at}], …}``;
    404 ``issue_not_found`` when the repository has no such issue."""
    try:
        return await github.item(project_id, number, pull=False, refresh=refresh)
    except ServiceError as exc:
        return refused_response(exc)


@router.get("/api/xo-projects/{project_id}/github/pulls/{number}")
async def project_github_pull(project_id: str, number: int, refresh: bool = _REFRESH) -> Any:
    """An issue's answer plus ``checks: [{name, state}]``, ``mergeable`` and
    ``conflicts: [path]`` (left out when the files can't be named); 404
    ``pull_not_found`` when there is no such pull request."""
    try:
        return await github.item(project_id, number, pull=True, refresh=refresh)
    except ServiceError as exc:
        return refused_response(exc)
