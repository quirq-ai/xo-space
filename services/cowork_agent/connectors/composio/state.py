"""This workspace's Composio identity, resolved from xo-swarm-api.

Composio is addressed by the **bare Clerk account id**. This module is the client for
``GET /auth/workspace-principal``, a pure identity lookup that reads no database on either
side; it answers ``{account_id, workspace_id, legacy_principal}``.

It used to be addressed by a composed ``<account_id>__ws__<workspace_id>`` key, which
isolated workspaces at the cost of binding every connected account to exactly one of them:
connect Gmail in one workspace and every other workspace saw nothing. **Connections are
account-wide now**, and workspaces are separated inside the Composio tool-router session
instead — a per-workspace ``toolkits`` allowlist plus explicitly pinned
``connected_accounts``. See :mod:`.workspace_scope`.

``legacy_principal`` is that retired key, recomposed by the swarm for one purpose only:
listing the connected accounts still stranded under it, so the UI can tell the user to
reconnect them. Never pin one into a session — Composio requires a pinned account to
belong to the session's ``user_id``, so they are unreachable by construction.

**The workspace half never leaves this pod.** It comes from ``CODER_WORKSPACE_ID``
(:func:`workspace_id`), and its only consumer is the ownership stamp on ``sessions.json``
— proof that a store found on disk was written by *this* workspace and not restored from
another. Session ids, MCP proxy tokens and the per-workspace connector scope all live in
that 0600 store under :mod:`.paths`; there is no remote mirror, and a store that is lost
is re-minted locally on the next sweep.

The account id is a constant for the life of the pod, so it is cached. A swarm that cannot
be reached falls back first to the cached value and then to the account recorded in this
pod's own store (:func:`adopt_account_id`) — which is what lets a pod that has booted once
keep serving the agent hot path through an outage. An *authoritative* refusal never falls
back: "the owner said no" is a real answer.

Never log a token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


# The env key keeps its old name, and so does the route: xo-space treats a 404 here as
# "this swarm predates the route", so renaming the path would read as an outage on every
# workspace that has not been redeployed yet.
IDENTITY_PATH = os.getenv("XO_PRINCIPAL_PATH", "/auth/workspace-principal")

_TTL = float(os.getenv("COMPOSIO_STATE_TTL", "900"))
_ERROR_TTL = float(os.getenv("COMPOSIO_STATE_ERROR_TTL", "30"))
_STALE_MAX = float(os.getenv("COMPOSIO_STATE_STALE_MAX", "3600"))

# Tighter than routers.auth.auth.HTTP_TIMEOUT (30s): some of these calls are sync and run
# on the event loop, so a hung swarm must fail fast rather than stall every request.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class StateUnavailable(RuntimeError):
    """The swarm could not answer.

    ``authoritative`` separates "the owner said no" (401/403 — never masked, never served
    from a stale cache) from "the owner could not be reached" (retryable, and the caller
    may serve a stale answer). The boot installer turns the first into "fix it and
    restart" and the second into "the next sweep retries".
    """

    def __init__(
        self, message: str, *, authoritative: bool = False, not_found: bool = False,
    ) -> None:
        super().__init__(message)
        self.authoritative = authoritative
        # A 404 on the identity path means "this swarm predates the route" — a deploy
        # ordering slip, not a refusal. The caller decides what to do about it.
        self.not_found = not_found


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_ALOCK: Optional[asyncio.Lock] = None
# (account_id, expires_at, fetched_at, payload) — one value for the life of the pod.
_IDENTITY: Optional[tuple[str, float, float, dict]] = None
# What this pod's own store says its rows belong to. No expiry: it is a fact about local
# data, and it is what lets a pod that has booted once ride out a swarm outage.
_ACCOUNT_FROM_STORE: Optional[str] = None


def invalidate() -> None:
    """Drop every cache. Test hook, and the seam a refresh route would use."""
    global _ALOCK, _IDENTITY, _ACCOUNT_FROM_STORE
    with _LOCK:
        _IDENTITY = None
        _ACCOUNT_FROM_STORE = None
    _ALOCK = None


def _alock() -> asyncio.Lock:
    # Created lazily so importing this module does not require a running loop.
    global _ALOCK
    if _ALOCK is None:
        _ALOCK = asyncio.Lock()
    return _ALOCK


# ---------------------------------------------------------------------------
# Transport — the single seam tests patch
# ---------------------------------------------------------------------------

def _endpoint() -> tuple[str, dict[str, str]]:
    from routers.auth.auth import CHAT_API_BASE_URL, get_auth_token

    token = get_auth_token()
    if not token:
        raise StateUnavailable(
            "This workspace's Composio account id comes from xo-swarm-api and this "
            "backend holds no XO credential. Set XO_API_KEY, or sign in to XO.",
            authoritative=True,
        )
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{IDENTITY_PATH}"
    return url, {"Authorization": f"Bearer {token}"}


def _request(*, params: Optional[dict] = None) -> Any:
    """One blocking round trip. Raises StateUnavailable; never returns an error shape."""
    url, headers = _endpoint()
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api could not be reached for this workspace's identity at "
            f"{url}: {exc}"
        ) from exc
    return _interpret(resp, url)


def _interpret(resp: httpx.Response, url: str) -> Any:
    if resp.status_code == 404:
        # Not a refusal: the swarm predates this route, which is equally a "do not
        # retry the same deploy" answer.
        raise StateUnavailable("not found", authoritative=True, not_found=True)
    if resp.status_code in (401, 403):
        raise StateUnavailable(
            f"xo-swarm-api rejected this backend's XO credential (HTTP "
            f"{resp.status_code}) for this workspace's identity.",
            authoritative=True,
        )
    if resp.status_code == 422:
        raise StateUnavailable(
            f"xo-swarm-api refused the identity request as invalid (HTTP 422): "
            f"{resp.text[:200]}",
            authoritative=True,
        )
    if resp.status_code >= 400:
        raise StateUnavailable(
            f"xo-swarm-api returned HTTP {resp.status_code} for this workspace's "
            f"identity at {url}."
        )
    try:
        return resp.json()
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api returned an unreadable identity payload: "
            f"{type(exc).__name__}."
        ) from exc


# Injected by the Coder pod. One Coder workspace = one pod = one home directory, so this
# is not a namespace key — the local stores are already isolated by the filesystem. Its
# one job is stamping ``sessions.json`` with the workspace that wrote it, so a store
# restored from a *different* workspace is discarded rather than adopted along with that
# workspace's connector scope. Never sent to Composio.
WORKSPACE_ENV = "CODER_WORKSPACE_ID"


class WorkspaceIdentityUnavailable(RuntimeError):
    """CODER_WORKSPACE_ID is unset or empty, so the store cannot be stamped."""


def workspace_id() -> str:
    """This pod's workspace id.

    Read at call time, not import time, so an operator (or a verification run) can change
    the environment without reimporting. Fails closed: there is no default and no
    ``"unknown"`` value, because a shared fallback would make every misconfigured pod
    claim ownership of every other's store.

    Raises:
        WorkspaceIdentityUnavailable: when the variable is unusable. Callers must surface
            this, never substitute a default.
    """
    value = (os.getenv(WORKSPACE_ENV) or "").strip()
    if value:
        return value
    raise WorkspaceIdentityUnavailable(f"{WORKSPACE_ENV} is not set")


def _workspace() -> str:
    return workspace_id()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def adopt_account_id(value: str) -> None:
    """Record the account id this pod's own store was written for.

    The store names its account, so a pod that has booted successfully once can keep
    serving the MCP hot path through an arbitrarily long swarm outage. This is a durable
    fact about local data, not a live grant, which is why an authoritative refusal from
    the swarm never falls back to it.
    """
    global _ACCOUNT_FROM_STORE
    value = (value or "").strip()
    if value:
        _ACCOUNT_FROM_STORE = value


def account_id_if_known() -> Optional[str]:
    """The last known account id, ignoring TTL. Never fetches.

    For paths that must not touch the network: proxy-token resolution runs on the agent
    hot path, on every ``tools/call``.
    """
    with _LOCK:
        cached = _IDENTITY
    return (cached[0] if cached else None) or _ACCOUNT_FROM_STORE


def identity_payload() -> dict:
    """This pod's identity from xo-swarm-api: account, workspace, legacy principal.

    The account id is a constant for the life of the pod, so the answer is cached. The
    workspace id is echoed back for symmetry only — this pod already knows its own, and
    :func:`workspace_id` is the authority.

    Raises :class:`StateUnavailable`. Callers that must not fail closed (the boot
    installer, the soft chat/tools paths) catch it.
    """
    global _IDENTITY

    now = time.monotonic()
    with _LOCK:
        cached = _IDENTITY
        if cached and cached[1] > now:
            return dict(cached[3])

    try:
        payload = _request(params={"workspace_id": _workspace()})
    except StateUnavailable as exc:
        # A 404 here is not "no", it is "this swarm predates the route" — a deploy
        # ordering slip, which must not take Composio down when the store already
        # names its owner.
        deploy_gap = exc.not_found
        if deploy_gap:
            log.error(
                "composio_state: xo-swarm-api has no %s. Deploy the swarm before this "
                "workspace.", IDENTITY_PATH,
            )
        with _LOCK:
            stale = _IDENTITY
            if stale and (deploy_gap or not exc.authoritative) and (now - stale[2]) < _STALE_MAX:
                _IDENTITY = (stale[0], now + _ERROR_TTL, stale[2], stale[3])
                log.warning(
                    "composio_state: identity refresh failed (%s); using the cached "
                    "value.", exc,
                )
                return dict(stale[3])
            if exc.authoritative and not deploy_gap:
                _IDENTITY = None
        if (deploy_gap or not exc.authoritative) and _ACCOUNT_FROM_STORE:
            log.warning(
                "composio_state: identity unavailable (%s); using the account recorded "
                "in this pod's own store.", exc,
            )
            # No legacy_principal: the store does not record one, and inventing it here
            # would mean composing the retired key locally. The reconnect prompt simply
            # does not appear until the swarm is reachable again.
            return {"account_id": _ACCOUNT_FROM_STORE, "workspace_id": None,
                    "legacy_principal": None}
        raise

    # Verbatim — no strip, no normalisation. Composio stores this string against every
    # connected account, so the bytes that arrive are the bytes that must be used.
    value = payload.get("account_id") or ""
    if not value:
        raise StateUnavailable(
            "xo-swarm-api returned an empty account_id.", authoritative=True,
        )
    with _LOCK:
        _IDENTITY = (value, now + _TTL, now, dict(payload))
    return dict(payload)


async def aidentity_payload() -> dict:
    """Async twin of :func:`identity_payload`."""
    now = time.monotonic()
    with _LOCK:
        cached = _IDENTITY
        if cached and cached[1] > now:
            return dict(cached[3])
    async with _alock():
        # Double-check under the lock: N concurrent requests on a cold cache must make
        # one round trip, not N.
        now = time.monotonic()
        with _LOCK:
            cached = _IDENTITY
            if cached and cached[1] > now:
                return dict(cached[3])
        return await asyncio.to_thread(identity_payload)


async def aaccount_id() -> str:
    """This pod's Composio ``user_id``, for the identity and proxy paths."""
    return (await aidentity_payload())["account_id"]


async def alegacy_principal() -> Optional[str]:
    """The retired ``<account>__ws__<workspace>`` key, for the migration probe only.

    None when the swarm could not be reached (the cached-store fallback does not record
    one) or when it has already dropped the field. Callers use it to *list* the connected
    accounts stranded under the old scheme so the UI can prompt a reconnect — never to
    address Composio for real work, and never to pin a session: Composio requires a
    pinned account to belong to the session's ``user_id``, so those accounts are
    unreachable by construction.
    """
    return (await aidentity_payload()).get("legacy_principal") or None
