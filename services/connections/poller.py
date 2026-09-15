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
the store is the cross-process guard. Each poll opens one MCP session
(``mcp_client.McpSession``: the handshake once, closed when the poll ends)
and lists its tools once: a slug that is exposed is called directly,
anything else runs through Composio's executor tool (see
``McpSession.execute_tool``). A session that is gone upstream is replaced
exactly once per poll; one that dies mid-poll (a collector's ``tools/call``
answers HTTP 404) ends the collector loop, the collectors after it are
recorded as not attempted, and the next poll's handshake replaces it.

The account a toolkit's session is bound to is resolved on the same
session, before the collectors, whenever the cached label is stale
(:func:`account_is_fresh`: older than :data:`ACCOUNT_TTL_S`, or resolved
under a different pinned account id) and remembered in ``accounts.json``
(:func:`store.remember_account`). The lookup is best-effort: a failure is
logged, never joins ``last_error`` and never blocks the collectors.
:func:`refresh_account` is the route's "resolve it now": the same
identity, scope and session steps as a poll (:func:`_open_session_for`)
on a session of its own, answered in the route's shape and throttled to
one provider call per :data:`ACCOUNT_MIN_REFRESH_S`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.connectors.composio import state, space_scope
from services.periodic import run_forever
from services.timestamps import aware, parse_ts

from . import collectors, mcp_client, store

logger = logging.getLogger(__name__)

#: The hard off switch.
ENV_ENABLED = "XO_CONNECTIONS_POLL_ENABLED"
ENV_TICK = "XO_CONNECTIONS_POLL_TICK_S"
DEFAULT_TICK_S = 30.0
MIN_TICK_S = 5.0
_STARTUP_DELAY_S = 5.0
#: The session's httpx read timeout (wide enough for a tool call, so one
#: client serves the whole poll); the per-call wait_for cap adds :data:`_GRACE_S`.
CALL_TIMEOUT_S = 60.0
_GRACE_S = 15.0
#: One ``tools/list`` per poll, so a collector knows whether its slug is exposed
#: directly or must run through the session's executor tool. The handshake and
#: the listing share this wait_for cap (plus :data:`_GRACE_S`).
LIST_TIMEOUT_S = 30.0
#: A forced poll ("poll now") waits this long for the loop to release the lock
#: before answering "busy".
FORCE_WAIT_S = 25.0
#: A cached account label is looked up again after this long (a poll does it
#: on its own session), and :func:`refresh_account` answers from the cache
#: within :data:`ACCOUNT_MIN_REFRESH_S` of the last check (Google's quota is
#: per minute).
ACCOUNT_TTL_S = 86400
ACCOUNT_MIN_REFRESH_S = 60

NOT_SIGNED_IN = "not signed in to XO (no account id)"
NO_TOOLKITS = "no toolkits are turned on in this workspace"
#: Recorded for every collector after the one whose ``tools/call`` found the session gone.
SESSION_LOST = "not attempted, the MCP session died mid-poll"

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


_NO_ACTIVE_CONNECTION = "No active connection found for toolkit"
_SESSION_RESTRICTION = "[Session Restriction]"
_RATE_LIMIT_MARKS = ("Quota exceeded", "rateLimitExceeded", "userRateLimitExceeded", "Rate limit", "429")


def humanize_error(toolkit: str, text: str) -> str:
    """Composio's tool-router texts are written for a model ("call
    COMPOSIO_MANAGE_CONNECTIONS ..."); the ones a poll meets most are
    reworded for ``last_error``, the card and the Inbox. Anything else
    passes through unchanged."""
    if _NO_ACTIVE_CONNECTION in text:
        return (f"{toolkit} is no longer connected on Composio (the sign-in expired or was revoked): "
                f"reconnect it from the Connectors tab")
    if _SESSION_RESTRICTION in text:
        return f"{toolkit} is turned off for this workspace's Composio session: turn it on from the Connectors tab"
    if any(mark in text for mark in _RATE_LIMIT_MARKS):
        return "the provider's rate limit was hit (queries per minute); the next poll retries"
    return text


def _error_text(exc: BaseException) -> str:
    """One line for a failed call: the timeout spelled out, otherwise the
    message (capped) or the exception's type."""
    if isinstance(exc, asyncio.TimeoutError):
        return f"timed out after {CALL_TIMEOUT_S + _GRACE_S:.0f}s"
    return str(exc)[:300] or type(exc).__name__


def _fail(toolkit: str, message: str, now_text: str) -> dict:
    """A poll that could not run its collectors: stamp ``last_poll_at`` and
    the error, leave ``last_ok_at`` alone, never touch ``events.jsonl``."""
    message = message[:store.ERROR_MAX]
    store.update_state(toolkit, last_poll_at=now_text, last_error=message)
    logger.info("connections poller: %s: %s", toolkit, message)
    return _outcome(toolkit, error=message)


async def _run_collector(toolkit: str, spec: dict, session: mcp_client.McpSession, state_doc: dict,
                         now: datetime, tool_names: list[str]) -> tuple[int, list[str]]:
    """One collector over the poll's open ``session``: call (directly, or
    through the session's executor when the slug is not listed), unwrap,
    map, dedupe, append, remember. Returns the number of events appended
    and the warnings the tool reported inside its successful envelope (the
    spec's ``warn_keys``, for example one calendar out of three failing);
    those join ``last_error`` while the events that did arrive are kept.
    Raises on any failure; Composio's envelope with ``successful`` false
    (``isError`` false, so the session raised nothing) is an
    :class:`mcp_client.McpError` with stage ``execute`` and no status."""
    args = collectors.render_args(spec, now)
    result = await asyncio.wait_for(
        session.execute_tool(spec["tool"], args, tool_names=tool_names),
        CALL_TIMEOUT_S + _GRACE_S,
    )
    payload = mcp_client.tool_result_json(result)
    if isinstance(payload, dict) and payload.get("successful") is False:
        # Composio's envelope: isError false with successful false is a silent failure.
        raise mcp_client.McpError((mcp_client.error_text(payload.get("error")) or "tool reported failure")[:store.ERROR_MAX],
                                  stage="execute")
    items = collectors.extract_items(spec, payload, toolkit=toolkit, now=now)
    warnings = collectors.extract_warnings(spec, payload)
    if isinstance(payload, dict) and payload.get("truncated"):
        warnings.append("Composio returned only a preview of a large answer; the newest items were read, "
                        "the rest were not")
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
    for warning in warnings:
        logger.warning("connections poller: %s/%s: %s", toolkit, spec["id"], warning)
    return appended, warnings


def _session_gone(exc: BaseException) -> bool:
    """Composio answers ``initialize`` with HTTP 404 ("Tool router session
    ... not found") once the session behind the cached MCP url has been
    deleted or expired upstream. The swarm still updates its own record for
    that id, so nothing else ever notices; the poller has to. Read from the
    error's ``stage`` and ``status``, never from its text."""
    return isinstance(exc, mcp_client.McpError) and exc.stage == "initialize" and exc.status == 404


def _session_lost(exc: BaseException) -> bool:
    """A session that died mid-poll: a collector's ``tools/call`` answered
    HTTP 404, which streamable HTTP reserves for a request on a session the
    server has terminated. Nothing later in the poll can succeed on it, so
    the collector loop stops; the next poll's handshake replaces it (through
    :func:`_session_gone` when Composio's tool-router session is what died).
    Read from ``stage`` and ``status``, never from the text."""
    return isinstance(exc, mcp_client.McpError) and exc.stage == "tools/call" and exc.status == 404


async def _open_and_list(entry: dict) -> tuple[mcp_client.McpSession, list[str]]:
    """Open one session on ``entry`` and list its tools, both under the
    listing cap. The session comes back open (the caller closes it); it is
    closed here when the handshake or the listing fails, and a close that
    fails itself is logged so the handshake or listing error is what
    propagates (the dead-session check reads it)."""
    session = mcp_client.McpSession(entry, timeout_s=CALL_TIMEOUT_S)

    async def handshake_and_list() -> list[str]:
        await session.open()
        return await session.list_tools()

    try:
        names = await asyncio.wait_for(handshake_and_list(), LIST_TIMEOUT_S + _GRACE_S)
    except BaseException:
        try:
            await session.close()
        except Exception as exc:
            logger.debug("connections poller: closing a failed session raised %s", type(exc).__name__)
        raise
    return session, names


async def _list_tools_healing(user_id: str, entry: dict) -> tuple[mcp_client.McpSession, list[str]]:
    """Open the poll's session on ``entry`` and list its tools; on a dead
    session, invalidate it, mint a fresh entry and open a new session
    exactly once more. Returns the open session the names belong to, so
    every collector runs against the live one; the caller closes it."""
    try:
        return await _open_and_list(entry)
    except mcp_client.McpError as exc:
        if not _session_gone(exc):
            raise
        logger.info("connections poller: the Composio session is gone upstream; minting a fresh one")
    await asyncio.to_thread(composio_service.invalidate_session)
    entry = await asyncio.to_thread(composio_service.build_mcp_server_entry, user_id)
    return await _open_and_list(entry)


class _SessionUnavailable(Exception):
    """Why no session could be opened for a toolkit; its text is what a poll
    records as ``last_error`` and what :func:`refresh_account` answers."""


async def _open_session_for(toolkit: str, user_id) -> tuple[mcp_client.McpSession, list[str]]:
    """The steps a poll and an account refresh share: resolve the identity
    (unless ``user_id`` is given), check the toolkit is turned on here, mint
    the entry, open the session with :func:`_list_tools_healing`. Raises
    :class:`_SessionUnavailable` with the recorded text at each step; the
    session comes back open and the caller closes it."""
    if user_id is _UNRESOLVED:
        user_id = await resolve_user_id()
    if not user_id:
        raise _SessionUnavailable(NOT_SIGNED_IN)
    try:
        enabled_here = toolkit in space_scope.enabled_toolkits()
    except Exception:
        logger.warning("connections poller: workspace scope unreadable", exc_info=True)
        enabled_here = False
    if not enabled_here:
        raise _SessionUnavailable(f"{toolkit} is not turned on in this workspace")
    try:
        entry = await asyncio.to_thread(composio_service.build_mcp_server_entry, user_id)
    except composio_service.NoToolkitsEnabled:
        raise _SessionUnavailable(NO_TOOLKITS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _SessionUnavailable(f"session unavailable: {str(exc)[:250]}")
    try:
        return await _list_tools_healing(user_id, entry)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _SessionUnavailable(f"tools/list failed: {str(exc)[:220] or type(exc).__name__}")


# ── The connected account ────────────────────────────────────────────────────


def pinned_account_id(toolkit: str) -> Optional[str]:
    """The first connected account id this workspace pins for ``toolkit``
    (the one the session is bound to), or ``None`` when nothing is pinned
    or the scope cannot be read."""
    try:
        ids = space_scope.load().get(toolkit, {}).get("connected_account_ids") or []
    except Exception as exc:
        logger.debug("connections poller: workspace scope unreadable for %s (%s)", toolkit, type(exc).__name__)
        return None
    return ids[0] if ids and isinstance(ids[0], str) else None


def account_is_fresh(toolkit: str, now: datetime) -> bool:
    """Cached, resolved under the account pinned right now (nothing pinned
    then and now counts as the same), and checked within
    :data:`ACCOUNT_TTL_S` of ``now``."""
    cached = store.read_accounts().get(toolkit)
    if cached is None or cached.get("connected_account_id") != pinned_account_id(toolkit):
        return False
    checked = parse_ts(cached.get("checked_at"))
    return checked is not None and 0 <= (aware(now) - checked).total_seconds() < ACCOUNT_TTL_S


async def _lookup_account(toolkit: str, spec: dict, session: mcp_client.McpSession, tool_names: list[str],
                          now: datetime) -> str:
    """One identity call over ``session`` (directly or through the executor,
    like a collector), the label read out of the envelope and remembered
    under the pinned account id. Raises on any failure, including an
    answer without a label."""
    result = await asyncio.wait_for(
        session.execute_tool(spec["tool"], dict(spec["args"]), tool_names=tool_names),
        CALL_TIMEOUT_S + _GRACE_S,
    )
    payload = mcp_client.tool_result_json(result)
    if isinstance(payload, dict) and payload.get("successful") is False:
        raise mcp_client.McpError((mcp_client.error_text(payload.get("error")) or "tool reported failure")[:store.ERROR_MAX],
                                  stage="execute")
    label = collectors.extract_identity(spec, payload)
    if label is None:
        raise mcp_client.McpError(f"{spec['tool']} answered without an account label", stage="execute")
    store.remember_account(toolkit, label, pinned_account_id(toolkit), now=now)
    return label


async def resolve_account(toolkit: str, session: mcp_client.McpSession, tool_names: list[str],
                          now: datetime) -> Optional[str]:
    """Resolve and remember the account label on the poll's open session.
    ``None`` without a call for a toolkit with no identity spec; ``None``
    and a WARN on any failure (never raises; cancellation propagates)."""
    spec = collectors.identity_spec(toolkit)
    if spec is None:
        return None
    try:
        return await _lookup_account(toolkit, spec, session, tool_names, now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("connections poller: %s: account lookup failed: %s", toolkit,
                       humanize_error(toolkit, _error_text(exc)))
        return None


async def refresh_account(toolkit: str, *, force: bool = False) -> dict:
    """Resolve the account label now, on a session of its own, and answer
    ``{"toolkit", "account_label", "account_checked_at", "error",
    "cached"}``. A toolkit without an identity spec answers with the error
    "no account lookup for <toolkit> yet"; a check within
    :data:`ACCOUNT_MIN_REFRESH_S` answers the cached values with ``cached``
    true unless ``force``; every failure keeps the cached label and sets
    ``error`` (reworded by :func:`humanize_error`). Never raises for a
    provider or session failure; an unknown toolkit is the service's 404."""
    cached = store.read_accounts().get(toolkit) or {}
    answer = {"toolkit": toolkit, "account_label": cached.get("label"),
              "account_checked_at": cached.get("checked_at"), "error": None, "cached": False}
    spec = collectors.identity_spec(toolkit)
    if spec is None:
        return {**answer, "error": f"no account lookup for {toolkit} yet"}
    now = datetime.now(timezone.utc)
    checked = parse_ts(cached.get("checked_at"))
    if not force and checked is not None and 0 <= (now - checked).total_seconds() < ACCOUNT_MIN_REFRESH_S:
        return {**answer, "cached": True}
    try:
        session, tool_names = await _open_session_for(toolkit, _UNRESOLVED)
    except _SessionUnavailable as exc:
        return {**answer, "error": humanize_error(toolkit, str(exc))}
    try:
        label = await _lookup_account(toolkit, spec, session, tool_names, now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        text = humanize_error(toolkit, _error_text(exc))
        logger.warning("connections poller: %s: account lookup failed: %s", toolkit, text)
        return {**answer, "error": text}
    finally:
        await session.close()
    return {**answer, "account_label": label, "account_checked_at": collectors.iso(now)}


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
    """The body of :func:`poll_connection`, run with the toolkit's lock held.
    Once the session is open and listed, a stale account label is resolved
    first (:func:`resolve_account`, best-effort). Collectors then run in
    config order over the one session; a collector whose ``tools/call``
    finds the session gone (:func:`_session_lost`) ends the loop, and the
    collectors after it are recorded as :data:`SESSION_LOST`."""
    config = _read_config(toolkit)      # re-read: a DELETE meanwhile must not be undone
    if config is None:
        return _outcome(toolkit, skipped="not_configured")
    now = datetime.now(timezone.utc)
    now_text = collectors.iso(now)
    try:
        session, tool_names = await _open_session_for(toolkit, user_id)
    except _SessionUnavailable as exc:
        return _fail(toolkit, str(exc), now_text)

    state_doc = store.read_state(toolkit)
    errors: list[str] = []
    added = 0
    try:
        if not account_is_fresh(toolkit, now):
            await resolve_account(toolkit, session, tool_names, now)
        specs = [(cid, collectors.collector(toolkit, cid)) for cid in config["collectors"]]
        specs = [(cid, spec) for cid, spec in specs if spec is not None]     # unknown ids are skipped
        for index, (collector_id, spec) in enumerate(specs):
            try:
                appended, warnings = await _run_collector(toolkit, spec, session, state_doc, now, tool_names)
                added += appended
                errors.extend(f"{collector_id}: {warning}" for warning in warnings)
            except asyncio.TimeoutError:
                errors.append(f"{collector_id}: timed out after {CALL_TIMEOUT_S + _GRACE_S:.0f}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                text = humanize_error(toolkit, str(exc)[:300] or type(exc).__name__)[:200]
                errors.append(f"{collector_id}: {text}")
                logger.warning("connections poller: %s/%s failed: %s", toolkit, collector_id, text)
                if _session_lost(exc):
                    rest = [cid for cid, _ in specs[index + 1:]]
                    errors.extend(f"{cid}: {SESSION_LOST}" for cid in rest)
                    logger.info("connections poller: %s: the MCP session died mid-poll; %d collector(s) not attempted",
                                toolkit, len(rest))
                    break
    finally:
        await session.close()
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


async def _tick() -> None:
    """One pass of the loop: poll what is due, note a tick that did something."""
    summary = await poll_once()
    if summary["polled"] or summary["errors"]:
        logger.debug("connections poller: %s", summary)


async def start_connections_poller() -> None:
    """Entry point for the background task (mirrors the GitHub poller). The
    enabled check and the startup delay stay here because their log lines
    are this poller's; the loop itself (tick, log a failure and go on,
    sleep ``tick_seconds()`` re-read each pass, stop on cancel) is
    :func:`services.periodic.run_forever`."""
    if not poller_enabled():
        logger.info("connections poller: disabled by %s", ENV_ENABLED)
        return
    await asyncio.sleep(_STARTUP_DELAY_S)
    logger.info("connections poller: started (%.0fs tick)", tick_seconds())
    await run_forever("connections poller", _tick, interval_s=tick_seconds, logger=logger)
