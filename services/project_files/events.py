"""Publishes file.version/attributed/rescan events to async subscribers, rate-limited per path; names no engine."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

VERSION = "file.version"
ATTRIBUTED = "file.version.attributed"
RESCAN = "file.rescan"
MIN_GAP_S = 0.25

#: ``(type, project_id, data)``.
Event = tuple[str, str, dict[str, Any]]
Subscriber = Callable[[list[Event]], Awaitable[None]]

_subscribers: list[Subscriber] = []
_loop: asyncio.AbstractEventLoop | None = None
_guard = threading.Lock()
#: Per (project, path): when its last version went out, and what waits.
_last_sent: dict[tuple[str, str], float] = {}
_waiting: dict[tuple[str, str], Event] = {}
_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
#: Events in the order they go out; one drain hands them on, so a
#: subscriber sees them in that order.
_queue: list[Event] = []
_drain: asyncio.Task | None = None


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def stop() -> None:
    """Send what still waits, then stop delivering."""
    global _loop
    if _loop is None:
        return
    for key in list(_waiting):
        _flush(key)
    for timer in _timers.values():
        timer.cancel()
    _timers.clear()
    while _drain is not None and not _drain.done():
        await asyncio.wait([_drain])
    _loop = None
    _last_sent.clear()


def subscribe(fn: Subscriber) -> None:
    with _guard:
        if fn not in _subscribers:
            _subscribers.append(fn)


def unsubscribe(fn: Subscriber) -> None:
    with _guard:
        if fn in _subscribers:
            _subscribers.remove(fn)


def publish(type_: str, project_id: str, data: dict[str, Any]) -> None:
    """Hand one event on (thread-safe); dropped when nothing listens, since ``GET /versions?since=`` catches up."""
    loop = _loop
    if loop is None or not _subscribers or loop.is_closed():
        return
    try:
        if _on_loop(loop):
            _accept((type_, project_id, data))
        else:
            loop.call_soon_threadsafe(_accept, (type_, project_id, data))
    except RuntimeError:                                  # the loop closed meanwhile
        pass


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def _accept(event: Event) -> None:
    type_, project_id, data = event
    if type_ == VERSION:
        key = (project_id, data.get("path") or "")
        last = _last_sent.get(key)
        now = time.monotonic()
        if last is None or now - last >= MIN_GAP_S:
            _waiting.pop(key, None)
            _last_sent[key] = now
            _deliver([event])
            return
        _waiting[key] = event                             # the newest of the burst
        if key not in _timers and _loop is not None:
            _timers[key] = _loop.call_later(MIN_GAP_S - (now - last), _flush, key)
        return
    if type_ == ATTRIBUTED:
        key = (project_id, data.get("path") or "")
        pending = _waiting.get(key)
        if pending is not None and pending[2].get("id") == data.get("id"):
            pending[2]["by"] = data.get("by")
            return
    _deliver([event])


def _flush(key: tuple[str, str]) -> None:
    _timers.pop(key, None)
    event = _waiting.pop(key, None)
    if event is not None:
        _last_sent[key] = time.monotonic()
        _deliver([event])
    if len(_last_sent) > 10_000:                           # forget paths long quiet
        horizon = time.monotonic() - 60
        for stale in [k for k, t in _last_sent.items() if t < horizon and k not in _waiting]:
            del _last_sent[stale]


def _deliver(batch: list[Event]) -> None:
    global _drain
    _queue.extend(batch)
    if _drain is None or _drain.done():
        _drain = asyncio.ensure_future(_drain_queue())


async def _drain_queue() -> None:
    while _queue:
        batch = _queue[:]
        del _queue[:]
        with _guard:
            subscribers = list(_subscribers)
        for fn in subscribers:
            await _safely(fn, batch)


async def _safely(fn: Subscriber, batch: list[Event]) -> None:
    try:
        await fn(batch)
    except Exception:  # noqa: BLE001 - one subscriber's failure must not stop the others
        log.exception("project files: a subscriber failed to take %d file events", len(batch))
