"""The telemetry module's loops, supervised.

* ``watcher``     tails the active agent's session store into project
                  records, stats and the timeline, once per tick
                  (``services/cowork_agent/visualizer/watcher.py``).
                  ``QUIRQ_WATCHER_ENABLED=false`` stays a second gate
                  beside the switch in ``module.json`` for one release.
* ``usage_sync``  reports usage to XO once a day (``usage_sync.py``).

Both were listed by ``modules/agent/tasks.py``; until those entries are
dropped the supervisor sees each loop twice, and the copy here stands by
while the other runs, so one process never runs two watchers over the same
cursors.
"""

from __future__ import annotations

import asyncio
import logging

from services.supervisor import Task

from . import service

logger = logging.getLogger(__name__)

#: The module that used to declare these loops. Transitional: delete this
#: guard together with its ``watcher`` and ``usage_sync`` entries.
async def start_watcher() -> None:
    await service.run_watcher()


async def start_usage_sync() -> None:
    await service.run_usage_sync()


TASKS = [
    Task("watcher", start_watcher, enabled=service.watcher_enabled,
         description="Tails the active agent's session store into project records, stats and the timeline."),
    Task("usage_sync", start_usage_sync,
         description="Reports usage to XO once a day."),
]
