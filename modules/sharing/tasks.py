"""The commit relay, supervised.

One loop: ``poller.run_relay_poller`` polls XO for shared projects, fetches
what the ledger delivered and publishes this machine's pushes, once per
tick (``PROJECT_SHARING_POLL_INTERVAL_SECONDS``, jittered, or sooner when
nudged). ``PROJECT_SHARING_ENABLED=false`` stays a second gate beside the
switch in ``module.json`` for one release; the switch is the documented
way to turn it off.
"""

from __future__ import annotations

from services.supervisor import Task

from . import config, poller

TASKS = [
    Task("relay", poller.run_relay_poller, enabled=config.enabled,
         description="Polls XO for shared projects, fetches and publishes commits, once per tick."),
]
