"""``GET /xo-auth/session/self`` — a pass-through to the swarm's minting endpoint.

The UI has no XO login of its own, and the connector routes refuse to act without knowing
the request came from a vouched-for tab. This route is how a tab gets its bearer.

**It no longer mints anything.** Minting moved to xo-swarm-api
(``POST /auth/session/self``, ``auth/session_identity.py``), which is where authentication
lives: the swarm verifies this backend's XO credential, validates the workspace id this
pod supplies, and composes the tenant key. A backend whose credential has been revoked
therefore fails *here*, at sign-in, rather than rendering "signed in" and 401ing every
route afterwards — exactly the property the old in-process route had, now enforced by the
service that owns the credential rather than by a copy of the rule shipped to every pod.

The path is unchanged on purpose: ``space_ui/js/core/session.js`` calls it, and the tab
must keep going through this backend — it holds the XO credential, and the browser does
not.

The id the swarm hands back is recorded locally by
:mod:`services.cowork_agent.connectors.composio.session_identity`, so validating it on the
next request stays a dict lookup. See that module for what the local record does and does
not know.

The tenant key is deliberately not returned; no consumer needs it and it does not belong
in a browser. ``user_id`` is the bare XO **account** id, for display.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from services import tenancy
from services.cowork_agent.connectors.composio import session_identity
from services.xo_credential import CHAT_API_BASE_URL, HTTP_TIMEOUT, get_auth_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/xo-auth", tags=["auth"])

SESSION_MINT_PATH = "/auth/session/self"


@router.get("/session/self")
async def xo_auth_session_self():
    """Mint the browser's opaque bearer, via xo-swarm-api."""
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

    try:
        workspace_id = tenancy.workspace_id()
    except tenancy.WorkspaceIdentityUnavailable as exc:
        # Fail closed, and say which half is missing. Falling back to an account-wide
        # bucket would share one Composio tenant across every workspace of this account.
        raise HTTPException(
            status_code=401,
            detail={
                "error": (
                    f"Workspace identity unavailable ({exc}). {tenancy.WORKSPACE_ENV} "
                    "is injected by the Coder pod."
                )
            },
        )

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{SESSION_MINT_PATH}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"workspace_id": workspace_id},
            )
    except Exception as exc:
        # Unreachable is not a sign-in problem, and telling the user to sign in would
        # send them at the wrong thing.
        raise HTTPException(
            status_code=503,
            detail={"error": f"xo-swarm-api could not be reached at {url}: {exc}"},
        )

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=401,
            detail={
                "error": (
                    f"xo-swarm-api rejected this backend's XO credential (HTTP "
                    f"{response.status_code}). Sign in to XO, or set XO_API_KEY."
                )
            },
        )
    if response.status_code == 404:
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    f"xo-swarm-api has no {SESSION_MINT_PATH}. Deploy the swarm before "
                    "this workspace."
                )
            },
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    f"xo-swarm-api returned HTTP {response.status_code} while minting a "
                    f"session."
                ),
                "upstream": response.text[:200],
            },
        )

    try:
        result = response.json()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={"error": "xo-swarm-api returned an unreadable session payload."},
        )

    session_id = (result.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=503,
            detail={"error": "xo-swarm-api returned no session id."},
        )

    session_identity.remember(session_id, ttl_seconds=result.get("expires_in"))

    # Best effort, and never fatal: the mint above already proved the credential and the
    # workspace, so this only warms the cache every later request reads.
    try:
        from services.cowork_agent.connectors.composio import state

        await state.aprincipal_payload()
    except Exception as exc:
        log.warning("xo_auth_session: principal cache not warmed: %s", exc)

    return {
        "success": True,
        "session_id": session_id,
        "user_id": result.get("account_id"),
    }
