"""The jobs tick, supervised.

Once per watcher tick interval (``utils.runtime_env.watcher_tick_interval_seconds``,
re-read each pass) the loop body :func:`tick_once` runs one
``service.tick()`` in a worker thread: the tick does file I/O under a lock
and never waits for a job, so the loop costs two small reads when nothing
is due. ``XO_SCHEDULER_ENABLED=false`` is the extra gate beside the module
switch. The loop itself (tick, log a failure and go on, sleep, stop on
cancel) is :func:`services.periodic.run_forever`.
"""

from __future__ import annotations

import asyncio
import logging

from services.periodic import run_forever
from services.supervisor import Task
from utils.runtime_env import watcher_tick_interval_seconds

from . import service

logger = logging.getLogger(__name__)


async def tick_once() -> service.TickReport:
    """The loop body: one tick, off the event loop; a tick that did
    something is logged (ids only, nothing from a job's output)."""
    report = await asyncio.to_thread(service.tick)
    if not report.quiet:
        logger.info("jobs: %s", report.as_dict())
    return report


async def start_tick() -> None:
    """Entry point for the supervised task. Registers the running loop so a
    module job's coroutine runs on it (the same loop its module's routes
    use), then ticks forever."""
    service.attach_loop(asyncio.get_running_loop())
    logger.info("jobs tick: started (%.2fs)", watcher_tick_interval_seconds())
    try:
        await run_forever("jobs tick", tick_once, interval_s=watcher_tick_interval_seconds, logger=logger)
    finally:
        service.detach_loop()


TASKS = [
    Task("tick", start_tick, enabled=service.scheduler_enabled,
         description="Once per watcher tick: records the runs that finished and launches the saved commands that are due."),
]
