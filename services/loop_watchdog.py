"""Name what blocks the event loop.

Everything the Space serves runs on one asyncio loop. A handler or a
background task that does blocking work on it (a large file walk, a
synchronous subprocess, a big JSON encode) freezes every request at once:
the UI's polls queue and then all complete together, and a clone's progress
looks stuck and then jumps. That is invisible in the access log, and ptrace
is usually locked down, so a profiler cannot attach to the live server.

The watchdog makes it visible. A task on the loop records a heartbeat every
:data:`BEAT_S`; a daemon thread checks it. When the loop has not beaten for
``QUIRQ_LOOP_STALL_S`` seconds (default 2; ``0`` turns the watchdog off) the
thread logs the loop thread's current stack, which is the code holding it,
once more if the stall goes on in different code, and the stall's length when
the loop comes back. Stacks carry file names, line numbers and source lines,
never local values. Cost while the loop is healthy: one tiny task step and
one thread wake-up per beat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("xo_space.loop_watchdog")

BEAT_S = 0.25
DEFAULT_THRESHOLD_S = 2.0
_STACK_FRAMES = 30
_KEPT = 20


@dataclass(frozen=True)
class Stall:
    """One stall, once the loop is back: when it began (epoch seconds), how
    long it lasted and the innermost frame first seen holding the loop."""

    started_at: float
    seconds: float
    where: str


def threshold_s() -> float:
    raw = (os.getenv("QUIRQ_LOOP_STALL_S") or "").strip()
    if not raw:
        return DEFAULT_THRESHOLD_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("QUIRQ_LOOP_STALL_S=%r is not a number; using %ss", raw, DEFAULT_THRESHOLD_S)
        return DEFAULT_THRESHOLD_S
    return max(0.0, value)


class LoopWatchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop, threshold: float, *, beat_s: float = BEAT_S) -> None:
        self._loop = loop
        self._threshold = threshold
        self._beat_s = beat_s
        self._last = time.monotonic()
        self._loop_thread = threading.get_ident()
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        #: The most recent stalls, newest last.
        self.stalls: deque[Stall] = deque(maxlen=_KEPT)

    def start(self) -> None:
        """Call from the loop's own thread, inside the running loop."""
        self._loop_thread = threading.get_ident()
        self._last = time.monotonic()
        self._task = self._loop.create_task(self._beat(), name="loop watchdog beat")
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2 * self._beat_s)

    async def _beat(self) -> None:
        while True:
            self._last = time.monotonic()
            await asyncio.sleep(self._beat_s)

    def _stack(self) -> tuple[str, str]:
        """``(innermost frame, formatted stack)`` of the loop thread now."""
        frame = sys._current_frames().get(self._loop_thread)
        if frame is None:
            return "unknown", ""
        stack = traceback.extract_stack(frame, limit=_STACK_FRAMES)
        where = f"{stack[-1].filename}:{stack[-1].lineno} in {stack[-1].name}" if stack else "unknown"
        return where, "".join(traceback.format_list(stack))

    def _watch(self) -> None:
        stalled_since: float | None = None
        first_where = ""
        reported: set[str] = set()
        while not self._stop.wait(self._beat_s):
            now = time.monotonic()
            lag = now - self._last
            if lag < self._threshold:
                if stalled_since is not None:
                    seconds = now - stalled_since
                    self.stalls.append(Stall(time.time() - seconds, seconds, first_where))
                    log.warning("event loop was blocked for %.1fs (first seen in %s)", seconds, first_where)
                    stalled_since, reported = None, set()
                continue
            if stalled_since is None:
                stalled_since = self._last
            where, stack = self._stack()
            if where in reported or len(reported) >= 3:
                continue
            if not reported:
                first_where = where
            reported.add(where)
            log.warning("event loop blocked for %.1fs so far; the loop thread is running:\n%s", lag, stack)


_current: LoopWatchdog | None = None


def start() -> LoopWatchdog | None:
    """Start the watchdog on the running loop, unless turned off. Never raises."""
    global _current
    threshold = threshold_s()
    if threshold <= 0 or _current is not None:
        return _current
    try:
        watchdog = LoopWatchdog(asyncio.get_running_loop(), threshold)
        watchdog.start()
    except Exception:  # noqa: BLE001 - a diagnostic never stops the server
        log.exception("loop watchdog did not start")
        return None
    _current = watchdog
    return watchdog


async def stop() -> None:
    global _current
    watchdog, _current = _current, None
    if watchdog is not None:
        await watchdog.stop()


def current() -> LoopWatchdog | None:
    return _current
