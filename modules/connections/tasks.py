"""The connections poller, supervised.

The tick reads ``tasks.poller.tick_s`` from the effective switches each
pass (``services.modules.settings``), so a change from Setup applies at the
next tick; ``XO_CONNECTIONS_POLL_ENABLED=false`` still works as a second
gate for one release.
"""

from __future__ import annotations

from services.supervisor import Task

from . import poller

TASKS = [
    Task("poller", poller.start_connections_poller, enabled=poller.poller_enabled,
         description="Runs each due connection's collectors and appends what arrived to its events log."),
]
