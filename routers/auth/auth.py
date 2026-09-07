"""``/xo-auth/*`` — the browser-auth flow, proxied to xo-swarm-api.

xo-swarm-api owns authentication: it runs the browser OAuth handshake
(``/auth/browser/start|status|consume``), validates tokens (``GET /get-user-id``) and mints
the opaque session id the UI carries (``POST /auth/session/self``). This router is the
thin client of those endpoints that a UI can drive through this backend, so the browser
never talks to the swarm directly and the raw XO token never reaches it.

    POST /xo-auth/start                    -> swarm  POST /auth/browser/start
    GET  /xo-auth/status/{auth_session_id} -> swarm  GET  /auth/browser/status/{id}
    POST /xo-auth/consume                  -> swarm  POST /auth/browser/consume, token kept
    GET  /xo-auth/whoami                   -> swarm  GET  /get-user-id (this backend's token)
    GET  /xo-auth/state                    -> local safe snapshot
    POST /xo-auth/logout                   -> local, forgets the consumed token

``GET /xo-auth/session/self`` is *not* here: it lives in
``routers/cowork_agent/connectors/composio_session.py`` because the connector UI depends
on it. Both routers share the ``/xo-auth`` prefix.

``POST /xo-auth/session`` (mint a session from a token the *caller* presents) is gone for
good. The Composio tenant key is composed from the credential this backend holds, so a
session minted for another account would silently act with this backend's principal —
a cross-account read. See ``tests/test_composio.py::RemovedEndpointTests``.

The credential itself — one per process — lives in ``services/xo_credential.py``; this
module keeps no state of its own. The names below are re-exported so older imports of
``routers.auth.auth`` keep working.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.xo_credential import (  # noqa: F401  (re-exported for older importers)
    CHAT_API_BASE_URL,
    HTTP_TIMEOUT,
    XO_API_KEY,
    XO_AUTH_CONSUME_PATH,
    XO_AUTH_START_PATH,
    XO_AUTH_STATUS_PATH,
    XO_GET_USER_ID_PATH,
    auth_lock,
    auth_state,
    clear_auth_token,
    consume_auth_flow,
    get_auth_state,
    get_auth_token,
    set_auth_token,
)

__all__ = [
    "router",
    "CHAT_API_BASE_URL",
    "XO_API_KEY",
    "auth_lock",
    "auth_state",
    "clear_auth_token",
    "consume_auth_flow",
    "get_auth_state",
    "get_auth_token",
    "set_auth_token",
    "resolve_consume_credentials",
]


class XOAuthStartRequest(BaseModel):
    """Start browser auth flow via xo-swarm-api."""

    scopes: Optional[str] = None
    client_reference: Optional[str] = None


class XOAuthConsumeRequest(BaseModel):
    """Consume completed browser auth flow."""

    auth_session_id: Optional[str] = None
    poll_token: Optional[str] = None


router = APIRouter(prefix="/xo-auth", tags=["auth"])


def _swarm_url(path: str) -> str:
    return f"{CHAT_API_BASE_URL.rstrip('/')}{path}"


def resolve_consume_credentials(
    auth_session_id: Optional[str], poll_token: Optional[str]
) -> tuple[str, str]:
    """Body first, then ``XO_AUTH_SESSION_ID`` / ``XO_POLL_TOKEN`` from the environment."""
    resolved_auth_session_id = (auth_session_id or "").strip() or os.getenv(
        "XO_AUTH_SESSION_ID", ""
    ).strip()
    resolved_poll_token = (poll_token or "").strip() or os.getenv(
        "XO_POLL_TOKEN", ""
    ).strip()
    if not resolved_auth_session_id or not resolved_poll_token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "Missing auth_session_id/poll_token. "
                    "Provide in request body or set XO_AUTH_SESSION_ID and XO_POLL_TOKEN."
                )
            },
        )
    return resolved_auth_session_id, resolved_poll_token


@router.post("/start")
async def xo_auth_start(data: XOAuthStartRequest) -> Dict[str, Any]:
    """Start the swarm's browser auth flow.

    Returns the swarm's payload unchanged: ``authorize_url``, ``auth_session_id``,
    ``poll_token``, ``status_url``, ``consume_url``, ``expires_at``.
    """
    url = _swarm_url(XO_AUTH_START_PATH)
    payload = {"scopes": data.scopes, "client_reference": data.client_reference}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to start auth flow", "upstream": response.text},
            )
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to start auth flow: {str(e)}"}
        )


@router.get("/status/{auth_session_id}")
async def xo_auth_status(auth_session_id: str, poll_token: str) -> Dict[str, Any]:
    """Poll the swarm for the state of a browser auth flow."""
    url = f"{_swarm_url(XO_AUTH_STATUS_PATH)}/{auth_session_id}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, params={"poll_token": poll_token})
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to check auth status", "upstream": response.text},
            )
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to check auth status: {str(e)}"}
        )


@router.post("/consume")
async def xo_auth_consume(data: XOAuthConsumeRequest) -> Dict[str, Any]:
    """Consume a completed flow and hold the token for this backend's outbound calls.

    Body values win; ``XO_AUTH_SESSION_ID`` / ``XO_POLL_TOKEN`` are the fallback. No
    session id is minted here — the UI asks ``GET /xo-auth/session/self`` for one.
    """
    auth_session_id, poll_token = resolve_consume_credentials(
        data.auth_session_id, data.poll_token
    )
    return await consume_auth_flow(auth_session_id, poll_token)


@router.get("/whoami")
async def xo_auth_whoami() -> Dict[str, Any]:
    """Validate this backend's credential against the swarm's ``/get-user-id``."""
    token = get_auth_token()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "No stored access token. Complete /xo-auth flow first."},
        )

    url = _swarm_url(XO_GET_USER_ID_PATH)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Token validation failed", "upstream": response.text},
            )
        data = response.json()
        with auth_lock:
            auth_state["user_id"] = data.get("user_id")
        return {"success": True, "user_id": data.get("user_id")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to validate token: {str(e)}"}
        )


@router.get("/state")
async def xo_auth_state() -> Dict[str, Any]:
    """Safe view of the credential state. Never exposes the token."""
    return get_auth_state()


@router.post("/logout")
async def xo_auth_logout() -> Dict[str, Any]:
    """Forget the consumed token. ``XO_API_KEY`` is environment and is unaffected."""
    clear_auth_token()
    return {"success": True, "message": "Auth token cleared"}
