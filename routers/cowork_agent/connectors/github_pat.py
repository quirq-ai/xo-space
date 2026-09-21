"""
REST routes for the GitHub connector — PAT method (paste a personal access token).

  POST /api/connectors/github/token       — receive & validate a PAT
  GET  /api/connectors/github/status      — current connection status
  POST /api/connectors/github/disconnect  — delete stored token
  POST /api/connectors/github/reconnect   — re-validate stored token
  GET  /api/connectors/github/repos       — reachable repos + current selection
  PUT  /api/connectors/github/repos       — choose all or selected repositories

Only one GitHub identity is connected at a time; the `gh auth login` device
flow is the other way to establish it (see github_cli.py). `/status`,
`/disconnect` and `/reconnect` are method-agnostic and live here because they
operate on the stored token regardless of how it was obtained.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.github import (
    delete_github_token,
    get_github_token,
    get_status,
    validate_token,
)
from services.cowork_agent.connectors.github import git_credential as github_git_credential
from services.cowork_agent.connectors.github import pat as github_pat
from services.cowork_agent.connectors.github import repo_access as github_repo_access

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/connectors/github/token
# ---------------------------------------------------------------------------

class TokenBody(BaseModel):
    token: str


@router.post("/api/connectors/github/token")
async def submit_github_token(body: TokenBody) -> JSONResponse:
    """Validate a GitHub PAT, store it, and return the connection status."""
    token = body.token.strip()
    if not token:
        raise HTTPException(400, detail="Token cannot be empty.")

    if not github_pat.looks_like_token(token):
        raise HTTPException(400, detail="This doesn't look like a valid GitHub token.")

    result = await github_pat.connect(token)

    if result["ok"]:
        return JSONResponse(result["payload"])

    return JSONResponse(
        {"status": result["status"], "error": result.get("error", "Validation failed.")},
        status_code=400 if result["status"] == "needs_auth" else 502,
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/github/status
# ---------------------------------------------------------------------------

@router.get("/api/connectors/github/status")
async def github_status() -> JSONResponse:
    """Return the current GitHub connector status."""
    status = await get_status()
    if status.get("status") == "connected":
        status["repo_access"] = github_repo_access.get_repo_access()
    return JSONResponse(status)


# ---------------------------------------------------------------------------
# POST /api/connectors/github/disconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/disconnect")
async def disconnect_github() -> JSONResponse:
    """Delete the stored GitHub token and clear the connection."""
    delete_github_token()
    await github_git_credential.apply_policy()
    return JSONResponse({"status": "needs_auth"})


# ---------------------------------------------------------------------------
# POST /api/connectors/github/reconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/reconnect")
async def reconnect_github() -> JSONResponse:
    """Re-validate the stored token and return the new status."""
    token = get_github_token()
    if not token:
        return JSONResponse({"status": "needs_auth", "error": "No token stored."})

    result = await validate_token(token)
    if result.get("valid"):
        return JSONResponse({
            "status": "connected",
            "username": result.get("username", ""),
            "name": result.get("name", ""),
            "avatar_url": result.get("avatar_url", ""),
            "scopes": result.get("scopes", ""),
        })
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "")},
            status_code=502,
        )


# ---------------------------------------------------------------------------
# GET / PUT /api/connectors/github/repos
# ---------------------------------------------------------------------------

class RepoAccessBody(BaseModel):
    mode: Literal["all", "selected"]
    repos: list[str] = []


@router.get("/api/connectors/github/repos")
async def github_repos() -> JSONResponse:
    """List the repositories the account can reach, with the current selection."""
    try:
        repos = await github_repo_access.list_accessible_repos()
    except github_repo_access.RepoAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({
        "repos": repos,
        "repo_access": github_repo_access.get_repo_access(),
    })


@router.put("/api/connectors/github/repos")
async def set_github_repos(body: RepoAccessBody) -> JSONResponse:
    """Allow every repository, or only the ones listed."""
    try:
        access = github_repo_access.set_repo_access(body.mode, body.repos)
    except github_repo_access.RepoAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Plain `git`/`gh` in a terminal never consult the allowlist; this does.
    await github_git_credential.apply_policy()
    return JSONResponse({"repo_access": access})
