"""Router-facing facade for polled connections. Raises
:class:`ConnectionsError`; knows nothing about HTTP. The only connections
module the BFF imports, so the route handlers stay free of os/pathlib
(BFF rule P2).

One entry per toolkit the Composio catalog knows, configured or not: an
unconfigured toolkit reports the defaults (``configured`` false, null poll
fields) so the Connectors tab's Polling drawer can render a form before a
``config.json`` exists. Nothing is written until :func:`configure` runs.
Every entry also carries ``account_label`` and ``account_checked_at``, the
account the toolkit's session is bound to as cached in ``accounts.json``
(separate from the polling state, so an unconfigured toolkit can carry
one); :func:`refresh_account` resolves it live through the poller.

Core code: names no agent and imports nothing from the adapters tree.
``signed_in`` looks at the auth router lazily (inside the function) so
importing this module never pulls a router in at load time. ``poll_now``
tells the listeners registered through :func:`register_new_events_listener`
when a poll collected something; the inbox registers one, this package
never imports the inbox.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from services.cowork_agent.connectors.composio import space_scope
from services.cowork_agent.connectors.composio.service import TOOLKITS

from . import collectors, poller, store
from .poller import poller_enabled  # re-exported: the router reports it next to signed_in
from .store import ConnectionsError  # re-exported: the router catches service.ConnectionsError

__all__ = [
    "ConnectionsError", "UNSET", "list_connections", "get_connection", "configure",
    "events", "remove", "poll_now", "refresh_account", "signed_in", "poller_enabled",
    "register_new_events_listener",
]

logger = logging.getLogger(__name__)

EVENTS_LIMIT_MIN, EVENTS_LIMIT_MAX, EVENTS_LIMIT_DEFAULT = 1, 500, 50

#: ``configure(field=UNSET)`` means "leave that field alone".
UNSET = object()


def _check_known(toolkit) -> str:
    """The catalog decides which ids exist; everything else is a 404 before
    any path is built (the store repeats the regex check on its own)."""
    if not isinstance(toolkit, str) or store.TOOLKIT_RE.fullmatch(toolkit) is None or toolkit not in TOOLKITS:
        raise ConnectionsError("unknown_toolkit", f"Unknown toolkit {toolkit!r}.", 404)
    return toolkit


def _connected_here(toolkit: str) -> bool:
    try:
        return bool(space_scope.is_enabled(toolkit))
    except Exception:
        logger.warning("connections: workspace scope unreadable for %s", toolkit, exc_info=True)
        return False


def _available(toolkit: str) -> list[dict]:
    return [{"id": spec["id"], "label": spec["label"], "default": bool(spec.get("default"))}
            for spec in collectors.catalog(toolkit)]


_NULL_STATE = {"last_poll_at": None, "last_ok_at": None, "last_error": None, "events_total": 0}


def _entry(toolkit: str, config: Optional[dict], accounts: dict) -> dict:
    """The connection dict for one toolkit; ``config`` is the normalised
    ``config.json`` or ``None`` when the folder holds none (then the poll
    fields are null: a stray ``state.json`` without a config is not shown);
    ``accounts`` is ``store.read_accounts()``, read once per listing."""
    configured = config is not None
    state_doc = store.read_state(toolkit) if configured else dict(_NULL_STATE)
    account = accounts.get(toolkit) or {}
    return {
        "toolkit": toolkit,
        "display_name": TOOLKITS[toolkit].display_name,
        "configured": configured,
        "enabled": config["enabled"] if configured else True,
        "interval_s": config["interval_s"] if configured else store.INTERVAL_DEFAULT,
        "collectors": list(config["collectors"]) if configured else collectors.default_ids(toolkit),
        "available_collectors": _available(toolkit),
        "connected_here": _connected_here(toolkit),
        "last_poll_at": state_doc["last_poll_at"],
        "last_ok_at": state_doc["last_ok_at"],
        "last_error": state_doc["last_error"],
        "events_total": state_doc["events_total"],
        "account_label": account.get("label"),
        "account_checked_at": account.get("checked_at"),
    }


def _read_config_or_none(toolkit: str) -> Optional[dict]:
    """A config.json that is not JSON reads as "not configured" here (the
    store already warned); the poller skips it the same way."""
    try:
        return store.read_config(toolkit)
    except Exception:
        logger.warning("connections: config.json unreadable for %s", toolkit, exc_info=True)
        return None


# ── Reads ────────────────────────────────────────────────────────────────────


def list_connections() -> list[dict]:
    """One entry per toolkit id in the Composio catalog, sorted by id."""
    accounts = store.read_accounts()
    return [_entry(toolkit, _read_config_or_none(toolkit), accounts) for toolkit in sorted(TOOLKITS)]


def get_connection(toolkit: str) -> dict:
    _check_known(toolkit)
    return _entry(toolkit, _read_config_or_none(toolkit), store.read_accounts())


def events(toolkit: str, limit: int = EVENTS_LIMIT_DEFAULT) -> list[dict]:
    """Collected events newest-first; ``limit`` clamped to 1..500 for
    programmatic callers (the route's Query bounds already 422 outside)."""
    _check_known(toolkit)
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = EVENTS_LIMIT_DEFAULT
    wanted = max(EVENTS_LIMIT_MIN, min(EVENTS_LIMIT_MAX, wanted))
    return store.read_events(toolkit, limit=wanted)


# ── Writes ───────────────────────────────────────────────────────────────────


def configure(toolkit: str, *, enabled=UNSET, interval_s=UNSET, collectors=UNSET) -> dict:
    """Create the folder and ``config.json`` on first call, merge the given
    fields (validated by the store), return the fresh connection dict."""
    _check_known(toolkit)
    fields = {name: value for name, value in
              (("enabled", enabled), ("interval_s", interval_s), ("collectors", collectors))
              if value is not UNSET}
    store.write_config(toolkit, **fields)
    logger.info("connections: %s configured (%s)", toolkit, ", ".join(sorted(fields)) or "defaults")
    return get_connection(toolkit)


def remove(toolkit: str) -> bool:
    """Delete that toolkit's folder (config, state and events). ``False``
    when there was nothing to remove."""
    _check_known(toolkit)
    removed = store.remove(toolkit)
    if removed:
        logger.info("connections: %s removed", toolkit)
    return removed


async def poll_now(toolkit: str) -> dict:
    """Poll one connection right away, ignoring ``enabled`` and the
    interval. The poll summary: ``{"toolkit", "polled", "new_events",
    "error", "skipped"}``.

    When the poll collected something, every listener registered through
    :func:`register_new_events_listener` is awaited (best-effort, see
    :func:`_notify_new_events`). The inbox registers one to ingest at once:
    the Inbox tab reloads its rows through ``GET /api/inbox`` right after
    this call, and that read throttles its own ingest to one per few
    seconds, so without the nudge the fresh events would sit in
    ``events.jsonl`` until the next tick."""
    _check_known(toolkit)
    outcome = await poller.poll_connection(toolkit, force=True)
    if outcome.get("new_events"):
        await _notify_new_events(toolkit)
    return outcome


async def refresh_account(toolkit: str) -> dict:
    """Resolve the account the toolkit's session is bound to, live, and
    cache it: ``{"toolkit", "account_label", "account_checked_at", "error",
    "cached"}`` (see :func:`poller.refresh_account`). A provider or session
    failure is an ``error`` in that dict, never a raise; only an unknown
    toolkit raises (404)."""
    _check_known(toolkit)
    return await poller.refresh_account(toolkit)


# ── New-events listeners ─────────────────────────────────────────────────────
# The dependency points one way: the inbox knows about connections (its
# feeder reads this package's events, and it registers a listener here at
# import time); this package never imports the inbox.

_new_events_listeners: list = []


def register_new_events_listener(fn) -> None:
    """Register ``async fn(toolkit)`` to run after a :func:`poll_now` that
    collected something. Registering the same callable again is a no-op."""
    if not any(existing is fn for existing in _new_events_listeners):
        _new_events_listeners.append(fn)


async def _notify_new_events(toolkit: str) -> None:
    """Await every listener in registration order. A listener that fails is
    logged and skipped (the events are on disk; its next own read picks
    them up); cancellation propagates."""
    for fn in list(_new_events_listeners):
        try:
            await fn(toolkit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("connections: listener %s failed after polling %s",
                           getattr(fn, "__qualname__", repr(fn)), toolkit, exc_info=True)


# ── Status ───────────────────────────────────────────────────────────────────


def signed_in() -> bool:
    """Whether this workspace holds a token for the platform (the poller
    needs one to resolve the account id). Imported lazily so this module
    never loads a router at import time; any failure reads as ``False``."""
    try:
        from routers.auth.auth import get_auth_token
        return bool(get_auth_token())
    except Exception:
        return False
