"""One ``describe()`` per runtime at a time, shared by whoever asks.

The runtimes list, health and the space advisory each read a runtime's
``describe()`` with a bound (``health.CHECK_WAIT_S``), so one that hangs
doesn't hold the others. A bound that cancels the check, though, throws it
away: a runtime keeps a probe's answer only once its ``await`` returns, so
the next read starts the CLI again and is cut again. Here the check runs as
its own task: a caller that stops waiting leaves it running, its answer is
kept for the next read, and callers that arrive meanwhile share it. A
change through XO (an update or a sign-in) calls ``forget``: a check that
started before it is never shared with a caller after it.
"""

from __future__ import annotations

import asyncio
import logging

from services.cowork_agent.adapters import contract as c

log = logging.getLogger(__name__)
_running: dict[str, asyncio.Task] = {}


def _settled(name: str, task: asyncio.Task) -> None:
    if _running.get(name) is task:
        del _running[name]
    if task.cancelled():
        return
    exc = task.exception()          # read: one no caller waited for to the end isn't logged as never retrieved
    if exc is not None and not isinstance(exc, c.RuntimeFailure):
        log.warning("runtimes: %s's check broke", name, exc_info=exc)   # a runtime's own failure is its answer


def forget(name: str) -> None:
    """``name`` changed: the next caller starts its own check. One already
    running finishes for those who wait on it, and is shared no more."""
    _running.pop(name, None)


async def describe_once(name: str, runtime: c.Runtime) -> c.RuntimeInfo:
    """``runtime.describe()``, joined if it is already running. Cancelling
    the caller (``asyncio.wait_for``) doesn't cancel the check."""
    loop = asyncio.get_running_loop()
    task = _running.get(name)
    if task is None or task.get_loop() is not loop:
        task = loop.create_task(runtime.describe(), name=f"describe:{name}")
        _running[name] = task
        task.add_done_callback(lambda done: _settled(name, done))
    return await asyncio.shield(task)
