"""Per-request identity for Composio.

**One backend, one principal.** This process holds exactly one XO credential
(``services.xo_credential.get_auth_token`` takes no arguments) and runs in exactly one Coder
workspace, so there is exactly one Composio tenant key for its whole lifetime. This
module used to resolve a bearer to an account id and compose a principal per request;
that apparatus always produced the same constant, and it is gone.

What remains is a **gate**, not a resolver:

1. does the request carry a live session id? — so the browser tab has been vouched for
   by a backend that holds a working XO credential;
2. does this pod know its own workspace? — fail closed if not;
3. hand back the pod's principal, composed by xo-swarm-api and cached in
   :mod:`.state`.

The tenant key itself is composed in one place only, on the swarm
(``auth/tenancy.py``). It is stored inside Composio against every connected account, so a
second implementation drifting from the first would orphan all of them — which is why
there is no longer one here.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from services import tenancy
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
    """The Composio principal for this request, or None.

    None when the request carries no live session id, when this pod has no workspace
    identity, or when the principal cannot be fetched. Callers on the soft paths (chat,
    ``/api/tools``) treat None as "run without Composio tools"; the connector routes turn
    it into a 401 via :func:`get_composio_user`.
    """
    from services.cowork_agent.connectors.composio.session_identity import is_valid

    session_id = _extract_bearer(request)
    if not session_id or not is_valid(session_id):
        return None
    try:
        return await state.aprincipal()
    except tenancy.WorkspaceIdentityUnavailable as exc:
        log.error(
            "composio_identity: %s — refusing to fall back to an unscoped Composio "
            "bucket, which would share one tenant across every workspace of this XO "
            "account. %s is injected by the Coder pod.",
            exc, tenancy.WORKSPACE_ENV,
        )
        return None
    except state.StateUnavailable as exc:
        log.warning("composio_identity: principal unavailable: %s", exc)
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
    # Checked before resolution so a missing workspace identity reports itself, rather
    # than surfacing as the misleading "invalid or expired session" below. 401 (not 503)
    # keeps this dependency's status-code surface unchanged.
    try:
        tenancy.workspace_id()
    except tenancy.WorkspaceIdentityUnavailable as exc:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Workspace identity unavailable ({exc}). This backend scopes connector "
                f"state per workspace and will not fall back to an account-wide bucket. "
                f"{tenancy.WORKSPACE_ENV} is injected by the Coder pod."
            ),
        )
    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session, or XO is unreachable.",
        )
    return user_id
