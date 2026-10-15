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

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import (
    AlreadyDecided, Ask, CallRef, Clock, Decision, Engine, Listener, Wake, Writes,
)
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.store import StoredEvent

__all__ = ["AlreadyDecided", "Ask", "Asked", "CallRef", "Clock", "Decision", "Engine", "Listener", "Resolved", "Wake", "Writes",
           "clock", "current", "deliver_call", "deliver_usage", "desk", "iso", "pause", "permission_step",
           "permission_steps", "read", "stop", "subscribe", "write"]


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


async def deliver_usage(engine: Engine, pool_id: str, *, continue_until: str | None,
                        record: Callable[[Writes], None]) -> None:
    await engine.deliver_usage_decision(pool_id, continue_until=continue_until, record=record)


async def pause(engine: Engine, session_id: str, *, why: str, by: str) -> None:
    """Pause the session (no new turns until the person resumes it; its
    ``session`` event tells the person, once) and stop what it is running, so
    a loop of asks ends now rather than at its turn's end."""
    await engine.pause(session_id, reason=why)
    await engine.stop(session_id, by=by)


async def stop(engine: Engine, session_id: str, *, by: str) -> None:
    await engine.stop(session_id, by=by)


@dataclass(frozen=True)
class Asked:
    """A CLI asked to run something (``step.permission``)."""

    session_id: str
    request_id: str
    action: Mapping[str, Any]          # kind, tool, command, paths, url, mcp


@dataclass(frozen=True)
class Resolved:
    """A permission request was answered (``step.permission_done``)."""

    session_id: str
    request_id: str
    allow: bool


_ACTION = ("kind", "tool", "command", "paths", "url", "mcp")


def permission_steps(events: Iterable[StoredEvent]) -> Iterator[Asked | Resolved]:
    """The root agents' permission steps, from the rows the engine stored."""
    for event in events:
        if event.session_id is None:
            continue
        if event.type == "step.permission":
            request = event.data.get("request") or {}
            yield Asked(event.session_id, str(request.get("request_id")), {k: request.get(k) for k in _ACTION})
        elif event.type == "step.permission_done":
            allow = bool((event.data.get("decision") or {}).get("allow"))
            yield Resolved(event.session_id, str(event.data.get("request_id")), allow)


def permission_step(event: c.Event) -> Asked | Resolved | None:
    """A sub-agent's permission step: the engine hands those over live, never
    storing them (ch 14 §2.1)."""
    if event.run is None:
        return None
    if isinstance(event, c.PermissionRequested):
        req = event.request
        return Asked(event.session, req.request_id, {"kind": req.kind, "tool": req.tool, "command": req.command,
                                                     "paths": list(req.paths), "url": req.url,
                                                     "mcp": list(req.mcp) if req.mcp else None})
    if isinstance(event, c.PermissionResolved):
        return Resolved(event.session, event.request_id, event.decision.allow)
    return None
