"""
Per-request identity for Composio.

Two distinct things travel through this module, and conflating them is the bug
this design exists to prevent:

- an **account id** — what XO's ``/get-user-id`` returns. Every workspace an XO
  account owns resolves to the same value.
- a **principal** — the account id composed with the workspace id by
  ``services.tenancy``. This is the Composio tenant key.

The invariant: *stored and in-process state stays keyed by the bare account id;
anything crossing the pod boundary is scoped.* So ``_TOKEN_CACHE`` below and
``session_identity`` both hold account ids, and composition happens once, at
read, in :func:`resolve_user_from_bearer`. Do not "fix" those to store
principals — a single composition point is what makes double-scoping impossible.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request

from services import tenancy

log = logging.getLogger(__name__)


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = float(os.getenv("COMPOSIO_IDENTITY_CACHE_TTL", "90"))


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


async def _validate_token(token: str) -> Optional[str]:
    """Resolve an XO access token to its **account** id (not a principal).

    The cache is keyed by the raw token and holds account ids; workspace scoping
    is applied after the cache read, in :func:`resolve_user_from_bearer`.
    """
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached[1] > now:
        return cached[0]

    from routers.auth.auth import CHAT_API_BASE_URL, HTTP_TIMEOUT, XO_GET_USER_ID_PATH

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_GET_USER_ID_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        log.warning("composio_identity: token validation request failed: %s", exc)
        return None

    if resp.status_code != 200:
        log.info("composio_identity: token rejected by XO (status=%s)", resp.status_code)
        return None
    try:
        user_id = resp.json().get("user_id")
    except Exception:
        return None
    if not user_id:
        return None

    user_id = str(user_id)
    # Drop expired rows before inserting. The dict is keyed by the raw XO token,
    # so without this it grows unbounded and retains credentials for the life of
    # the process. Mirrors session_identity._prune.
    for stale in [t for t, (_, exp) in _TOKEN_CACHE.items() if exp <= now]:
        _TOKEN_CACHE.pop(stale, None)
    _TOKEN_CACHE[token] = (user_id, now + _TOKEN_TTL_SECONDS)
    return user_id


async def resolve_user_from_bearer(request: Request) -> Optional[str]:
    """Resolve the request's bearer to a workspace-scoped Composio principal.

    This is the single composition point: both resolution branches (an opaque
    session id, or a raw XO token) yield an account id, and the workspace half is
    attached here — so every downstream consumer receives an already-scoped
    principal and none of them re-derive it.

    Returns None when there is no valid identity *or* no workspace identity. The
    latter is deliberate: without a workspace there is no tenant, and falling
    back to the bare account id would put every workspace of the account into one
    shared Composio bucket.
    """
    token = _extract_bearer(request)
    if not token:
        return None
    from services.cowork_agent.connectors.composio.session_identity import resolve as resolve_session
    account_id = resolve_session(token) or await _validate_token(token)
    if not account_id:
        return None
    try:
        return tenancy.scoped_principal(account_id)
    except tenancy.WorkspaceIdentityUnavailable as exc:
        log.error(
            "composio_identity: %s — refusing to fall back to the account-wide "
            "bucket, which would share one Composio tenant across every "
            "workspace of this XO account. %s is injected by the Coder pod.",
            exc, tenancy.WORKSPACE_ENV,
        )
        return None


async def get_composio_user(request: Request) -> str:
    if not _extract_bearer(request):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing session identity. Send 'X-XO-Session: <session_id>' "
                "(or 'Authorization: Bearer <session_id>'). Mint one with "
                "POST /xo-auth/session, or GET /xo-auth/session/self when the "
                "backend holds the XO credential."
            ),
        )
    # Checked before resolution so a missing workspace identity reports itself,
    # rather than surfacing as the misleading "invalid or expired bearer token"
    # below. 401 (not 503) keeps this dependency's status-code surface unchanged.
    try:
        tenancy.workspace_id()
    except tenancy.WorkspaceIdentityUnavailable as exc:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Workspace identity unavailable ({exc}). This backend scopes "
                "connector state per workspace and will not fall back to the "
                f"account-wide bucket. {tenancy.WORKSPACE_ENV} is injected by "
                "the Coder pod."
            ),
        )
    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token.",
        )
    return user_id
