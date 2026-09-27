"""
REST routes for the GitHub connector — XO GitHub App method.

  POST /api/connectors/github/app/start     — get the app's install URL
  GET  /api/connectors/github/app/complete  — where xo-swarm-api sends the browser
                                              back with the grant after install

On success the installation token lands in the same store as a PAT or a `gh`
session, so `/status`, `/disconnect` and `/reconnect` (in github_pat.py) serve
this method too, and the GitHub pollers pick it up unchanged.
"""

import html
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.github import app_auth

log = logging.getLogger(__name__)
router = APIRouter()

COMPLETE_PATH = "/api/connectors/github/app/complete"


class StartBody(BaseModel):
    #: Public URL of this workspace's `/complete` route. Defaults to the one
    #: derived from the request, which is wrong behind a rewriting proxy, so
    #: the UI should pass `window.location.origin + COMPLETE_PATH`.
    return_to: Optional[str] = None


@router.post("/api/connectors/github/app/start")
async def app_install_start(request: Request, body: StartBody = StartBody()) -> JSONResponse:
    """Return `{url}`; the UI opens it in a new tab and polls `/status`."""
    return_to = body.return_to or str(request.url_for("app_install_complete"))
    result = await app_auth.start(return_to)
    if not result["ok"]:
        return JSONResponse({"error": result["error"]}, status_code=result["status"])
    return JSONResponse({"url": result["url"]})


def _page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'><title>{t}</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        "<h2>{t}</h2><p>{m}</p><p>You can close this tab.</p>"
        "<script>try{{window.opener&&window.opener.postMessage({{source:'xo-github-app'}},'*')}}catch(e){{}}</script>"
    ).format(t=html.escape(title), m=html.escape(message))
    return HTMLResponse(body, status_code=status_code)


@router.get(COMPLETE_PATH, name="app_install_complete")
async def app_install_complete(
    status: str = "",
    grant: Optional[str] = None,
    installation_id: Optional[int] = None,
    account_login: str = "",
    message: str = "",
) -> HTMLResponse:
    """Browser landing page after install. The grant is bound to the XO user who
    started the install, so a grant from anyone else fails at the swarm."""
    if status == "requested":
        return _page("Approval requested",
                     "An organization owner must approve the XO GitHub App before it can be used.")
    if status != "installed" or not grant:
        return _page("GitHub not connected", message or "The GitHub App installation did not complete.", 400)

    result = await app_auth.connect(grant, account_login=account_login, installation_id=installation_id)
    if not result["ok"]:
        log.warning("GitHub App connect failed: %s", result.get("error"))
        return _page("GitHub not connected", result.get("error") or "Could not connect GitHub.", 502)
    return _page("GitHub connected", f"Connected to {account_login or 'GitHub'} through the XO GitHub App.")
