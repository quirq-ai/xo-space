"""The connections poller: a standalone loop, deliberately not a watcher sink.

Every :data:`ENV_TICK` seconds it looks for due connections (a folder
with a ``config.json``, enabled, and ``interval_s`` elapsed since
``last_poll_at``) and runs each one's collectors over the same MCP
upstream the agent proxy uses. New items (keys not in the collector's
``seen`` cursor, deduplicated within the batch) are appended to
``events.jsonl``; the outcome lands in ``state.json``.

Degradation is recorded, never raised: no account id -> ``last_error``
"not signed in to XO (no account id)"; the toolkit not turned on in this
workspace -> ``last_error`` says so; a collector that fails leaves the
others running and its message joins ``last_error``. Every attempted poll
stamps ``last_poll_at`` (so a failing connection is retried at its
interval, not every tick); ``last_ok_at`` moves only when nothing failed.

The loop never creates a folder, never polls a toolkit without a
``config.json``, and survives a corrupt config (WARN, skip). A per-toolkit
``asyncio.Lock`` makes a "poll now" from the route and the loop's own tick
take turns (a forced "poll now" waits up to :data:`FORCE_WAIT_S` for the
tick to finish, any other second caller is answered "busy"); ``flock`` in
the store is the cross-process guard. Each poll lists the session's tools
once: a slug that is exposed is called directly, anything else runs through
Composio's executor tool (see ``mcp_client.execute_tool``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.connectors.composio import state, workspace_scope
from services.inbox.store import parse_ts

from . import collectors, mcp_client, store

logger = logging.getLogger(__name__)

#: The hard off switch.
ENV_ENABLED = "XO_CONNECTIONS_POLL_ENABLED"
ENV_TICK = "XO_CONNECTIONS_POLL_TICK_S"
DEFAULT_TICK_S = 30.0
MIN_TICK_S = 5.0
_STARTUP_DELAY_S = 5.0
#: Read timeout for one tool call; the wait_for cap adds :data:`_GRACE_S`.
CALL_TIMEOUT_S = 60.0
_GRACE_S = 15.0
#: One ``tools/list`` per poll, so a collector knows whether its slug is exposed
#: directly or must run through the session's executor tool.
LIST_TIMEOUT_S = 30.0
#: A forced poll ("poll now") waits this long for the loop to release the lock
#: before answering "busy".
FORCE_WAIT_S = 25.0

NOT_SIGNED_IN = "not signed in to XO (no account id)"
NO_TOOLKITS = "no toolkits are turned on in this workspace"

#: ``poll_connection(user_id=...)`` default: resolve the identity here.
_UNRESOLVED = object()


# ── Configuration ────────────────────────────────────────────────────────────


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _number(name: str, default: float, *, minimum: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def poller_enabled() -> bool:
    return _flag(ENV_ENABLED, True)


def tick_seconds() -> float:
    return _number(ENV_TICK, DEFAULT_TICK_S, minimum=MIN_TICK_S)


# ── Per-toolkit locks ────────────────────────────────────────────────────────

_locks: dict[str, asyncio.Lock] = {}


def _lock(toolkit: str) -> asyncio.Lock:
    lock = _locks.get(toolkit)
    if lock is None:
        lock = asyncio.Lock()
        _locks[toolkit] = lock
    return lock


def reset_for_tests() -> None:
    """Forget every per-toolkit lock (they bind to the loop that used them)."""
    _locks.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _outcome(toolkit: str, *, polled: bool = False, new_events: int = 0,
             error: Optional[str] = None, skipped: Optional[str] = None) -> dict:
    return {"toolkit": toolkit, "polled": polled, "new_events": new_events, "error": error, "skipped": skipped}


def _due(config: dict, state_doc: dict, now: datetime) -> bool:
    """Never polled, an unparsable or future ``last_poll_at`` (hand edit,
    clock change) and an elapsed interval all count as due."""
    last = parse_ts(state_doc.get("last_poll_at"))
    if last is None:
        return True
    if last > now:
        logger.debug("connections poller: last_poll_at is in the future; polling now")
        return True
    return (now - last).total_seconds() >= float(config.get("interval_s") or store.INTERVAL_DEFAULT)


def _read_config(toolkit: str) -> Optional[dict]:
    """A config that cannot be read is a skip, not a crash."""
    try:
        return store.read_config(toolkit)
    except store.ConnectionsError:
        return None
    except Exception:
        logger.warning("connections poller: %s config.json unreadable", toolkit, exc_info=True)
        return None


def _ready(toolkit: str, now: datetime) -> bool:
    """Configured, enabled and due: the cheap pre-check the tick uses."""
    config = _read_config(toolkit)
    return config is not None and config["enabled"] and _due(config, store.read_state(toolkit), now)


async def resolve_user_id() -> Optional[str]:
    """The account id Composio knows this workspace by, or ``None``. The
    cached id first (no network), then one identity round trip."""
    try:
        known = state.account_id_if_known()
        if known:
            return known
        return (await state.aaccount_id()) or None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("connections poller: identity unavailable (%s)", type(exc).__name__)
        return None


def _fail(toolkit: str, message: str, now_text: str) -> dict:
    """A poll that could not run its collectors: stamp ``last_poll_at`` and
    the error, leave ``last_ok_at`` alone, never touch ``events.jsonl``."""
    message = message[:store.ERROR_MAX]
    store.update_state(toolkit, last_poll_at=now_text, last_error=message)
    logger.info("connections poller: %s: %s", toolkit, message)
    return _outcome(toolkit, error=message)


async def _run_collector(toolkit: str, spec: dict, entry: dict, state_doc: dict, now: datetime,
                         tool_names: list[str]) -> int:
    """One collector: call (directly, or through the session's executor when
    the slug is not listed), unwrap, map, dedupe, append, remember. Returns
    the number of events appended. Raises on any failure."""
    args = collectors.render_args(spec, now)
    result = await asyncio.wait_for(
        mcp_client.execute_tool(entry, spec["tool"], args, tool_names=tool_names, timeout_s=CALL_TIMEOUT_S),
        CALL_TIMEOUT_S + _GRACE_S,
    )
    payload = mcp_client.tool_result_json(result)
    if isinstance(payload, dict) and payload.get("successful") is False:
        # Composio's envelope: isError false with successful false is a silent failure.
        raise mcp_client.McpError(str(payload.get("error") or "tool reported failure")[:store.ERROR_MAX])
    items = collectors.extract_items(spec, payload, toolkit=toolkit, now=now)
    seen = set(state_doc["cursors"].get(spec["id"], {}).get("seen", []))
    fresh: list[dict] = []
    for item in items:
        if item["key"] in seen:
            continue
        seen.add(item["key"])          # also dedupes within the batch
        fresh.append(item)
    appended = store.append_events(toolkit, fresh) if fresh else 0
    store.remember_seen(state_doc, spec["id"], [item["key"] for item in fresh])
    logger.debug("connections poller: %s/%s: %d item(s), %d new", toolkit, spec["id"], len(items), appended)
    return appended


def _session_gone(exc: BaseException) -> bool:
    """Composio answers ``initialize`` with HTTP 404 ("Tool router session
    ... not found") once the session behind the cached MCP url has been
    deleted or expired upstream. The swarm still updates its own record for
    that id, so nothing else ever notices; the poller has to."""
    text = str(exc)
    return "initialize failed: HTTP 404" in text


async def _list_tools_healing(user_id: str, entry: dict) -> tuple[dict, list[str]]:
    """``tools/list`` for ``entry``; on a dead session, invalidate it, mint a
    fresh entry and try exactly once more. Returns the entry the names
    belong to, so the collectors run against the live session."""
    try:
        names = await asyncio.wait_for(mcp_client.list_tools(entry, timeout_s=LIST_TIMEOUT_S),
                                       LIST_TIMEOUT_S + _GRACE_S)
        return entry, names
    except mcp_client.McpError as exc:
        if not _session_gone(exc):
            raise
        logger.info("connections poller: the Composio session is gone upstream; minting a fresh one")
    await asyncio.to_thread(composio_service.invalidate_session)
    entry = await asyncio.to_thread(composio_service.build_mcp_server_entry, user_id)
    names = await asyncio.wait_for(mcp_client.list_tools(entry, timeout_s=LIST_TIMEOUT_S),
                                   LIST_TIMEOUT_S + _GRACE_S)
    return entry, names


# ── One connection ───────────────────────────────────────────────────────────


async def poll_connection(toolkit: str, *, force: bool = False, user_id=_UNRESOLVED) -> dict:
    """Poll one connection. ``force`` ignores ``enabled`` and the interval
    (the route's "poll now"). ``user_id`` lets the tick resolve the identity
    once and share it. Returns ``{"toolkit", "polled", "new_events",
    "error", "skipped"}``; never raises for a connection-level failure."""
    config = _read_config(toolkit)
    if config is None:
        return _outcome(toolkit, skipped="not_configured")
    if not config["enabled"] and not force:
        return _outcome(toolkit, skipped="disabled")
    if not force and not _due(config, store.read_state(toolkit), datetime.now(timezone.utc)):
        return _outcome(toolkit, skipped="not_due")
    lock = _lock(toolkit)
    if lock.locked():
        if not force:
            return _outcome(toolkit, skipped="busy")
        # "Poll now" while the loop's own tick holds the lock: wait a little
        # rather than bounce, so the button answers with a real outcome.
        try:
            await asyncio.wait_for(lock.acquire(), FORCE_WAIT_S)
        except asyncio.TimeoutError:
            return _outcome(toolkit, skipped="busy")
    else:
        await lock.acquire()
    try:
        return await _poll_locked(toolkit, user_id)
    finally:
        lock.release()


async def _poll_locked(toolkit: str, user_id) -> dict:
    """The body of :func:`poll_connection`, run with the toolkit's lock held."""
    config = _read_config(toolkit)      # re-read: a DELETE meanwhile must not be undone
    if config is None:
        return _outcome(toolkit, skipped="not_configured")
    now = datetime.now(timezone.utc)
    now_text = collectors.iso(now)
    if user_id is _UNRESOLVED:
        user_id = await resolve_user_id()
    if not user_id:
        return _fail(toolkit, NOT_SIGNED_IN, now_text)
    try:
        enabled_here = toolkit in workspace_scope.enabled_toolkits()
    except Exception:
        logger.warning("connections poller: workspace scope unreadable", exc_info=True)
        enabled_here = False
    if not enabled_here:
        return _fail(toolkit, f"{toolkit} is not turned on in this workspace", now_text)
    try:
        entry = await asyncio.to_thread(composio_service.build_mcp_server_entry, user_id)
    except composio_service.NoToolkitsEnabled:
        return _fail(toolkit, NO_TOOLKITS, now_text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _fail(toolkit, f"session unavailable: {str(exc)[:250]}", now_text)

    try:
        entry, tool_names = await _list_tools_healing(user_id, entry)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _fail(toolkit, f"tools/list failed: {str(exc)[:220] or type(exc).__name__}", now_text)

    state_doc = store.read_state(toolkit)
    errors: list[str] = []
    added = 0
    for collector_id in config["collectors"]:
        spec = collectors.collector(toolkit, collector_id)
        if spec is None:
            continue
        try:
            added += await _run_collector(toolkit, spec, entry, state_doc, now, tool_names)
        except asyncio.TimeoutError:
            errors.append(f"{collector_id}: timed out after {CALL_TIMEOUT_S + _GRACE_S:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            text = str(exc)[:200] or type(exc).__name__
            errors.append(f"{collector_id}: {text}")
            logger.warning("connections poller: %s/%s failed: %s", toolkit, collector_id, text)
    last_error = "; ".join(errors)[:store.ERROR_MAX] or None
    fields = dict(last_poll_at=now_text, last_error=last_error, cursors=state_doc["cursors"],
                  events_total=state_doc["events_total"] + added)
    if not errors:
        fields["last_ok_at"] = now_text
    store.update_state(toolkit, **fields)
    return _outcome(toolkit, polled=True, new_events=added, error=last_error)


# ── One tick ─────────────────────────────────────────────────────────────────


async def poll_once() -> dict:
    """Poll every due connection once. The identity is resolved once per
    tick, and only when something is due. Never raises."""
    summary = {"configured": 0, "polled": 0, "skipped": 0, "errors": 0}
    try:
        toolkits = store.list_configured()
    except Exception:
        logger.warning("connections poller: could not list connections", exc_info=True)
        return summary
    summary["configured"] = len(toolkits)
    if not toolkits:
        return summary
    now = datetime.now(timezone.utc)
    user_id = await resolve_user_id() if any(_ready(tk, now) for tk in toolkits) else None
    for toolkit in toolkits:
        try:
            outcome = await poll_connection(toolkit, user_id=user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("connections poller: %s failed unexpectedly", toolkit, exc_info=True)
            summary["errors"] += 1
            continue
        if outcome["skipped"]:
            summary["skipped"] += 1
        elif outcome["polled"]:
            summary["polled"] += 1
        if outcome["error"]:
            summary["errors"] += 1
    return summary


# ── The loop ─────────────────────────────────────────────────────────────────


async def start_connections_poller() -> None:
    """Entry point for the background task (mirrors the GitHub poller)."""
    if not poller_enabled():
        logger.info("connections poller: disabled by %s", ENV_ENABLED)
        return
    await asyncio.sleep(_STARTUP_DELAY_S)
    logger.info("connections poller: started (%.0fs tick)", tick_seconds())
    while True:
        try:
            summary = await poll_once()
            if summary["polled"] or summary["errors"]:
                logger.debug("connections poller: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("connections poller: tick failed (non-fatal)", exc_info=True)
        await asyncio.sleep(tick_seconds())
