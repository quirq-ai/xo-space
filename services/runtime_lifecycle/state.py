"""A runtime's lifecycle state (plan §5.1), derived from its binary on PATH,
its local login check and the job XO runs on it, if any."""

from __future__ import annotations

import asyncio

from services.cowork_agent.registry.agent_registry import installed
from services.runtime_lifecycle import jobs
from services.runtime_lifecycle.registry import ACTIONS, Runtime

_RUNNING = {"install": "installing", "update": "updating", "uninstall": "uninstalling"}


async def state(rt: Runtime, *, version: str | None = None) -> dict:
    """``not_installed``, ``installed`` or ``signed_in``; while a job runs its
    action (``installing``…), after one fails ``failed``. With the CLI's
    ``version``, whether it needs an update."""
    is_installed = installed(rt.spec.binary())
    signed_in = is_installed and await asyncio.to_thread(rt.spec.logged_in)
    out: dict = {"name": rt.name, "state": "signed_in" if signed_in else "installed" if is_installed
                 else "not_installed", "installed": is_installed, "signed_in": signed_in,
                 "actions": [a for a in ACTIONS if rt.command(a)] + (["sign_in"] if rt.spec.sign_in else [])}
    job = jobs.last(rt.name)
    if job is not None and (job.status == "failed" or (job.status == "running" and job.action in _RUNNING)):
        out.update(state="failed" if job.status == "failed" else _RUNNING[job.action], job=job.id)
    if version is not None:
        note = rt.spec.update_note(version)
        out.update(version=version, needs_update=note is not None, update=note)
    return out
