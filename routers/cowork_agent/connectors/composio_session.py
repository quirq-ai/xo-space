"""``GET /xo-auth/session/self``: a pass-through to the swarm's minting endpoint.

The UI has no XO login of its own, and the connector routes refuse to act without knowing
the request came from a vouched-for tab. This route is how a tab gets its bearer.

**Nothing is minted here.** Minting lives in xo-swarm-api (``POST /auth/session/self``),
which verifies this backend's XO credential and validates the space id this pod
supplies (sent as ``space_id``, and as the retired ``workspace_id`` too until
xo-swarm-api #41 is deployed). A backend whose credential has been revoked therefore
fails *here*, at sign-in, rather than rendering "signed in" and 401ing every route
afterwards. A 422 is reported as a 503, never as a sign-in problem, with the diagnosis
split by :func:`state.identity_field_gap`: a swarm that does not know the ``space_id``
field is a deploy gap (deploy xo-swarm-api #41), while a rejected value is this
install's ``XO_SPACE_ID`` being wrong, which no deploy fixes.

The path must not change: ``space_ui/js/core/session.js`` calls it, and the tab has to
keep going through this backend: it holds the XO credential, the browser does not.

The id the swarm hands back is recorded in
:mod:`services.cowork_agent.connectors.composio.session_identity`, so validating it on the
next request stays a dict lookup. ``user_id`` is the bare XO account id, for display.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from routers.auth.auth import get_auth_token
from services import swarm_api
from services.cowork_agent.connectors.composio import session_identity, state

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
        space_id = state.space_id()
    except state.SpaceIdentityUnavailable as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error": (
                    f"Space identity unavailable ({exc}). Set {state.SPACE_ENV} in "
                    f".env to this install's id at XO, the same value project "
                    f"sharing uses."
                )
            },
        )

    url = f"{swarm_api.base_url()}{SESSION_MINT_PATH}"
    try:
        async with httpx.AsyncClient(timeout=swarm_api.DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                # Dual-sent: the deployed swarm reads workspace_id, xo-swarm-api #41
                # reads space_id, and each ignores the field it does not know. Drop
                # workspace_id once xo-swarm-api #41 is deployed.
                json={"workspace_id": space_id, "space_id": space_id},
            )
    except Exception as exc:
        # 503, not 401: unreachable is not a sign-in problem, and "sign in" would send
        # the user at the wrong thing.
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
    if response.status_code == 422:
        # The upstream text carries the swarm's complaint for the operator; the request
        # headers never go in.
        if state.identity_field_gap(response):
            # The swarm does not know the identity field this install sends: it
            # predates xo-swarm-api #41 (the space_id rename). A deploy gap, not a
            # sign-in problem.
            error = (
                f"xo-swarm-api does not know the space_id field at {SESSION_MINT_PATH} "
                f"(HTTP 422). Deploy xo-swarm-api #41, which adds it, before this space."
            )
        else:
            # The swarm read this install's id and rejected its value: XO_SPACE_ID is
            # wrong here, and no deploy fixes that. Not a sign-in problem either.
            error = (
                f"xo-swarm-api did not accept this space's id at {SESSION_MINT_PATH} "
                f"(HTTP 422). Check {state.SPACE_ENV} in .env: it must be this "
                f"install's id at XO, the same value project sharing uses."
            )
        raise HTTPException(
            status_code=503,
            detail={"error": error, "upstream": response.text[:200]},
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

    # Never fatal: the mint already proved the credential, so this only warms a cache.
    try:
        await state.aidentity_payload()
    except Exception as exc:
        log.warning("xo_auth_session: identity cache not warmed: %s", exc)

    return {
        "success": True,
        "session_id": session_id,
        "user_id": result.get("account_id"),
    }
