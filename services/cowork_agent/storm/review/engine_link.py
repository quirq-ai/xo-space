"""Everything the review queue asks of the engine, in one place (design
ch 07 §6; the seams of ``storm/engine/ports.py``).

The queue is the engine's ``ReviewDesk``: the engine calls ``open``,
``park`` and ``withdraw`` inside its own transactions. The other way round,
the queue delivers decisions, writes in the engine's transactions, pauses a
thread's session, stops a turn, and hears the permission steps the engine
records (the loop breaker counts the policy's denials from them). Each of
those calls is here, so the queue itself never reaches into the engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import (
    AlreadyDecided, Ask, CallRef, Clock, Decision, Engine, EngineUnavailable, Listener, Wake, Writes,
)
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.review.policy import request_sha256
from services.cowork_agent.storm.store import StoredEvent

log = logging.getLogger(__name__)

__all__ = [
    "AlreadyDecided", "Ask", "Asked", "CallRef", "Clock", "Decision", "Engine", "EngineUnavailable", "Listener",
    "Resolved", "Wake", "Writes", "boot", "clock", "current", "deliver_call", "deliver_usage", "desk", "iso", "pause",
    "permission_step", "permission_steps", "read", "shutdown", "subscribe", "write",
]


async def boot(**options: Any) -> Engine | None:
    return await storm_engine.boot(**options)


async def shutdown() -> None:
    await storm_engine.shutdown()


def current() -> Engine:
    """The running engine (503 ``engine_elsewhere`` when another server runs it)."""
    return storm_engine.current()


def desk(engine: Engine) -> Any:
    return engine.desk


def clock(engine: Engine) -> Clock:
    return engine.clock


def subscribe(engine: Engine, listener: Listener) -> None:
    """Every committed transaction's events, and the live ones, after the engine handled them."""
    engine.subscribe(listener)


async def read(engine: Engine, fn: Callable[[Any], Any]) -> Any:
    return await engine.store.read(fn)


async def write(engine: Engine, fn: Callable[[Writes], Any]) -> Any:
    """One engine transaction: the wakes it queues are admitted after the commit."""
    return await engine.write(fn)


async def deliver_call(engine: Engine, item_id: str, decision: Decision, call: CallRef,
                       record: Callable[[Writes, str], int]) -> str:
    """Into the held call, or as a grant and a wake once it is gone."""
    return await engine.deliver_decision(item_id, decision, call, record)


async def deliver_usage(engine: Engine, pool_id: str, *, keep_going: bool, record: Callable[[Writes], None]) -> None:
    """Continue (until the pool's reset as the store knows it in that
    transaction) or wait."""
    await engine.deliver_usage_decision(pool_id, keep_going=keep_going, record=record)


async def pause(engine: Engine, session_id: str, *, why: str, by: str) -> None:
    """Pause the session (no new turns until the person resumes it; its
    ``session`` event tells the person, once) and stop what it is running, so
    a loop of asks ends now rather than at its turn's end."""
    await engine.pause(session_id, reason=why)
    await engine.stop(session_id, by=by)


@dataclass(frozen=True)
class Asked:
    """A CLI asked to run something (``step.permission``)."""

    session_id: str
    request_id: str
    action_sha256: str                 # the action's canonical hash (the policy's ``request_sha256``)


@dataclass(frozen=True)
class Resolved:
    """A permission request was answered (``step.permission_done``)."""

    session_id: str
    request_id: str
    allow: bool


def permission_steps(events: Iterable[StoredEvent]) -> Iterator[Asked | Resolved]:
    """The root agents' permission steps, from the rows the engine stored. A
    step naming no request or hash is the engine's error: logged, skipped."""
    for event in events:
        if event.session_id is None or event.type not in ("step.permission", "step.permission_done"):
            continue
        asked = event.type == "step.permission"
        data = (event.data.get("request") or {}) if asked else event.data
        request_id, digest = data.get("request_id"), data.get("input_sha256")
        if not isinstance(request_id, str) or (asked and not isinstance(digest, str)):
            log.error("storm review: %s at seq %s names no request id or action hash; the loop breaker skips it",
                      event.type, event.seq)
            continue
        if asked:
            yield Asked(event.session_id, request_id, digest)
        else:
            yield Resolved(event.session_id, request_id, bool((event.data.get("decision") or {}).get("allow")))


def permission_step(event: c.Event) -> Asked | Resolved | None:
    """A sub-agent's permission step: the engine hands those over live, never
    storing them (ch 14 §2.1)."""
    if event.run is None:
        return None
    if isinstance(event, c.PermissionRequested):
        return Asked(event.session, event.request.request_id, request_sha256(event.request))
    if isinstance(event, c.PermissionResolved):
        return Resolved(event.session, event.request_id, event.decision.allow)
    return None
