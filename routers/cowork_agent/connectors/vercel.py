"""HTTP surface for the Vercel connector.

  POST /api/connectors/vercel/token            — store a pasted API token
  GET  /api/connectors/vercel/status           — current connection state
  POST /api/connectors/vercel/disconnect       — forget the stored credentials
  POST /api/connectors/vercel/reconnect        — re-check what's stored
  GET  /api/connectors/vercel/oauth/start      — begin Sign in with Vercel
  POST /api/connectors/vercel/oauth/exchange   — finish it from a pasted URL
  GET  /callback                               — finish it from Vercel's redirect
  GET/OPTIONS /.well-known/oauth-protected-resource — RFC 9728 metadata

``vercel_oauth_callback`` is imported by the MagicPath router, which owns the
shared ``/callback`` path and delegates Vercel-shaped requests here; its name
and parameters are that contract.
"""

import html
import json
import logging
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.connectors.vercel import (
    Connection,
    complete_authorization,
    connect_with_api_token,
    disconnect,
    get_status,
    start_authorization,
)
from services.cowork_agent.connectors.vercel.oauth import VercelOAuthError

log = logging.getLogger(__name__)
router = APIRouter()


class TokenBody(BaseModel):
    token: str = Field(max_length=4096)


class ExchangeBody(BaseModel):
    """The finish step. `callback_url` is the whole URL from the address bar;
    `code` accepts one too, since that is the field people paste into."""

    code: str | None = Field(default=None, max_length=8192)
    state: str | None = Field(default=None, max_length=4096)
    callback_url: str | None = Field(default=None, max_length=8192)


def _respond(connection: Connection) -> JSONResponse:
    status_code = {"connected": 200, "needs_auth": 200, "failed": 502}.get(
        connection.status, 200
    )
    return JSONResponse(connection.as_dict(), status_code=status_code)


@router.post("/api/connectors/vercel/token")
async def submit_token(body: TokenBody) -> JSONResponse:
    connection = await connect_with_api_token(body.token)
    if connection.status != "connected":
        raise HTTPException(422, detail=connection.error or "Vercel rejected the token.")
    return _respond(connection)


@router.get("/api/connectors/vercel/status")
async def status() -> JSONResponse:
    return _respond(await get_status())


@router.post("/api/connectors/vercel/reconnect")
async def reconnect() -> JSONResponse:
    """Re-check the stored credentials, refreshing an OAuth token if due."""
    return _respond(await get_status())


@router.post("/api/connectors/vercel/disconnect")
async def disconnect_vercel() -> JSONResponse:
    return _respond(await disconnect())


@router.get("/api/connectors/vercel/oauth/start")
async def oauth_start(
    redirect_uri: str = Query(default=None, description="Override the callback URL"),
) -> JSONResponse:
    """Start an authorization.

    The response says whether the callback will complete on its own
    (`reachable_callback`) and, when it won't, how to finish by pasting the URL
    (`instructions`, `exchange_url`).
    """
    try:
        authorization = await start_authorization(redirect_uri)
    except VercelOAuthError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return JSONResponse(authorization.as_dict())


def _split_pasted(body: ExchangeBody) -> tuple[str, str]:
    """Pull (code, state) out of separate fields or a pasted callback URL."""
    code = (body.code or "").strip()
    state = (body.state or "").strip()

    pasted = (body.callback_url or "").strip()
    if not pasted and code.lower().startswith(("http://", "https://")):
        pasted, code = code, ""

    if pasted:
        query = parse_qs(urlparse(pasted).query)
        error = (query.get("error") or [""])[0].strip()
        if error:
            description = (query.get("error_description") or [""])[0].strip()
            raise HTTPException(422, detail=description or error)
        code = (query.get("code") or [code])[0].strip()
        state = (query.get("state") or [state])[0].strip()

    if not code or not state:
        raise HTTPException(
            400,
            detail=(
                "Send the full callback URL from the browser's address bar as "
                "callback_url, or code and state separately."
            ),
        )
    return code, state


@router.post("/api/connectors/vercel/oauth/exchange")
async def oauth_exchange(body: ExchangeBody) -> JSONResponse:
    """Finish an authorization without a working redirect."""
    code, state = _split_pasted(body)
    connection = await complete_authorization(code=code, state=state)
    if connection.status != "connected":
        raise HTTPException(422, detail=connection.error or "Could not complete sign-in.")
    return _respond(connection)


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; display: grid;
         place-items: center; min-height: 100vh; background: #0b0b0c; color: #ededef; }}
  main {{ max-width: 32rem; padding: 2rem; text-align: center; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
  p {{ margin: 0; color: #a1a1a6; }}
</style>
</head>
<body>
<main><h1>{title}</h1><p>{message}</p></main>
<script>
  if (window.opener) {{
    window.opener.postMessage({payload}, "*");
    window.close();
  }}
</script>
</body>
</html>
"""


def _page(title: str, message: str, payload: dict, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(
            title=html.escape(title),
            message=html.escape(message),
            payload=json.dumps(payload),
        ),
        status_code=status_code,
    )


@router.get("/callback")
async def vercel_oauth_callback(
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str = Query(default=None),
    error_description: str = Query(default=None),
) -> HTMLResponse:
    """Where Vercel sends the browser after the user approves or denies.

    Reports the outcome to whoever opened the window via postMessage
    (`vercel_oauth_success` / `vercel_oauth_error`) and closes itself.
    """
    if error:
        detail = error_description or error
        return _page(
            "Vercel authorization failed",
            detail,
            {"type": "vercel_oauth_error", "error": detail},
            400,
        )

    if not code or not state:
        detail = "The callback was missing its code or state. Start the connection again."
        return _page(
            "Vercel authorization incomplete",
            detail,
            {"type": "vercel_oauth_error", "error": detail},
            400,
        )

    connection = await complete_authorization(code=code, state=state)
    if connection.status != "connected":
        detail = connection.error or "Vercel would not issue a token."
        return _page(
            "Vercel authorization failed",
            detail,
            {"type": "vercel_oauth_error", "error": detail},
            502,
        )

    who = (connection.identity.name or connection.identity.username) if connection.identity else ""
    return _page(
        "Vercel connected",
        f"Signed in as {who}. You can close this window." if who
        else "You can close this window.",
        {
            "type": "vercel_oauth_success",
            "username": connection.identity.username if connection.identity else "",
            "name": who,
        },
        200,
    )


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    """Tell MCP clients which authorization server issues tokens for us."""
    return JSONResponse(
        {
            "resource": str(request.base_url).rstrip("/"),
            "authorization_servers": ["https://vercel.com"],
            "scopes_supported": ["openid", "email", "profile", "offline_access"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://vercel.com/docs/rest-api",
        },
        headers={**_CORS, "Cache-Control": "no-store"},
    )


@router.options("/.well-known/oauth-protected-resource")
async def protected_resource_preflight() -> JSONResponse:
    return JSONResponse({}, headers=_CORS)
