"""The agent side's background loops, supervised like every module's.

The watcher and usage sync belong to modules/telemetry, the MCP gateway
reconcile to modules/connectors; what stays here is what only the agent
side needs.

Each entry wraps a loop that ``server.py`` used to start and cancel by
hand. The env flags those loops honoured keep working for one release as a
second gate beside the switch in ``module.json``; the switch is the
documented way to turn one off.
"""

from __future__ import annotations

import os

from services.supervisor import Task



async def _github_poller() -> None:
    from services.cowork_agent.github_poller import start_github_poller
    await start_github_poller()


def _github_enabled() -> bool:
    from services.cowork_agent.github_poller import poller_enabled
    return poller_enabled()


async def _startup_skills() -> None:
    from services.cowork_agent.skill_catalog import install_startup_skills
    await install_startup_skills()


async def _xo_status() -> None:
    from services.xo_manifest import seed_agent_status
    await seed_agent_status()


TASKS = [
    Task("github_poller", _github_poller, enabled=_github_enabled,
         description="Refreshes the GitHub issue mirror of every project with a github.com remote."),
    Task("startup_skills", _startup_skills, oneshot=True,
         description="Installs the active agent's declared boot-time skills, once per start."),
    Task("xo_status", _xo_status, oneshot=True,
         description="Seeds the live status in xo.json, once per start."),
]
