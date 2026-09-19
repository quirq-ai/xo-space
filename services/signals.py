"""The in-process signal bus between modules.

A module declares the signals it raises in ``events.SIGNALS`` and raises
one with ``await signals.notify("connections.new_events", toolkit=...)``.
Every enabled listener another module registered in its ``listeners.py``
(``LISTENERS = {"connections.new_events": fn}``) runs in turn: awaited when
it is a coroutine function, called otherwise; a listener that fails is
logged and skipped (the data is on disk; its next own read picks it up);
cancellation propagates. A signal is ``<module>.<name>``; the registry
refuses a listener on a signal no module declares.

Nothing here imports the registry at load time, so a module's ``service``
can import this module freely.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Extra listeners registered at runtime (tests, ad hoc hooks), beside the
#: ones the registry discovers. ``(module, fn)`` pairs per signal.
_extra: dict[str, list[tuple[str, Callable[..., Any]]]] = {}


def reset_for_tests() -> None:
    """Drop every runtime listener, the ones modules registered at import
    included; a test that calls this re-registers what it needs."""
    _extra.clear()


def on(name: str, fn: Callable[..., Any], *, module: str = "runtime") -> None:
    """Register a listener outside the module contract (the registry's
    ``listeners.py`` files need no call here)."""
    pairs = _extra.setdefault(name, [])
    if not any(existing is fn for _m, existing in pairs):
        pairs.append((module, fn))


def off(name: str, fn: Callable[..., Any]) -> None:
    pairs = _extra.get(name) or []
    _extra[name] = [(m, f) for m, f in pairs if f is not fn]


def listeners_for(name: str) -> list[tuple[str, Callable[..., Any]]]:
    from services import modules as registry

    pairs: list[tuple[str, Callable[..., Any]]] = []
    for module, fn in registry.listeners().get(name, []):
        if registry.enabled(module, "listeners"):
            pairs.append((module, fn))
    pairs.extend(_extra.get(name) or [])
    return pairs


async def notify(name: str, **payload: Any) -> int:
    """Run every enabled listener for ``name``. Returns how many ran."""
    ran = 0
    for module, fn in listeners_for(name):
        try:
            result = fn(**payload)
            if inspect.isawaitable(result):
                await result
            ran += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("signals: listener %s (%s) failed on %s",
                           getattr(fn, "__qualname__", repr(fn)), module, name, exc_info=True)
    return ran


def notify_soon(name: str, **payload: Any) -> Optional["asyncio.Task[int]"]:
    """Schedule :func:`notify` on the running loop (for a synchronous caller
    inside the server); with no running loop the listeners run now."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(notify(name, **payload))
        return None
    return loop.create_task(notify(name, **payload))
