"""Durable Composio tenant state, held by xo-swarm-api.

The session ids and MCP proxy tokens keyed by a Composio principal used to live only in
``data/composio_sessions.json`` inside this checkout. The published container mounts no
volume on ``/app/data``, so a pod recreation lost them — and because every agent's MCP
config has a proxy token baked into it, every agent came back to a 401 until the config
was rewritten with a fresh token. xo-swarm-api owns that state now; this module is the
client.

**The store is split, and the split is the point.** The swarm holds
``sha256(proxy_token)``; this pod keeps the plaintext, in the same 0600 file it always
has. The swarm only ever answers "which principal owns this token?", which a unique-index
lookup on the digest does exactly as well, so the shared table is not a usable credential
dump for every tenant at once. Two consequences worth knowing:

- **Resolution is local-first.** The hot path — the MCP proxy calls it on ``initialize``,
  ``tools/list`` and *every* ``tools/call`` — stays a dict lookup. The swarm is consulted
  only on a local miss, which is exactly the case this whole change exists for: the file
  died, the agent's config did not. The answer is written back, so the pod self-heals.
- **Mint must never fall back; resolve may.** If the swarm is unreachable while minting,
  this pod must not invent a token and write it into every agent's config — the swarm
  would never have seen it, and once the local file is dropped everything 401s while a
  re-install during the same outage can only mint another unrecorded one. Mint proceeds
  local-only, loudly, and reports ``durable=False`` so the reconcile sweep can say so;
  the next sweep re-registers the token once the swarm is back.

``COMPOSIO_STATE_SOURCE`` mirrors ``COMPOSIO_CREDENTIALS_SOURCE`` in
:mod:`.credentials` — ``local`` (the default today) writes through to the swarm but reads
from the file; ``swarm`` also reads from it. Nothing here is ever fatal: a swarm that is
down or predates these endpoints degrades to exactly today's behaviour.

Never log a token, and never send one: the wire carries a 64-hex digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


STATE_PATH = os.getenv("COMPOSIO_STATE_PATH", "/connectors/composio/state")
PRINCIPAL_PATH = os.getenv("XO_PRINCIPAL_PATH", "/auth/workspace-principal")

_TTL = float(os.getenv("COMPOSIO_STATE_TTL", "900"))
_ERROR_TTL = float(os.getenv("COMPOSIO_STATE_ERROR_TTL", "30"))
_STALE_MAX = float(os.getenv("COMPOSIO_STATE_STALE_MAX", "3600"))
_NEGATIVE_TTL = float(os.getenv("COMPOSIO_STATE_NEGATIVE_TTL", "60"))

# Bounded because the negative cache is keyed by attacker-supplied tokens. Mirrors the
# prune-on-insert in identity._validate_token.
_CACHE_MAX = 512

# Tighter than services.xo_credential.HTTP_TIMEOUT (30s): some of these calls are sync and run
# on the event loop, so a hung swarm must fail fast rather than stall every request.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# secrets.token_urlsafe(32) is 43 chars of URL-safe base64. Checking the shape before any
# lookup makes a garbage token cost nothing.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class StateUnavailable(RuntimeError):
    """The swarm could not answer.

    ``authoritative`` separates "the owner said no" (404/401/403 — never masked, never
    served from a stale cache) from "the owner could not be reached" (retryable, and the
    caller may serve a stale answer). The MCP proxy turns the first into a 401 telling the
    agent its config is stale, and the second into a retryable 503 — a 401 there would
    claim a stale config when the config is fine and the reconcile sweep could not
    rewrite it during the outage anyway.
    """

    def __init__(
        self, message: str, *, authoritative: bool = False, not_found: bool = False,
    ) -> None:
        super().__init__(message)
        self.authoritative = authoritative
        # A 404 means different things per route: "no such token" on /resolve, but
        # "this swarm predates the route" on the identity path. The caller decides.
        self.not_found = not_found


def token_fingerprint(token: str) -> str:
    """sha256 of a proxy token — the only form that ever crosses the wire."""
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def is_plausible_token(token: Optional[str]) -> bool:
    return bool(token) and bool(_TOKEN_RE.match(token))


def source() -> str:
    """``local`` (default) or ``swarm``. Unknown values fall back to ``local``."""
    raw = (os.getenv("COMPOSIO_STATE_SOURCE", "local") or "local").strip().lower()
    if raw not in {"local", "swarm"}:
        log.warning(
            "composio_state: COMPOSIO_STATE_SOURCE=%r is not 'local' or 'swarm'; "
            "using 'local'.", raw,
        )
        return "local"
    return raw


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_ALOCK: Optional[asyncio.Lock] = None
# token -> (principal | None, expires_at, fetched_at). A None principal is a cached
# negative: without it a stale agent config retries forever over HTTP.
_TOKEN_CACHE: dict[str, tuple[Optional[str], float, float]] = {}
# (principal, expires_at, fetched_at, payload) — one value for the life of the pod.
_PRINCIPAL: Optional[tuple[str, float, float, dict]] = None
# What this pod's own store says its rows belong to. No expiry: it is a fact about local
# data, and it is what lets a pod that has booted once ride out a swarm outage.
_PRINCIPAL_FROM_STORE: Optional[str] = None


def _prune(now: float) -> None:
    for key in [k for k, (_, exp, _) in _TOKEN_CACHE.items() if exp <= now]:
        _TOKEN_CACHE.pop(key, None)
    if len(_TOKEN_CACHE) > _CACHE_MAX:
        for key in sorted(_TOKEN_CACHE, key=lambda k: _TOKEN_CACHE[k][1])[
            : len(_TOKEN_CACHE) - _CACHE_MAX
        ]:
            _TOKEN_CACHE.pop(key, None)


def invalidate() -> None:
    """Drop every cache. Test hook, and the seam a refresh route would use."""
    global _ALOCK, _PRINCIPAL, _PRINCIPAL_FROM_STORE
    with _LOCK:
        _TOKEN_CACHE.clear()
        _PRINCIPAL = None
        _PRINCIPAL_FROM_STORE = None
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

def _endpoint(suffix: str = "", base: str = "") -> tuple[str, dict[str, str]]:
    from services.xo_credential import CHAT_API_BASE_URL, get_auth_token

    token = get_auth_token()
    if not token:
        raise StateUnavailable(
            "Composio tenant state lives in xo-swarm-api and this backend holds no XO "
            "credential. Set XO_API_KEY, or sign in to XO.",
            authoritative=True,
        )
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{base or STATE_PATH}{suffix}"
    return url, {"Authorization": f"Bearer {token}"}


def _request(
    method: str,
    suffix: str = "",
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    base: str = "",
) -> Any:
    """One blocking round trip. Raises StateUnavailable; never returns an error shape."""
    url, headers = _endpoint(suffix, base)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.request(method, url, headers=headers, params=params, json=json)
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api could not be reached for Composio state at {url}: {exc}"
        ) from exc
    return _interpret(resp, url)


async def _arequest(
    method: str,
    suffix: str = "",
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    base: str = "",
) -> Any:
    """Async twin of :func:`_request`, for the MCP proxy's hot path."""
    url, headers = _endpoint(suffix, base)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.request(
                method, url, headers=headers, params=params, json=json
            )
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api could not be reached for Composio state at {url}: {exc}"
        ) from exc
    return _interpret(resp, url)


def _interpret(resp: httpx.Response, url: str) -> Any:
    if resp.status_code == 404:
        # Authoritative on /resolve ("no such token"); on the other routes it means the
        # swarm predates these endpoints, which is equally a "do not retry" answer.
        raise StateUnavailable("not found", authoritative=True, not_found=True)
    if resp.status_code in (401, 403):
        raise StateUnavailable(
            f"xo-swarm-api rejected this backend's XO credential (HTTP "
            f"{resp.status_code}) for Composio state.",
            authoritative=True,
        )
    if resp.status_code == 422:
        raise StateUnavailable(
            f"xo-swarm-api refused the Composio state request as invalid (HTTP 422): "
            f"{resp.text[:200]}",
            authoritative=True,
        )
    if resp.status_code >= 400:
        raise StateUnavailable(
            f"xo-swarm-api returned HTTP {resp.status_code} for Composio state at {url}."
        )
    try:
        return resp.json()
    except Exception as exc:
        raise StateUnavailable(
            f"xo-swarm-api returned an unreadable Composio state payload: "
            f"{type(exc).__name__}."
        ) from exc


def _workspace() -> str:
    from services import tenancy

    return tenancy.workspace_id()


# ---------------------------------------------------------------------------
# Writes — best-effort, never fatal
# ---------------------------------------------------------------------------

def put_session_id(session_id: Optional[str]) -> bool:
    """Mirror this tenant's Composio session id to the swarm. Returns success."""
    try:
        _request(
            "PUT", "/session",
            json={"workspace_id": _workspace(), "session_id": session_id},
        )
        return True
    except Exception as exc:
        log.warning("composio_state: could not persist session id to xo-swarm-api: %s", exc)
        return False


def put_proxy_token(token: str) -> bool:
    """Register a proxy token's digest with the swarm. Returns whether it is durable.

    The caller must surface a False — an agent config pointing at a token the swarm has
    never seen will 401 the moment this pod's local file is lost.
    """
    try:
        _request(
            "PUT", "/proxy-token",
            json={
                "workspace_id": _workspace(),
                "token_sha256": token_fingerprint(token),
            },
        )
        return True
    except Exception as exc:
        log.warning(
            "composio_state: proxy token is NOT durable — xo-swarm-api did not record "
            "it (%s). It works until this pod's local store is lost, after which the "
            "agent must re-install its MCP config.", exc,
        )
        return False


def put_prefs(toolkit: str, updates: dict[str, bool]) -> bool:
    """Merge per-action prefs for one toolkit into the swarm. Returns success."""
    try:
        _request(
            "PUT", f"/prefs/{toolkit}",
            json={"workspace_id": _workspace(), "updates": updates},
        )
        return True
    except Exception as exc:
        log.warning("composio_state: could not persist prefs to xo-swarm-api: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def adopt_principal(value: str) -> None:
    """Record the principal this pod's own store says its rows belong to.

    The store names its owner, so a pod that has booted successfully once can classify
    its rows — and keep serving the MCP hot path — through an arbitrarily long swarm
    outage. This is a durable fact about local data, not a live grant, which is why an
    authoritative refusal from the swarm never falls back to it.
    """
    global _PRINCIPAL_FROM_STORE
    value = (value or "").strip()
    if value:
        _PRINCIPAL_FROM_STORE = value


def principal_if_known() -> Optional[str]:
    """The last known principal, ignoring TTL. Never fetches.

    For paths that must not touch the network: store classification and proxy-token
    resolution both run on the agent hot path.
    """
    with _LOCK:
        cached = _PRINCIPAL
    return (cached[0] if cached else None) or _PRINCIPAL_FROM_STORE


def principal_payload() -> dict:
    """This pod's tenant identity from xo-swarm-api: account, workspace, principal.

    One workspace is one tenant, so the answer is a constant for the life of the pod —
    but it is *composed* by the swarm, the only place that rule lives. xo-space supplies
    the one half the swarm cannot know: its own workspace id.

    Raises :class:`StateUnavailable`. Callers that must not fail closed (the boot
    installer, the soft chat/tools paths) catch it.
    """
    global _PRINCIPAL

    now = time.monotonic()
    with _LOCK:
        cached = _PRINCIPAL
        if cached and cached[1] > now:
            return dict(cached[3])

    try:
        payload = _request(
            "GET", params={"workspace_id": _workspace()}, base=PRINCIPAL_PATH
        )
    except StateUnavailable as exc:
        # A 404 here is not "no", it is "this swarm predates the route" — a deploy
        # ordering slip, which must not take Composio down when the store already
        # names its owner.
        deploy_gap = exc.not_found
        if deploy_gap:
            log.error(
                "composio_state: xo-swarm-api has no %s. Deploy the swarm before this "
                "workspace.", PRINCIPAL_PATH,
            )
        with _LOCK:
            stale = _PRINCIPAL
            if stale and (deploy_gap or not exc.authoritative) and (now - stale[2]) < _STALE_MAX:
                _PRINCIPAL = (stale[0], now + _ERROR_TTL, stale[2], stale[3])
                log.warning(
                    "composio_state: principal refresh failed (%s); using the cached "
                    "value.", exc,
                )
                return dict(stale[3])
            if exc.authoritative and not deploy_gap:
                _PRINCIPAL = None
        if (deploy_gap or not exc.authoritative) and _PRINCIPAL_FROM_STORE:
            log.warning(
                "composio_state: principal unavailable (%s); using the owner recorded "
                "in this pod's own store.", exc,
            )
            return {"principal": _PRINCIPAL_FROM_STORE, "account_id": None,
                    "workspace_id": None}
        raise

    # Verbatim — no strip, no normalisation. Composio stores this string against every
    # connected account, so the bytes that arrive are the bytes that must be used.
    value = payload.get("principal") or ""
    if not value:
        raise StateUnavailable(
            "xo-swarm-api returned an empty principal.", authoritative=True,
        )
    with _LOCK:
        _PRINCIPAL = (value, now + _TTL, now, dict(payload))
    return dict(payload)


def principal() -> str:
    """This pod's Composio tenant key. See :func:`principal_payload`."""
    return principal_payload()["principal"]


async def aprincipal_payload() -> dict:
    """Async twin of :func:`principal_payload`."""
    now = time.monotonic()
    with _LOCK:
        cached = _PRINCIPAL
        if cached and cached[1] > now:
            return dict(cached[3])
    async with _alock():
        # Double-check under the lock: N concurrent requests on a cold cache must make
        # one round trip, not N.
        now = time.monotonic()
        with _LOCK:
            cached = _PRINCIPAL
            if cached and cached[1] > now:
                return dict(cached[3])
        return await asyncio.to_thread(principal_payload)


async def aprincipal() -> str:
    """Async twin of :func:`principal`, for the identity and proxy paths."""
    return (await aprincipal_payload())["principal"]


def assert_principal_is_ours(remote: str) -> Optional[str]:
    """Refuse a principal that is not this pod's tenant.

    Reached only from :func:`resolve_proxy_token`, the recovery path. The swarm already
    filters by account, so a mismatch here means a different *workspace* of the same
    account — another pod's token presented to this one. Adopting it would re-home that
    token into this workspace's tenant, which is exactly what the workspace half of the
    key exists to prevent.

    Accepts when this pod's principal is not yet known, so a cold cache does not turn
    recovery into a dead end.
    """
    ours = principal_if_known()
    if ours and remote != ours:
        log.error(
            "composio_state: refusing a proxy token owned by %r — this workspace is %r. "
            "Tokens do not cross workspaces.", remote, ours,
        )
        return None
    return remote


async def resolve_proxy_token(token: str) -> Optional[str]:
    """Resolve a proxy token to its principal via the swarm, cached.

    Only reached on a local miss. Raises :class:`StateUnavailable` when the swarm cannot
    be reached and nothing is cached — the proxy turns that into a retryable 503 rather
    than a misleading "re-install your config".
    """
    if not is_plausible_token(token):
        return None

    now = time.monotonic()
    with _LOCK:
        cached = _TOKEN_CACHE.get(token)
        if cached and cached[1] > now:
            return cached[0]

    async with _alock():
        # Double-check: N concurrent tool calls on a cold cache must make one round trip.
        now = time.monotonic()
        with _LOCK:
            cached = _TOKEN_CACHE.get(token)
            if cached and cached[1] > now:
                return cached[0]

        try:
            payload = await _arequest(
                "POST", "/resolve", json={"token_sha256": token_fingerprint(token)}
            )
        except StateUnavailable as exc:
            if exc.authoritative:
                # "No such token" is a real answer; cache it so a stale agent config
                # does not retry in a loop.
                with _LOCK:
                    _prune(now)
                    _TOKEN_CACHE[token] = (None, now + _NEGATIVE_TTL, now)
                return None
            with _LOCK:
                stale = _TOKEN_CACHE.get(token)
                if stale and stale[0] and (now - stale[2]) < _STALE_MAX:
                    _TOKEN_CACHE[token] = (stale[0], now + _ERROR_TTL, stale[2])
                    log.warning(
                        "composio_state: resolve failed (%s); serving the cached "
                        "principal.", exc,
                    )
                    return stale[0]
            raise

        principal = str(payload.get("principal") or "") or None
        if principal:
            # The swarm filters by account, not workspace: a sibling workspace's token
            # must not be adopted into this one's tenant.
            principal = assert_principal_is_ours(principal)
        with _LOCK:
            _prune(now)
            ttl = _TTL if principal else _NEGATIVE_TTL
            _TOKEN_CACHE[token] = (principal, now + ttl, now)
        return principal


def cache_principal(token: str, principal: str) -> None:
    """Seed the cache from a local hit, so a later miss is the only network call."""
    if not token or not principal:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _TOKEN_CACHE[token] = (principal, now + _TTL, now)
