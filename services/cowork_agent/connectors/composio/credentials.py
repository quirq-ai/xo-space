"""Where the Composio credentials come from.

xo-swarm-api owns one global Composio credential set — the API key and the eight
auth-config ids — and serves it over ``GET /connectors/composio/credentials`` to any
install that can authenticate with its XO credential. This module is the only place in
xo-space that knows that; ``service.py`` asks for a key and an id and does not care
which side of the wire they came from.

Nothing here is ever logged. The bundle is a secret; the only things that reach the log
are the source, the status code, and the *names* of the configured auth configs.

Two failures must stay distinguishable, and the cache turns on it:

    authoritative   the owner answered, and said either "there is no key" (503) or
                    "not you" (401/403). Never masked by a local env value, never
                    served from a stale cache — a revoked credential must stop working.
    transient       the owner could not be reached. The last good bundle is served for
                    up to ``COMPOSIO_CREDENTIALS_STALE_MAX`` so a swarm restart does not
                    take every connector down with it.

Every message raised from here contains the literal string ``COMPOSIO_API_KEY``. That is
load-bearing: ``space_ui/js/views/connectors.js`` matches on it to tell "Composio is not
configured" apart from a generic failure.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


CREDENTIALS_PATH = os.getenv(
    "COMPOSIO_CREDENTIALS_PATH", "/connectors/composio/credentials"
)
AUTH_CONFIG_PREFIX = "COMPOSIO_AUTH_CONFIG_"

_TTL = float(os.getenv("COMPOSIO_CREDENTIALS_TTL", "300"))
_ERROR_TTL = float(os.getenv("COMPOSIO_CREDENTIALS_ERROR_TTL", "30"))
_STALE_MAX = float(os.getenv("COMPOSIO_CREDENTIALS_STALE_MAX", "3600"))

# Deliberately tighter than services.xo_credential.HTTP_TIMEOUT (30s/10s): this call is sync
# and runs on the event loop, so a hung swarm must fail fast into the documented degraded
# state rather than stall every request for half a minute.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class CredentialsUnavailable(RuntimeError):
    """No usable Composio credentials.

    A ``RuntimeError`` on purpose: the connectors router maps ``RuntimeError`` to 422 on
    ``/connect`` and lets it 500 elsewhere, which is exactly the degradation contract in
    DEVELOPING.md §10.3. Raising a new exception type here would silently change it.
    """

    def __init__(self, message: str, *, authoritative: bool = False) -> None:
        super().__init__(message)
        self.authoritative = authoritative


@dataclass(frozen=True)
class Bundle:
    api_key: str
    auth_configs: dict[str, str]
    source: str  # "swarm" | "env"
    fetched_at: float  # time.monotonic()


# Mirrors services/xo_credential.py's auth_lock: the fetch can be entered from several
# request handlers at once, and one blocking round trip is enough.
_LOCK = threading.Lock()
_BUNDLE: Optional[Bundle] = None  # only ever a "swarm" bundle
_NEXT_ATTEMPT: float = 0.0


def _source() -> str:
    """``swarm`` (default) or ``env``.

    ``env`` restores the pre-migration behaviour for a self-hosted install with its own
    Composio project, and for the test suite. It has to be named: a fallback that fires
    on error would mask a swarm outage as "not configured" and, worse, let anything that
    can write this process's environment — the Setup tab writes into it — override the
    organisation's credential and point the connector at another Composio project.
    """
    raw = (os.getenv("COMPOSIO_CREDENTIALS_SOURCE", "swarm") or "swarm").strip().lower()
    if raw not in {"swarm", "env"}:
        log.warning(
            "composio_credentials: COMPOSIO_CREDENTIALS_SOURCE=%r is not 'swarm' or "
            "'env'; using 'swarm'.", raw,
        )
        return "swarm"
    return raw


def _from_env() -> Bundle:
    api_key = (os.getenv("COMPOSIO_API_KEY") or "").strip()
    if not api_key:
        raise CredentialsUnavailable(
            "COMPOSIO_API_KEY is not set in this process's environment, and "
            "COMPOSIO_CREDENTIALS_SOURCE=env asks for the local value."
        )
    # Scanned by prefix rather than read off service.TOOLKITS: importing service from
    # here would be a load cycle, and the scan covers any toolkit the table gains later.
    return Bundle(
        api_key=api_key,
        auth_configs={
            name: value.strip()
            for name, value in os.environ.items()
            if name.startswith(AUTH_CONFIG_PREFIX) and (value or "").strip()
        },
        source="env",
        fetched_at=time.monotonic(),
    )


def _get(url: str, headers: dict[str, str]) -> httpx.Response:
    """The one network seam, so tests patch a single function."""
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        return client.get(url, headers=headers)


def _fetch_from_swarm() -> Bundle:
    # Deferred import: routers.auth and this package import each other lazily to avoid a
    # load cycle (same reason as identity._validate_token).
    from services.xo_credential import CHAT_API_BASE_URL, get_auth_token

    token = get_auth_token()
    if not token:
        raise CredentialsUnavailable(
            "COMPOSIO_API_KEY comes from xo-swarm-api and this backend holds no XO "
            "credential. Set XO_API_KEY, or sign in to XO. A self-hosted install with "
            "its own Composio project can set COMPOSIO_CREDENTIALS_SOURCE=env instead.",
            authoritative=True,
        )

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{CREDENTIALS_PATH}"
    try:
        resp = _get(url, {"Authorization": f"Bearer {token}"})
    except Exception as exc:
        raise CredentialsUnavailable(
            f"COMPOSIO_API_KEY could not be fetched from xo-swarm-api at {url}: {exc}. "
            "Check CHAT_API_BASE_URL and that xo-swarm-api is reachable."
        ) from exc

    if resp.status_code == 503:
        raise CredentialsUnavailable(
            "COMPOSIO_API_KEY is not configured on xo-swarm-api (its "
            f"{CREDENTIALS_PATH} returned 503). Set it there and restart it.",
            authoritative=True,
        )
    if resp.status_code in (401, 403):
        raise CredentialsUnavailable(
            f"xo-swarm-api rejected this backend's XO credential (HTTP "
            f"{resp.status_code}), so COMPOSIO_API_KEY could not be fetched. Fix or "
            "replace XO_API_KEY.",
            authoritative=True,
        )
    if resp.status_code != 200:
        raise CredentialsUnavailable(
            "COMPOSIO_API_KEY could not be fetched from xo-swarm-api "
            f"(HTTP {resp.status_code})."
        )

    try:
        data = resp.json()
        api_key = str(data.get("api_key") or "").strip()
        raw = data.get("auth_configs") or {}
        auth_configs = {
            str(name): str(value).strip()
            for name, value in raw.items()
            if isinstance(name, str) and str(value or "").strip()
        }
    except Exception as exc:  # never echo the body — it carries the key
        raise CredentialsUnavailable(
            "xo-swarm-api returned an unreadable COMPOSIO_API_KEY payload: "
            f"{type(exc).__name__}."
        ) from exc
    if not api_key:
        raise CredentialsUnavailable(
            "xo-swarm-api returned an empty COMPOSIO_API_KEY.", authoritative=True,
        )

    log.info(
        "composio_credentials: fetched from xo-swarm-api (%d auth configs: %s)",
        len(auth_configs), ", ".join(sorted(auth_configs)) or "none",
    )
    return Bundle(api_key, auth_configs, "swarm", time.monotonic())


def bundle() -> Bundle:
    """The active credential set, cached. Raises :class:`CredentialsUnavailable`."""
    global _BUNDLE, _NEXT_ATTEMPT

    if _source() == "env":
        # Uncached on purpose: reading the environment is free, and service.py's tests
        # rely on every env read happening at call time.
        return _from_env()

    now = time.monotonic()
    with _LOCK:
        if _BUNDLE is not None and now < _NEXT_ATTEMPT:
            return _BUNDLE
        try:
            fresh = _fetch_from_swarm()
        except CredentialsUnavailable as exc:
            serve_stale = (
                _BUNDLE is not None
                and not exc.authoritative
                and (now - _BUNDLE.fetched_at) < _STALE_MAX
            )
            if serve_stale:
                _NEXT_ATTEMPT = now + _ERROR_TTL
                log.warning(
                    "composio_credentials: refresh failed (%s); serving the cached "
                    "bundle for up to %.0fs more.",
                    exc, _STALE_MAX - (now - _BUNDLE.fetched_at),
                )
                return _BUNDLE
            _BUNDLE, _NEXT_ATTEMPT = None, now + _ERROR_TTL
            raise
        _BUNDLE, _NEXT_ATTEMPT = fresh, now + _TTL
        return fresh


def api_key() -> str:
    return bundle().api_key


def auth_config_id(env_key: str) -> Optional[str]:
    """The configured id for one ``COMPOSIO_AUTH_CONFIG_*`` name, or ``None``.

    ``None`` rather than an exception for a *missing id*: ``service._auth_config_id_for``
    owns that message because it is the only caller that knows the toolkit slug and the
    auth scheme. A missing *bundle* still raises — that is a different failure, and the
    two get different HTTP treatment.
    """
    return (bundle().auth_configs.get(env_key) or "").strip() or None


def invalidate() -> None:
    """Drop the cache. Test hook, and the seam a future refresh route would use."""
    global _BUNDLE, _NEXT_ATTEMPT
    with _LOCK:
        _BUNDLE, _NEXT_ATTEMPT = None, 0.0


def status() -> dict:
    """A key-free snapshot for diagnostics. Never returns credential material."""
    try:
        current = bundle()
    except CredentialsUnavailable as exc:
        return {"source": "unavailable", "configured": [], "error": str(exc)}
    return {
        "source": current.source,
        "configured": sorted(current.auth_configs),
        "error": None,
    }
