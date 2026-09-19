"""One supervisor for every background loop the modules declare.

A module's ``tasks.py`` lists ``TASKS = [Task("poller", start_poller)]``.
The supervisor starts every declared and enabled task when the server
starts, holds one ``asyncio.Task`` per item, reports one that dies on its
own, cancels or spawns on a switch flip (:meth:`reconcile`), and cancels
and awaits each on shutdown. The lifespan in ``server.py`` is the one
caller of :meth:`start` and :meth:`stop`; the registry calls
:meth:`reconcile` after a ``PUT /api/modules``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from services.timestamps import now_iso

logger = logging.getLogger("xo_space.supervisor")

Start = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class Task:
    """One background loop. ``start`` is the coroutine function that runs it
    until cancelled; ``enabled`` is an extra gate beside the module switch
    (an env flag kept for one release, a precondition); ``description`` is
    what the Modules page says about it."""

    name: str
    start: Start
    enabled: Optional[Callable[[], bool]] = None
    description: str = ""
    #: A task that runs once and returns (a seed, an install) rather than a
    #: loop: its clean exit is not reported as a death.
    oneshot: bool = False


@dataclass(frozen=True)
class TaskSpec:
    module: str
    task: Task

    @property
    def key(self) -> str:
        return f"{self.module}.{self.task.name}"


@dataclass
class _Running:
    spec: TaskSpec
    handle: "asyncio.Task[Any]"
    started_at: str = field(default_factory=now_iso)
    exited_at: Optional[str] = None
    error: Optional[str] = None


class Supervisor:
    def __init__(self) -> None:
        self._running: dict[str, _Running] = {}
        self._history: dict[str, _Running] = {}
        #: Set by :meth:`start`; until then :meth:`reconcile` changes nothing,
        #: so a switch flipped before the lifespan (or in a test) starts no loop.
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────

    def _wanted(self, spec: TaskSpec) -> bool:
        from services import modules as registry

        if not registry.enabled(spec.module, "tasks", spec.task.name):
            return False
        gate = spec.task.enabled
        try:
            return True if gate is None else bool(gate())
        except Exception:
            logger.warning("supervisor: enabled() of %s raised; treating as off", spec.key, exc_info=True)
            return False

    def spawn(self, spec: TaskSpec) -> bool:
        if spec.key in self._running:
            return False
        try:
            handle = asyncio.create_task(spec.task.start(), name=spec.key)
        except Exception as exc:
            logger.warning("supervisor: %s failed to start (non-fatal): %s", spec.key, exc)
            self._history[spec.key] = _Running(spec, handle=None, error=str(exc))  # type: ignore[arg-type]
            return False
        entry = _Running(spec, handle)
        self._running[spec.key] = entry
        handle.add_done_callback(lambda done, key=spec.key: self._on_done(key, done))
        logger.info("supervisor: %s started", spec.key)
        return True

    def _on_done(self, key: str, done: "asyncio.Task[Any]") -> None:
        entry = self._running.pop(key, None)
        if entry is None:
            return
        entry.exited_at = now_iso()
        if done.cancelled():
            entry.error = None
        else:
            exc = done.exception()
            if exc is not None:
                entry.error = f"{type(exc).__name__}: {exc}"
                logger.error("supervisor: %s died (non-fatal): %r", key, exc, exc_info=exc)
            elif entry.spec.task.oneshot:
                logger.info("supervisor: %s finished", key)
            else:
                logger.warning("supervisor: %s exited on its own; no further ticks will run", key)
        self._history[key] = entry

    async def stop_one(self, key: str) -> bool:
        entry = self._running.get(key)
        if entry is None:
            return False
        entry.handle.cancel()
        try:
            await entry.handle
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # reported by the done callback
        return True

    async def start(self, specs: list[TaskSpec]) -> list[str]:
        """Start every wanted task. Returns the keys started."""
        self._started = True
        started = []
        for spec in specs:
            if self._wanted(spec) and self.spawn(spec):
                started.append(spec.key)
        return started

    async def reconcile(self, specs: list[TaskSpec]) -> dict[str, list[str]]:
        """Cancel tasks that are no longer wanted, spawn the ones that are."""
        stopped, started = [], []
        if not self._started:
            return {"stopped": stopped, "started": started}
        wanted = {spec.key: spec for spec in specs if self._wanted(spec)}
        for key in list(self._running):
            if key not in wanted and await self.stop_one(key):
                stopped.append(key)
        for key, spec in wanted.items():
            if key not in self._running and self.spawn(spec):
                started.append(key)
        return {"stopped": stopped, "started": started}

    async def stop(self) -> None:
        for key in list(self._running):
            await self.stop_one(key)

    # ── status ───────────────────────────────────────────────────────────

    def running(self) -> list[str]:
        return sorted(self._running)

    def status(self, key: str) -> dict:
        entry = self._running.get(key) or self._history.get(key)
        if entry is None:
            return {"running": False, "started_at": None, "exited_at": None, "error": None}
        return {"running": key in self._running, "started_at": entry.started_at,
                "exited_at": entry.exited_at, "error": entry.error}

    def reset_for_tests(self) -> None:
        self._running.clear()
        self._history.clear()
        self._started = False


supervisor = Supervisor()
