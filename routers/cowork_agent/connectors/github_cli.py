"""
REST routes for the GitHub connector — CLI method (`gh auth login` device flow).

  POST /api/connectors/github/cli/start   — spawn `gh auth login`, return device code
                                            (or import a usable existing gh login; ?fresh=true forces the flow)
  POST /api/connectors/github/cli/poll    — poll until the user authorizes
  POST /api/connectors/github/cli/cancel  — abort an in-progress login
  GET  /api/connectors/github/cli/existing — this machine's gh login, without its token
  POST /api/connectors/github/cli/import  — connect with that login (repo + workflow scopes needed)

On success the token lands in the same store as a pasted PAT, so `/status`,
`/disconnect` and `/reconnect` (in github_pat.py) serve both methods.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.github import cli_auth as github_cli_auth

log = logging.getLogger(__name__)
router = APIRouter()


class CliSessionBody(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# POST /api/connectors/github/cli/start
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/cli/start")
async def cli_login_start(fresh: bool = False) -> JSONResponse:
    """Spawn `gh auth login --web` and return the device code + verification URL.

    The frontend should display `user_code` and a clickable link to
    `verification_uri`, then poll `/cli/poll` until status flips to `completed`.

    When gh is already signed in with the scopes the Space needs, that login
    is used instead: the answer has ``status: "connected"`` and
    ``imported: true`` with the connection, and no device code (polling its
    session returns the connection once). ``?fresh=true`` always starts the
    device flow, e.g. to switch accounts.
    """
    try:
        info = await github_cli_auth.start_login(fresh=fresh)
    except RuntimeError as exc:
        # Caller-actionable: gh missing, parse failure, concurrent session, etc.
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(info)


# ---------------------------------------------------------------------------
# POST /api/connectors/github/cli/poll
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/cli/poll")
async def cli_login_poll(body: CliSessionBody) -> JSONResponse:
    """Check the status of an in-progress CLI login.

    On completion, the token is validated against /user, persisted to
    token.json with `auth_method="cli"`, and the user profile is returned
    in the same shape as the PAT flow.
    """
    result = await github_cli_auth.connect(body.session_id)

    if result["ok"]:
        return JSONResponse(result["payload"])

    status = result.get("status")

    if status == "pending":
        return JSONResponse({
            "status": "pending",
            "user_code": result.get("user_code", ""),
            "verification_uri": result.get("verification_uri", ""),
        })

    if status == "not_found":
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired CLI login session. Start a new one.",
        )

    return JSONResponse(
        {"status": "failed", "error": result.get("error", "CLI login failed.")},
        status_code=502,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/github/cli/cancel
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/cli/cancel")
async def cli_login_cancel(body: CliSessionBody) -> JSONResponse:
    """Abort an in-progress CLI login."""
    result = await github_cli_auth.cancel_login(body.session_id)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# GET /api/connectors/github/cli/existing, POST /api/connectors/github/cli/import
# ---------------------------------------------------------------------------

@router.get("/api/connectors/github/cli/existing")
async def cli_existing_login() -> JSONResponse:
    """This machine's own `gh` login, never its token: ``logged_in``,
    ``username``, ``scopes``, ``missing`` (of repo, workflow) and ``usable``."""
    return JSONResponse(await github_cli_auth.existing_login())


@router.post("/api/connectors/github/cli/import")
async def cli_import_login() -> JSONResponse:
    """Connect GitHub with the existing `gh` login, so nobody signs in twice.

    200 with the connection (as `/cli/poll` returns it, plus ``imported``);
    409 ``gh_not_logged_in``, ``gh_missing_scopes`` (with ``missing``) or
    ``gh_token_invalid`` when it can't be used: then sign in with `/cli/start`.
    """
    result = await github_cli_auth.import_existing_login()
    if result["ok"]:
        return JSONResponse(result["payload"])
    detail = {k: v for k, v in result.items() if k != "ok"}
    raise HTTPException(status_code=409, detail=detail)
