"""Per-request identity for Composio.

**One backend, one account.** This process holds exactly one XO credential
(``routers.auth.auth.get_auth_token`` takes no arguments), so there is exactly one
Composio ``user_id`` for its whole lifetime. This module used to resolve a bearer to an
account id and compose a principal per request; that apparatus always produced the same
constant, and it is gone.

What remains is a **gate**, not a resolver:

1. does the request carry a live session id? — so the browser tab has been vouched for
   by a backend that holds a working XO credential;
2. hand back the pod's account id, fetched from xo-swarm-api and cached in :mod:`.state`.

The account id is the whole tenant key. Connected accounts are account-wide: connect
Gmail once and every workspace of that account can reach it. Which of them a *particular*
workspace may actually use is decided later, when the Composio session is built — see
:mod:`.workspace_scope`. That is a property of the session, not of the caller's identity,
so it is deliberately not this module's business.

Note what is **not** checked here any more. This gate used to 401 when
``CODER_WORKSPACE_ID`` was unset, to avoid "falling back to an account-wide Composio
bucket". The account-wide bucket is now the intended design, so that check had inverted
from a protection into an outage and was removed. The workspace id still matters — it
stamps this pod's session store — but a missing one is the store's problem to report,
not a reason to refuse an authenticated request.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from services.cowork_agent.connectors.composio import state

log = logging.getLogger(__name__)


_SESSION_HEADER = "x-xo-session"


def _extract_bearer(request: Request) -> Optional[str]:
    session_header = (request.headers.get(_SESSION_HEADER) or "").strip()
    if session_header:
        return session_header
    auth = request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def resolve_user_from_bearer(request: Request) -> Optional[str]:
    """The Composio account id for this request, or None.

    None when the request carries no live session id, or when the account id cannot be
    fetched. Callers on the soft paths (chat, ``/api/tools``) treat None as "run without
    Composio tools"; the connector routes turn it into a 401 via
    :func:`get_composio_user`.
    """
    from services.cowork_agent.connectors.composio.session_identity import is_valid

    session_id = _extract_bearer(request)
    if not session_id or not is_valid(session_id):
        return None
    try:
        return await state.aaccount_id()
    except state.StateUnavailable as exc:
        log.warning("composio_identity: account id unavailable: %s", exc)
        return None


async def get_composio_user(request: Request) -> str:
    """FastAPI dependency for the Composio routes. 401s rather than returning None."""
    if not _extract_bearer(request):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing session identity. Send 'X-XO-Session: <session_id>' "
                "(or 'Authorization: Bearer <session_id>'). Mint one with "
                "GET /xo-auth/session/self."
            ),
        )
    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session, or XO is unreachable.",
        )
    return user_id
