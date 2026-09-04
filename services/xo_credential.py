"""The one XO credential this process holds for its own outbound calls.

**This is not an auth subsystem, and there is no longer one here.** xo-swarm-api owns
authentication: it verifies Clerk credentials, composes tenant keys (`auth/tenancy.py`),
runs the browser OAuth handshake (`/auth/browser/start|status|consume`) and, since the
session utility moved, mints the opaque bearer the UI carries
(`POST /auth/session/self`). What is left here is the thing that cannot live over there —
the credential *this* client presents when it calls the swarm.

Two ways to hold it, and only two:

- ``XO_API_KEY`` — a long-lived Clerk API key, read from the environment. This is the
  normal path, and when it is set nothing else in this module does any work.
- the consume flow — for installs provisioned with ``XO_AUTH_SESSION_ID`` /
  ``XO_POLL_TOKEN`` instead of a key. :func:`consume_auth_flow` trades those for an
  access token at the swarm's ``/auth/browser/consume`` and keeps it in memory for the
  life of the process. It is a client of the flow, not an implementation of it.

**One backend, one XO identity.** :func:`get_auth_token` takes no arguments and consults
no request — the fact the whole Composio tenancy model rests on. A backend serving several
XO accounts would need a way to forward each caller's own credential to the swarm, which
is a design change, not a config change.

The raw token never leaves this process: the browser gets a session id minted by the
swarm (see ``routers/cowork_agent/connectors/composio_session.py``), never this.
"""

from __future__ import annotations

import datetime
import os
import threading
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException


# xo-swarm-api base URL.
CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")

# Clerk user API key (long-lived). When set it is the Bearer for every outbound call and
# the consume flow is skipped entirely.
XO_API_KEY = os.getenv("XO_API_KEY", "").strip() or None

XO_AUTH_CONSUME_PATH = os.getenv("XO_AUTH_CONSUME_PATH", "/auth/browser/consume")

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
    """Store the active access token for outbound requests to xo-swarm-api."""
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


def get_auth_token() -> Optional[str]:
    """The Bearer for outbound calls to xo-swarm-api, or None when signed out.

    ``XO_API_KEY`` wins when set; otherwise the in-memory token from the consume flow.
    """
    if XO_API_KEY:
        return XO_API_KEY
    with auth_lock:
        return auth_state.get("access_token")


def get_auth_state() -> Dict[str, Any]:
    """A safe snapshot for ``/health`` and the boot banner. Never exposes the token."""
    with auth_lock:
        token = auth_state.get("access_token")
        source = "api_key" if XO_API_KEY else ("session" if token else "none")
        return {
            "authenticated": bool(XO_API_KEY or token),
            "user_id": auth_state.get("user_id"),
            "expires_at": auth_state.get("expires_at"),
            "auth_session_id": auth_state.get("auth_session_id"),
            "token_source": source,
        }


async def consume_auth_flow(auth_session_id: str, poll_token: str) -> Dict[str, Any]:
    """Trade a completed browser flow for an access token and hold it in memory.

    Called at boot from ``XO_AUTH_SESSION_ID`` / ``XO_POLL_TOKEN`` (``server.py``). The
    flow itself — start, callback, status, consume — belongs to xo-swarm-api; this is the
    one step with a local consequence, which is why it is the only one that survived here.

    No session id is minted here. The UI asks for one when it loads, and the swarm mints
    it; a token consumed at boot vouches for nothing until a tab actually arrives.
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
        return {
            "success": True,
            "message": "Authentication completed and token stored",
            "user_id": result.get("user_id"),
            "expires_in": result.get("expires_in"),
            "scope": result.get("scope"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to consume auth flow: {str(e)}"}
        )
