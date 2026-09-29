"""The one loop skeleton behind the background pollers.

:func:`run_forever` is the shape the GitHub and the connections pollers
share: an optional enabled gate, a startup delay, then tick, log a failure
and go on, sleep, forever, until the task is cancelled. Space-level: any
package may use it. Each tick is recorded in :mod:`services.background`
under ``name``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from services import background

_module_logger = logging.getLogger(__name__)


async def run_forever(
    name: str,
    tick: Callable[[], Awaitable[object]],
    *,
    interval_s: Callable[[], float],
    startup_delay_s: float = 0.0,
    enabled: Optional[Callable[[], bool]] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Run ``tick`` forever, ``interval_s()`` seconds apart.

    Returns at once when ``enabled`` is given and ``enabled()`` is false.
    Otherwise sleeps ``startup_delay_s``, then loops: ``await tick()``; an
    ``Exception`` is logged as ``"<name>: tick failed (non-fatal)"`` with
    the traceback and the loop goes on; ``CancelledError`` propagates, so
    cancelling the task is how the loop stops. ``interval_s`` is called
    again on every pass (the pollers re-read their environment each tick).
    ``logger`` defaults to this module's.
    """
    log = logger or _module_logger
    if enabled is not None and not enabled():
        return
    await asyncio.sleep(startup_delay_s)
    while True:
        background.tick_started(name)
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            background.tick_failed(name, exc)
            log.warning("%s: tick failed (non-fatal)", name, exc_info=True)
        else:
            background.tick_succeeded(name)
        await asyncio.sleep(interval_s())
