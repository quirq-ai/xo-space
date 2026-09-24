"""Whose connections these are: the XO **account**, not the Space.

Composio is addressed by the bare XO account id (``user_...``, what xo-swarm-api answers
``GET /get-user-id`` with), never by ``XO_SPACE_ID``. Connections at Composio are
account-wide, so addressing them by the account is what makes one sign-in enough: a
person who connects Gmail in one Space sees that connection in every other Space they
own, turns it on there (:mod:`.space_scope`, the per-Space true/false switch) and gets a
fresh session minted against the same connection. No second OAuth round.

What a Space may *reach* stays local and opt-in, which is the half that must not be
shared. Identity answers "whose?"; :mod:`.space_scope` answers "what, here?".

**Two sources, so the second Space needs only the key** (:func:`resolve`): xo-swarm-api
first, then the Composio project the key opens. Composio records the ``user_id`` on every
connection, so a project that already holds connections already knows the account — which
is how a Space with no XO credential of its own still lands on the same identity as the
Space that made them. Paste the key, and the account reflects.

**Resolved out of band, cached on disk.** Every reader here is synchronous and one of
them (``account_for_proxy_token_local``) runs on the agent's MCP hot path, so nothing in
this module fetches on read. :func:`resolve` is awaited where a round trip is affordable
— the boot sweep, the connector routes on a cold cache, and the auth routes, which
already hold the answer and just :func:`remember` it — and the result is written to
``identity.json`` beside the other Composio stores so a restart, and an outage, are both
served from disk.

**Repairs itself.** A Space carried over from when Composio was addressed by
``XO_SPACE_ID`` has its own UUID cached where the account id belongs, and its project
holds connections filed under it. :func:`is_space_shaped` recognises both, so the cache
is discarded on the first read and the project's legacy ids are never adopted — the
account is then resolved afresh. The rest of the repair already follows on its own: the
session stamped for the old id is dropped (its proxy tokens kept) by
``service._ensure_sessions_loaded``, and the pins it owned are pruned by
``service.prune_scope_to_live_accounts`` on the next session build. Nothing to run by
hand.

**Fails closed.** With no cached id and neither source available there is no Composio
user id, so nothing connects and no session is minted. There is deliberately no fall back
to ``XO_SPACE_ID``: a Space that quietly connected under its own id would file those
connections where no other Space could see them, which is the split this module exists
to remove.
"""
from __future__ import annotations

import logging
import os
import re
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


#: A bare UUID is what a Space id looks like. An XO account id never is.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def is_space_shaped(value: object) -> bool:
    """Whether an id is really a *Space* id wearing the account's hat.

    Composio used to be addressed by ``XO_SPACE_ID``, so a Space carried over from that
    scheme has its own UUID cached here, and its project holds connections filed under
    it. Both would otherwise be adopted forever: :func:`account_id` returns the cache
    before anything else, and the project only hands the same value back.

    Recognising the shape is what makes the repair automatic. It rejects the known-bad
    shape rather than requiring a known-good one, so an XO id in some future format is
    not refused along with it.
    """
    if not isinstance(value, str) or not value:
        return False
    if _UUID_RE.match(value):
        return True
    space = (os.getenv("XO_SPACE_ID") or "").strip()
    return bool(space) and value == space


def _env() -> Optional[str]:
    # Not shape-checked: an operator who pins XO_ACCOUNT_ID means it, whatever it is.
    return (os.getenv(ENV_VAR) or "").strip() or None


def _from_disk() -> Optional[str]:
    doc = read_json(_PATH)
    if not isinstance(doc, dict):
        return None
    stored = str(doc.get("account_id") or "").strip() or None
    if stored and is_space_shaped(stored):
        log.warning(
            "composio: identity.json holds %s, which is a Space id, not an XO account "
            "id — a leftover from when Composio was addressed by XO_SPACE_ID. "
            "Discarding it and resolving the account afresh; the stale session is "
            "dropped and dead pins are pruned on the next session build.", stored,
        )
        try:
            _PATH.unlink()
        except OSError:
            pass
        return None
    return stored


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
    if is_space_shaped(value):
        # Never write one back: it is what the automatic repair has just removed.
        log.warning("composio: refusing to cache %s as the account id; it is a Space "
                    "id, not an XO account id.", value)
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
    """Establish which XO account this backend acts for, and cache the answer.

    Two sources, in order:

    1. **xo-swarm-api** (:func:`_from_swarm`), ``GET /get-user-id`` — the route that
       already validates this backend's token, so the swarm needs no change to serve
       connector identity. Authoritative, and right even for an empty project.
    2. **the Composio project** (:func:`_from_project`) — the connections the key's own
       project holds record the account on them. This is what makes a second Space need
       nothing but the same key: no XO credential, no swarm call.

    Returns the cached id without any round trip unless ``force``.

    Rate-limited after a failure (:data:`RETRY_AFTER_SECONDS`), because the connector
    routes call this on a cold cache: without the floor, an outage would cost a round
    trip on every page load. ``force`` ignores both the cache and the floor.

    Never raises: with both sources unavailable, whatever is cached stays in place and
    is returned (None when nothing is cached), which is what lets the boot sweep treat
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
    resolved = await _from_swarm()
    if not resolved:
        # No XO credential here, or the swarm is down. The Composio project the key
        # opens already records the account on its connections, so a Space that was
        # handed the same key can read the identity out of the project itself. This is
        # what makes the second Space need nothing but the key.
        resolved = await _from_project()
    if not resolved:
        _LAST_FAILURE = time.monotonic()
        return account_id()
    _LAST_FAILURE = 0.0
    return remember(resolved)


async def _from_swarm() -> Optional[str]:
    """The authoritative source: whoever this backend's XO credential belongs to.

    Preferred over the project because it is right even when the project is empty, and
    because it cannot be confused by a project holding more than one account.
    """
    from services.swarm_api import auth as swarm_auth

    res = await swarm_auth.get_user_id()
    if not res.ok:
        if not res.unauthenticated:
            log.info("composio: xo-swarm-api did not answer the account id: %s", res.detail)
        return None
    data = res.data if isinstance(res.data, dict) else {}
    resolved = str(data.get("user_id") or "").strip()
    if not resolved:
        log.warning("composio: xo-swarm-api returned no user_id for this credential.")
    return resolved or None


async def _from_project() -> Optional[str]:
    """The account the Composio key's own project already has connections under.

    Empty project, no key, or an unreachable Composio means None — this is a fallback,
    so it degrades to the signed-out state rather than raising. Runs in a worker thread:
    the SDK call is blocking and :func:`resolve` is awaited from request handlers.
    """
    import asyncio

    from services.cowork_agent.connectors.composio import byo_key
    from services.cowork_agent.connectors.composio import client

    if not byo_key.configured():
        return None
    try:
        candidates = await asyncio.to_thread(client.account_ids_in_project)
    except Exception as exc:  # noqa: BLE001 — a fallback must not raise
        log.info("composio: could not read the account id from the project: %s", exc)
        return None
    # A project from before account-scoping still holds connections filed under a
    # Space's UUID. Adopting one would put the Space id straight back where the account
    # id belongs, undoing the repair on the very next resolve.
    legacy = [uid for uid in candidates if is_space_shaped(uid)]
    candidates = [uid for uid in candidates if not is_space_shaped(uid)]
    if legacy:
        log.info("composio: ignoring %d Space-scoped id(s) in this project (%s); they "
                 "predate account-scoped connections.", len(legacy), ", ".join(legacy[:4]))
    if not candidates:
        return None
    if len(candidates) > 1:
        # One key, several accounts. Take the one holding most of the project and say
        # so: it is a real ambiguity, and the log is how an operator sees it.
        log.warning(
            "composio: this Composio project holds connections for %d accounts (%s); "
            "adopting %s. Set XO_ACCOUNT_ID to pin a different one.",
            len(candidates), ", ".join(candidates[:4]), candidates[0],
        )
    log.info("composio: adopted the account id %s from the Composio project.",
             candidates[0])
    return candidates[0]
