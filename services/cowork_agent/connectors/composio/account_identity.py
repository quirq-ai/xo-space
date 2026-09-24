"""Whose connections these are: the XO **account**, not the Space.

Composio is addressed by the bare XO account id (``user_...``, what xo-swarm-api answers
``GET /get-user-id`` with), never by ``XO_SPACE_ID``. Connections at Composio are
account-wide, so addressing them by the account is what makes one sign-in enough: a
person who connects Gmail in one Space sees that connection in every other Space they
own, turns it on there (:mod:`.space_scope`, the per-Space true/false switch) and gets a
fresh session minted against the same connection. No second OAuth round.

What a Space may *reach* stays local and opt-in, which is the half that must not be
shared. Identity answers "whose?"; :mod:`.space_scope` answers "what, here?".

**Resolved out of band, cached on disk.** Every reader here is synchronous and one of
them (``account_for_proxy_token_local``) runs on the agent's MCP hot path, so nothing in
this module fetches on read. :func:`resolve` is awaited where a round trip is free — the
boot sweep, and the auth routes, which already hold the answer and just
:func:`remember` it — and the result is written to ``identity.json`` beside the other
Composio stores so a restart, and an xo-swarm-api outage, are both served from disk.

**Fails closed.** With no cached id and no credential there is no Composio user id, so
nothing connects and no session is minted. There is deliberately no fall back to
``XO_SPACE_ID``: a Space that quietly connected under its own id would file those
connections where no other Space could see them, which is the split this module exists
to remove.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from services.cowork_agent.connectors.composio import paths
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

#: Set it to pin the account id without a swarm round trip (offline work, tests).
ENV_VAR = "XO_ACCOUNT_ID"

STORE_VERSION = 1

_PATH = paths.store_dir() / "identity.json"

#: Seconds to wait before asking again after a failed lookup. The connector routes
#: resolve on a cold cache, so without this a swarm outage would cost one round trip per
#: page load; with it, the tab stays responsive and retries on its own.
RETRY_AFTER_SECONDS = 30.0

_LOCK = threading.Lock()
_CACHED: Optional[str] = None
_LOADED = False
_LAST_FAILURE = 0.0     # monotonic; 0 = no failure since the last success


class XOAccountRequired(RuntimeError):
    """This backend does not know which XO account it acts for, so Composio is inactive.

    ``authoritative`` marks it as an answer rather than an outage, the way
    :class:`~.byo_key.ComposioKeyRequired` does: waiting will not fix it, signing in
    will. The routes turn it into a 409 ``composio_identity_required``.
    """

    authoritative = True


def _env() -> Optional[str]:
    return (os.getenv(ENV_VAR) or "").strip() or None


def _from_disk() -> Optional[str]:
    doc = read_json(_PATH)
    if not isinstance(doc, dict):
        return None
    return str(doc.get("account_id") or "").strip() or None


def account_id() -> Optional[str]:
    """This backend's XO account id, or None when it has never been resolved.

    Memory, then ``XO_ACCOUNT_ID``, then the on-disk cache, which is read once. Never
    touches the network: the MCP proxy resolves a token through here on every
    ``tools/call``.
    """
    global _CACHED, _LOADED
    with _LOCK:
        if _CACHED:
            return _CACHED
        env = _env()
        if env:
            return env
        if _LOADED:
            return None
    # The read is outside the lock: it is a file hit, and a second concurrent reader
    # racing to the same answer is harmless.
    stored = _from_disk()
    with _LOCK:
        _LOADED = True
        if stored and not _CACHED:
            _CACHED = stored
        return _CACHED


def known() -> bool:
    return account_id() is not None


def require() -> str:
    """The account id, or raise. The gate every Composio call passes through."""
    value = account_id()
    if not value:
        raise XOAccountRequired(
            "This Space does not know which XO account it acts for, so connectors are "
            "inactive. Sign in to XO (or set XO_API_KEY) and reload."
        )
    return value


def remember(value: Optional[str]) -> Optional[str]:
    """Cache an account id the caller already holds, in memory and on disk.

    The auth routes call this with what the swarm just told them, so signing in costs
    no extra round trip. A blank value is ignored rather than treated as a signal to
    forget: only :func:`forget` forgets, and losing the id would strand the hot path.
    """
    global _CACHED, _LOADED
    value = (value or "").strip()
    if not value:
        return account_id()
    with _LOCK:
        unchanged = _CACHED == value
        _CACHED = value
        _LOADED = True
    if unchanged:
        return value
    try:
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        with locked(_PATH):
            write_json_atomic(_PATH, {
                "version": STORE_VERSION,
                "account_id": value,
                "resolved_at": time.time(),
            })
    except Exception as exc:  # noqa: BLE001 — a cache that cannot be written still works
        log.warning("composio: could not cache the XO account id: %s", exc)
    return value


def forget() -> None:
    """Drop the cached id, on disk and in memory. Test hook; nothing else calls it."""
    global _CACHED, _LOADED, _LAST_FAILURE
    with _LOCK:
        _CACHED = None
        _LOADED = False
        _LAST_FAILURE = 0.0
    try:
        _PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("composio: could not clear the cached XO account id: %s", exc)


async def resolve(*, force: bool = False) -> Optional[str]:
    """Ask xo-swarm-api who this backend's credential belongs to, and cache the answer.

    ``GET /get-user-id`` — the route that already validates this backend's token, so
    xo-swarm-api needs no change to serve connector identity. Returns the cached id
    without a round trip unless ``force``.

    Rate-limited after a failure (:data:`RETRY_AFTER_SECONDS`), because the connector
    routes call this on a cold cache: without the floor, a swarm outage would cost a
    round trip on every page load. ``force`` ignores both the cache and the floor.

    Never raises: a swarm that cannot be reached leaves whatever is cached in place and
    returns it (None when nothing is cached), which is what lets the boot sweep treat
    identity as retryable rather than fatal.
    """
    global _LAST_FAILURE
    if not force:
        cached = account_id()
        if cached:
            return cached
        with _LOCK:
            waiting = (
                _LAST_FAILURE
                and time.monotonic() - _LAST_FAILURE < RETRY_AFTER_SECONDS
            )
        if waiting:
            return None
    from services.swarm_api import auth as swarm_auth

    def _failed() -> None:
        global _LAST_FAILURE
        _LAST_FAILURE = time.monotonic()

    res = await swarm_auth.get_user_id()
    if not res.ok:
        if not res.unauthenticated:
            log.info("composio: xo-swarm-api did not answer the account id: %s", res.detail)
        _failed()
        return account_id()
    data = res.data if isinstance(res.data, dict) else {}
    resolved = str(data.get("user_id") or "").strip()
    if not resolved:
        log.warning("composio: xo-swarm-api returned no user_id for this credential.")
        _failed()
        return account_id()
    _LAST_FAILURE = 0.0
    return remember(resolved)
