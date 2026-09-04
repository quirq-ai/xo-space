"""
REST routes for the GitHub connector — CLI method (`gh auth login` device flow).

  POST /api/connectors/github/cli/start   — spawn `gh auth login`, return device code
  POST /api/connectors/github/cli/poll    — poll until the user authorizes
  POST /api/connectors/github/cli/cancel  — abort an in-progress login

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
async def cli_login_start() -> JSONResponse:
    """Spawn `gh auth login --web` and return the device code + verification URL.

    The frontend should display `user_code` and a clickable link to
    `verification_uri`, then poll `/cli/poll` until status flips to `completed`.
    """
    try:
        info = await github_cli_auth.start_login()
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
