"""Storm's session engine (design ch 07): it runs every session end to end.

Boot, from the server's lifespan (ch 07 §9.3): take the engine lock, open the
store, run boot recovery, then start admitting. All of it under the state
root (``QUIRQ_STATE_ROOT`` when set), so a test or a scratch server never
touches ``~/.quirq``. If another server holds the lock, or the store can't be
used, this server runs no engine and ``current()`` raises the 503 the routes
answer with (``engine_elsewhere``, ``store_failed``, ``store_refused``).

The API the surface packages call is ``Engine``'s (``engine.py``); the seams
the stream and the review queue implement are in ``ports.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from services.cowork_agent.adapters.contract import Runtime
from services.cowork_agent.adapters.loader import list_capability_providers, try_load_capability
from services.cowork_agent.storm.engine.engine import Engine
from services.cowork_agent.storm.engine.model import (
    AlreadyDecided,
    Ask,
    CallRef,
    Decision,
    EngineUnavailable,
    SessionNotFound,
    SessionRefused,
    StopResult,
    Unsupported,
    Wake,
    WouldStopRuns,
)
from services.cowork_agent.storm.engine.ports import Clock, Listener, Preparer, ReviewDesk, Writes
from services.cowork_agent.storm.store import EngineElsewhere, EngineLock, Store, StoreFailed, StoreRefused, recover

log = logging.getLogger(__name__)

_engine: Engine | None = None
_unavailable: EngineUnavailable | None = None


def load_runtime(agent: str) -> Runtime | None:
    """The agent's runtime (capability ``runtime``), or None when its adapter
    has none."""
    try:
        module = try_load_capability("runtime", agent=agent)
    except ValueError:              # not an agent name at all
        return None
    return module.runtime() if module is not None else None


def runtime_providers() -> list[str]:
    """Every adapter that ships a storm runtime."""
    return list_capability_providers("runtime")


def current() -> Engine:
    """The running engine; raises ``EngineUnavailable`` (503) when this
    server runs none."""
    if _engine is not None:
        return _engine
    raise _unavailable or EngineUnavailable("engine_not_running", "Storm's engine isn't running in this server "
                                                                  "(it starts with the server); retry shortly.")


async def boot(**options: Any) -> Engine | None:
    """Start the engine. A lock held elsewhere or an unusable store is not
    raised: ``current()`` reports it. ``options`` go to ``Engine``: the review
    queue's ``desk``, the stream's ``listeners`` (registered before the
    first admission), the worktrees' ``preparers``, a test's clock, memory
    guard or runtime loader."""
    global _engine, _unavailable
    try:
        lock = EngineLock.acquire()
    except EngineElsewhere as exc:
        _unavailable = EngineUnavailable(exc.code, exc.message)
        log.warning("storm: %s", exc.message)
        return None
    try:
        store = await Store.open()
        try:
            report = await recover(store, lock)
        except BaseException:
            await store.close()
            raise
    except (StoreRefused, StoreFailed) as exc:
        lock.release()
        _unavailable = EngineUnavailable(exc.code, exc.message)
        log.error("storm: the engine can't use its store: %s", exc.message)
        return None
    except BaseException:
        lock.release()
        raise
    options.setdefault("runtimes", load_runtime)
    options.setdefault("providers", runtime_providers())
    options.setdefault("xo_ports", frozenset({int(os.getenv("PORT", "5002"))}))
    engine: Engine | None = None
    try:
        engine = Engine(store, lock, **options)
        await engine.start(report)
    except BaseException as exc:
        # This server runs no engine, so it must not keep the lock: another
        # server would be told the engine runs here.
        for task in engine.background_loops() if engine is not None else ():
            task.cancel()
        await store.close()
        lock.release()
        _unavailable = EngineUnavailable("engine_failed", f"Storm's engine failed to start ({exc}); see the "
                                                          "server log, then restart the server.")
        raise
    _engine, _unavailable = engine, None
    return engine


async def shutdown() -> None:
    global _engine
    engine, _engine = _engine, None
    if engine is not None:
        await engine.shutdown()


__all__ = [
    "AlreadyDecided", "Ask", "CallRef", "Clock", "Decision", "Engine", "EngineUnavailable", "Listener", "Preparer",
    "ReviewDesk",
    "SessionNotFound", "SessionRefused", "StopResult", "Unsupported", "Wake", "WouldStopRuns", "Writes", "boot",
    "current", "load_runtime", "runtime_providers", "shutdown",
]
