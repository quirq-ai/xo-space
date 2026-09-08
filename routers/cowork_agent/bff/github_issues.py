"""BFF routes for a project's GitHub issues, read through `gh`.

  GET /api/xo-projects/{id}/issues?state=open&limit=30   number / title / state per issue
  GET /api/xo-projects/{id}/issues/{number}              full issue, comments included

Declarative over services.cowork_agent.github_issues (typed errors → HTTP
here). No os/pathlib in this module (BFF rule P2); the state predicate lives
in filters.py (rule P4).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from routers.cowork_agent.bff.filters import is_valid_issue_state
from services.cowork_agent import github_issues as service

router = APIRouter()


def _http(exc: service.IssuesError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/api/xo-projects/{project_id}/issues")
async def project_issues(project_id: str, state: str = "open", limit: int = 30) -> dict:
    if not is_valid_issue_state(state):
        raise HTTPException(status_code=422, detail={
            "code": "bad_state", "message": "state must be open, closed or all."})
    try:
        return await service.list_issues(project_id, state, limit)
    except service.IssuesError as exc:
        raise _http(exc)


@router.get("/api/xo-projects/{project_id}/issues/{number}")
async def project_issue(project_id: str, number: int) -> dict:
    if number < 1:
        raise HTTPException(status_code=422, detail={
            "code": "bad_number", "message": "Issue numbers start at 1."})
    try:
        return await service.get_issue(project_id, number)
    except service.IssuesError as exc:
        raise _http(exc)
