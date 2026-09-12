"""This space's Composio identity, resolved from xo-swarm-api.

Composio is addressed by the **bare Clerk account id**. This module is the client for
``GET /auth/workspace-principal``, a pure identity lookup that reads no database on either
side; it answers ``{account_id, space_id}``.

**Connections are account-wide**, and spaces are separated inside the Composio
tool-router session — see :mod:`.workspace_scope`. Never compose the account and space
into one key: an account connected under such a key is unreachable from an account-scoped
session, because Composio requires a pinned account to belong to the session's ``user_id``.

``XO_SPACE_ID`` (:func:`space_id`) is how this install names itself. It is sent to the
swarm as the ``space_id`` parameter here and in the session mint, and it stamps
``sessions.json`` — the ownership check that stops a store restored from another space
being adopted. It is never sent to Composio, and it is not a key in any store.

The account id is cached for the life of the pod. A swarm that cannot be reached falls
back to the cached value, then to the account recorded in this pod's own store
(:func:`adopt_account_id`). An *authoritative* refusal never falls back.

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


# Do not rename: a 404 here reads as "this swarm predates the route", so a rename would
# look like an outage on every space that has not been redeployed yet.
IDENTITY_PATH = os.getenv("XO_PRINCIPAL_PATH", "/auth/workspace-principal")

_TTL = float(os.getenv("COMPOSIO_STATE_TTL", "900"))
_ERROR_TTL = float(os.getenv("COMPOSIO_STATE_ERROR_TTL", "30"))
_STALE_MAX = float(os.getenv("COMPOSIO_STATE_STALE_MAX", "3600"))

# Tighter than the swarm client's default timeout (30s): some of these calls are sync and run
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
        # A 404 on the identity path is a deploy-ordering slip, not a refusal: the swarm
        # predates the route. The caller decides what to do about it.
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
    from routers.auth.auth import get_auth_token
    from services import swarm_api

    token = get_auth_token()
    if not token:
        raise StateUnavailable(
            "This space's Composio account id comes from xo-swarm-api and this "
            "backend holds no XO credential. Set XO_API_KEY, or sign in to XO.",
            authoritative=True,
        )
    url = f"{swarm_api.base_url()}{IDENTITY_PATH}"
    return url, {"Authorization": f"Bearer {token}"}


def _request(*, params: Optional[dict] = None) -> Any:
    """One blocking round trip. Raises StateUnavailable; never returns an error shape."""
    url, headers = _endpoint()
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api could not be reached for this space's identity at "
            f"{url}: {exc}"
        ) from exc
    return _interpret(resp, url)


def _interpret(resp: httpx.Response, url: str) -> Any:
    if resp.status_code == 404:
        raise StateUnavailable("not found", authoritative=True, not_found=True)
    if resp.status_code in (401, 403):
        raise StateUnavailable(
            f"xo-swarm-api rejected this backend's XO credential (HTTP "
            f"{resp.status_code}) for this space's identity.",
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
            f"xo-swarm-api returned HTTP {resp.status_code} for this space's "
            f"identity at {url}."
        )
    try:
        return resp.json()
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api returned an unreadable identity payload: "
            f"{type(exc).__name__}."
        ) from exc


# The id the swarm knows this Space by — the same value project sharing and usage
# reporting send — so one install has exactly one identity, on Coder and off. It names
# this install to xo-swarm-api and stamps ``sessions.json``, so a store restored from a
# *different* space is discarded rather than adopted along with that space's connector
# scope. Not a namespace key (the local stores are already isolated by the filesystem),
# and never sent to Composio.
SPACE_ENV = "XO_SPACE_ID"


class SpaceIdentityUnavailable(StateUnavailable):
    """XO_SPACE_ID is not set, so this install cannot name itself to xo-swarm-api.

    A :class:`StateUnavailable` subclass, and authoritative: the space id is read *inside*
    the identity fetch, so a plain ``RuntimeError`` here would escape the soft paths
    (chat, ``/api/tools``) that only guard against ``StateUnavailable`` and surface as a
    500. As an authoritative refusal it degrades the way a rejected credential does —
    no stale cache, no store fallback — which is right: an install that cannot name
    itself has no business being served another space's cached answer.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, authoritative=True)


def space_id() -> str:
    """This install's id at the swarm, from ``XO_SPACE_ID``.

    Read at call time, not import time, so an operator (or a verification run) can change
    the environment without reimporting. Fails closed: there is no default and no
    ``"unknown"`` value, because a shared fallback would have every misconfigured install
    claiming to be the same one.

    ``CODER_WORKSPACE_ID`` is deliberately **not** consulted, on Coder or anywhere else.
    Coder's id names a *pod*; ``XO_SPACE_ID`` names this install *at the swarm*, which is
    the identity every other XO-facing feature already uses.

    Raises:
        SpaceIdentityUnavailable: when the variable is unusable. Callers must surface
            this, never substitute a default.
    """
    value = (os.getenv(SPACE_ENV) or "").strip()
    if value:
        return value
    raise SpaceIdentityUnavailable(f"{SPACE_ENV} is not set")


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
    """This pod's identity from xo-swarm-api: account and space.

    The account id is a constant for the life of the pod, so the answer is cached. The
    space id is echoed back for symmetry only — this install already knows its own, and
    :func:`space_id` is the authority.

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
        payload = _request(params={"space_id": space_id()})
    except StateUnavailable as exc:
        # A deploy gap must not take Composio down when the store already names its owner.
        deploy_gap = exc.not_found
        if deploy_gap:
            log.error(
                "composio_state: xo-swarm-api has no %s. Deploy the swarm before this "
                "space.", IDENTITY_PATH,
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
            return {"account_id": _ACCOUNT_FROM_STORE, "space_id": None}
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
