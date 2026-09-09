"""
Auth router and auth-state helpers for XO Space API.
"""

import datetime
import os
import threading
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.swarm_api import auth as swarm_auth


# Clerk user API key (long-lived). When set, used as Bearer token for all chat API calls;
# no consume flow. When not set, auth uses XO_AUTH_SESSION_ID + XO_POLL_TOKEN and consume.
# Requires xo-swarm-api to verify Clerk API keys (Bearer ak_xxx).
XO_API_KEY = os.getenv("XO_API_KEY", "").strip() or None

# The browser-auth handshake and token validation calls live in
# services/swarm_api/auth.py (the one swarm door); this module owns the
# token STATE and the /xo-auth routes.


auth_lock = threading.Lock()
auth_state: Dict[str, Any] = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": None,
    "user_id": None,
    "auth_session_id": None,
}


def set_auth_token(
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_in: Optional[int] = None,
    user_id: Optional[str] = None,
    auth_session_id: Optional[str] = None,
) -> None:
    """Store active auth token for outbound requests to xo-swarm-api."""
    expires_at = None
    if expires_in:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=expires_in)
        ).isoformat()
    with auth_lock:
        auth_state["access_token"] = access_token
        auth_state["refresh_token"] = refresh_token
        auth_state["expires_at"] = expires_at
        auth_state["user_id"] = user_id
        auth_state["auth_session_id"] = auth_session_id


def clear_auth_token() -> None:
    """Clear active auth token state."""
    with auth_lock:
        auth_state["access_token"] = None
        auth_state["refresh_token"] = None
        auth_state["expires_at"] = None
        auth_state["user_id"] = None
        auth_state["auth_session_id"] = None


def get_auth_token() -> Optional[str]:
    """
    Get active access token for outbound calls to xo-swarm-api.
    When XO_API_KEY is set it is used (no consume). Otherwise in-memory token from consume.
    """
    if XO_API_KEY:
        return XO_API_KEY
    with auth_lock:
        return auth_state.get("access_token")


def get_auth_state() -> Dict[str, Any]:
    """Return a safe auth state snapshot (without exposing token value)."""
    with auth_lock:
        token = auth_state.get("access_token")
    # When XO_API_KEY is set it is used for all requests; otherwise session token from consume.
    source = "api_key" if XO_API_KEY else ("session" if token else "none")
    effective_token = XO_API_KEY or token
    with auth_lock:
        return {
            "authenticated": bool(effective_token),
            "user_id": auth_state.get("user_id"),
            "expires_at": auth_state.get("expires_at"),
            "auth_session_id": auth_state.get("auth_session_id"),
            "token_source": source,
        }


class XOAuthStartRequest(BaseModel):
    """Start browser auth flow via xo-swarm-api."""

    scopes: Optional[str] = None
    client_reference: Optional[str] = None


class XOAuthConsumeRequest(BaseModel):
    """Consume completed browser auth flow."""

    auth_session_id: Optional[str] = None
    poll_token: Optional[str] = None


router = APIRouter(prefix="/xo-auth", tags=["auth"])


def resolve_consume_credentials(
    auth_session_id: Optional[str], poll_token: Optional[str]
) -> tuple[str, str]:
    """
    Resolve consume credentials with body-first, env-fallback strategy.
    """
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


async def consume_auth_flow(auth_session_id: str, poll_token: str) -> Dict[str, Any]:
    """
    Call XO consume endpoint and store returned access token in-memory.
    """
    res = await swarm_auth.browser_auth_consume(auth_session_id, poll_token)
    if res.offline or res.unauthenticated:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to consume auth flow: {res.detail}"}
        )
    if not res.ok:
        raise HTTPException(
            status_code=res.status,
            detail={"error": "Failed to consume auth flow", "upstream": res.text},
        )
    result = res.data if isinstance(res.data, dict) else {}
    access_token = result.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=500, detail={"error": "No access token in consume response"}
        )
    set_auth_token(
        access_token=access_token,
        refresh_token=result.get("refresh_token"),
        expires_in=result.get("expires_in"),
        user_id=result.get("user_id"),
        auth_session_id=result.get("auth_session_id"),
    )
    return {
        "success": True,
        "message": "Authentication completed and token stored",
        "user_id": result.get("user_id"),
        "expires_in": result.get("expires_in"),
        "scope": result.get("scope"),
    }


@router.post("/start")
async def xo_auth_start(data: XOAuthStartRequest):
    """
    Start XO backend browser auth flow.
    Returns authorize_url + auth_session_id + poll_token.
    """
    res = await swarm_auth.browser_auth_start(data.scopes, data.client_reference)
    if res.offline or res.unauthenticated:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to start auth flow: {res.detail}"}
        )
    if not res.ok:
        raise HTTPException(
            status_code=res.status,
            detail={"error": "Failed to start auth flow", "upstream": res.text},
        )
    return res.data


@router.get("/status/{auth_session_id}")
async def xo_auth_status(auth_session_id: str, poll_token: str):
    """Poll XO backend auth flow status."""
    res = await swarm_auth.browser_auth_status(auth_session_id, poll_token)
    if res.offline or res.unauthenticated:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to check auth status: {res.detail}"}
        )
    if not res.ok:
        raise HTTPException(
            status_code=res.status,
            detail={"error": "Failed to check auth status", "upstream": res.text},
        )
    return res.data


@router.post("/consume")
async def xo_auth_consume(data: XOAuthConsumeRequest):
    """
    Consume auth flow and store token in-memory for outgoing XO backend calls.

    Request body values take precedence. If missing, fallback to env:
    - XO_AUTH_SESSION_ID
    - XO_POLL_TOKEN
    """
    auth_session_id, poll_token = resolve_consume_credentials(
        data.auth_session_id, data.poll_token
    )
    return await consume_auth_flow(auth_session_id, poll_token)


@router.get("/whoami")
async def xo_auth_whoami():
    """
    Validate stored token against XO backend /get-user-id endpoint.
    """
    if not get_auth_token():
        raise HTTPException(
            status_code=401,
            detail={"error": "No stored access token. Complete /xo-auth flow first."},
        )
    res = await swarm_auth.get_user_id()
    if res.offline or res.unauthenticated:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to validate token: {res.detail}"}
        )
    if not res.ok:
        raise HTTPException(
            status_code=res.status,
            detail={"error": "Token validation failed", "upstream": res.text},
        )
    data = res.data if isinstance(res.data, dict) else {}
    with auth_lock:
        auth_state["user_id"] = data.get("user_id")
    return {"success": True, "user_id": data.get("user_id")}


@router.get("/state")
async def xo_auth_state():
    """Get current auth state (safe view)."""
    return get_auth_state()


@router.post("/logout")
async def xo_auth_logout():
    """Clear stored auth token state."""
    clear_auth_token()
    return {"success": True, "message": "Auth token cleared"}
