"""
Auth router and auth-state helpers for XO Space API.
"""

import datetime
import os
import threading
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


# External Chat API base URL (xo-swarm-api or similar)
CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")

# Clerk user API key (long-lived). When set, used as Bearer token for all chat API calls;
# no consume flow. When not set, auth uses XO_AUTH_SESSION_ID + XO_POLL_TOKEN and consume.
# Requires xo-swarm-api to verify Clerk API keys (Bearer ak_xxx).
XO_API_KEY = os.getenv("XO_API_KEY", "").strip() or None

# XO backend browser-auth endpoints (new flow)
XO_AUTH_START_PATH = os.getenv("XO_AUTH_START_PATH", "/auth/browser/start")
XO_AUTH_STATUS_PATH = os.getenv("XO_AUTH_STATUS_PATH", "/auth/browser/status")
XO_AUTH_CONSUME_PATH = os.getenv("XO_AUTH_CONSUME_PATH", "/auth/browser/consume")
XO_GET_USER_ID_PATH = os.getenv("XO_GET_USER_ID_PATH", "/get-user-id")

# HTTP client timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_CONSUME_PATH}"
    payload = {"auth_session_id": auth_session_id, "poll_token": poll_token}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to consume auth flow", "upstream": response.text},
            )

        result = response.json()
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
        # Mint a backend-held session id so a browser can carry per-user identity
        # without ever holding the raw XO token. Best-effort: identity resolution
        # still works without it, this just enables the multi-tenant bearer path.
        session_id = None
        user_id = result.get("user_id")
        if user_id:
            from services.cowork_agent.composio import session_identity  # local import: avoid load cycle
            session_id = session_identity.register(
                user_id, ttl_seconds=result.get("expires_in"),
            )
        return {
            "success": True,
            "message": "Authentication completed and token stored",
            "user_id": user_id,
            "expires_in": result.get("expires_in"),
            "scope": result.get("scope"),
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to consume auth flow: {str(e)}"}
        )


@router.post("/start")
async def xo_auth_start(data: XOAuthStartRequest):
    """
    Start XO backend browser auth flow.
    Returns authorize_url + auth_session_id + poll_token.
    """
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_START_PATH}"
    payload = {
        "scopes": data.scopes,
        "client_reference": data.client_reference,
    }
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
async def xo_auth_status(auth_session_id: str, poll_token: str):
    """Poll XO backend auth flow status."""
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_STATUS_PATH}/{auth_session_id}"
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


@router.post("/session")
async def xo_auth_session(request: Request):
    """Mint a backend-held opaque session id from an XO access token.

    The XO platform (which already authenticated the user) hands the user's XO
    access token here as ``Authorization: Bearer <xo_token>``. We validate it
    against XO ``/get-user-id``, store ``{user_id, token}`` server-side, and
    return ``{session_id}``. The browser then sends that opaque id as the bearer
    on ``/api/connectors/composio/*`` and ``/api/chat/prompt`` — the raw XO token
    never reaches the client. See services/cowork_agent/composio/session_identity.py.
    """
    from services.cowork_agent.composio import session_identity  # local import: avoid load cycle

    auth = request.headers.get("authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=401,
            detail={"error": "Missing 'Authorization: Bearer <xo_token>' header."},
        )
    session_id = await session_identity.mint(parts[1].strip())
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail={"error": "XO token rejected by /get-user-id."},
        )
    return {"success": True, "session_id": session_id}


@router.get("/session/self")
async def xo_auth_session_self():
    """Mint a session id for the XO user this backend is itself authenticated as.

    Bootstrap for the local/desktop UI, which has no XO login of its own: the
    backend already holds an XO credential (XO_API_KEY, or the token from the
    consume flow), so it resolves *that* credential's real ``user_id`` and hands
    back an opaque session id the browser can use as its bearer.

    This is a real XO user, not a shared sentinel, and it travels the same
    per-request bearer path as any platform-supplied identity — when real
    per-user tokens start arriving via POST /xo-auth/session, nothing else
    changes and this endpoint simply stops being used.

    401 when the backend itself is unauthenticated or XO rejects its credential.

    The returned ``user_id`` is the bare XO **account** id — it answers "which XO
    user is this backend", not "which tenant". The workspace-scoped principal
    used for Composio is composed per request in
    ``services.cowork_agent.composio.identity.resolve_user_from_bearer``.
    """
    from services.cowork_agent.composio import session_identity  # local import: avoid load cycle

    token = get_auth_token()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": (
                    "Backend is not authenticated to XO (no XO_API_KEY and no "
                    "consumed session). Cannot mint a session."
                )
            },
        )

    session_id = await session_identity.mint(token)
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail={"error": "XO rejected this backend's credential at /get-user-id."},
        )
    return {
        "success": True,
        "session_id": session_id,
        "user_id": session_identity.resolve(session_id),
    }


@router.get("/whoami")
async def xo_auth_whoami():
    """
    Validate stored token against XO backend /get-user-id endpoint.
    """
    token = get_auth_token()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "No stored access token. Complete /xo-auth flow first."},
        )

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_GET_USER_ID_PATH}"
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
async def xo_auth_state():
    """Get current auth state (safe view)."""
    return get_auth_state()


@router.post("/logout")
async def xo_auth_logout():
    """Clear stored auth token state."""
    clear_auth_token()
    return {"success": True, "message": "Auth token cleared"}
