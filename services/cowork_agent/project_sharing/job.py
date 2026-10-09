"""The relay as a built-in job of the watcher's command scheduler.

Design: infra/commands/DESIGN.md ("Project sharing on the scheduler"). At
start-up the server registers the "sharing tick" job (every poll interval,
`qq sharing tick --json`, or the same module run directly when qq is not on
PATH). The watcher keeps time, the scheduler handles overlap and logging, and
each run is tick.py. With the watcher off there is no job and sharing is off;
status_snapshot() then reports reason "watcher_off" for the Sharing page.

Nudges (share, revoke, apply, "Check now", a local push the watcher notices)
start the job at once through scheduler.run_now. A nudge while a run is in
progress leaves a marker the running tick picks up for one more round.
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

from . import config, poller

log = logging.getLogger(__name__)

JOB_NAME = "sharing tick"
DESCRIPTION = ("Built in: the project sharing relay (poll, fetch, auto-clone, publish). "
               "The server keeps this job; edits are reset when it starts.")
TIMEOUT_SECONDS = 600
SCAN_INTERVAL = 5.0
REPO_ROOT = Path(__file__).resolve().parents[3]

_job_id: str | None = None
_watcher_off = False
_last_signature: tuple | None = None
_last_scan = 0.0


def _argv() -> list[str]:
    """qq first; the same tick module directly when qq cannot be found."""
    qq = shutil.which("qq")
    if qq:
        return [qq, "sharing", "tick", "--json"]
    return [sys.executable, "-m", "services.cowork_agent.project_sharing.tick", "--json"]


def ensure_job() -> str:
    """Create or reset the built-in job and remember its id."""
    global _job_id
    from utils.commands import scheduler

    payload = {
        "name": JOB_NAME,
        "description": DESCRIPTION,
        "command": {"argv": _argv(), "timeout": TIMEOUT_SECONDS, "cwd": str(REPO_ROOT)},
        "every_seconds": max(1, int(round(config.poll_interval()))),
        "enabled": True,
    }
    existing = next((j for j in scheduler.list_jobs() if j.get("name") == JOB_NAME), None)
    job = scheduler.update_job(existing["id"], payload) if existing else scheduler.create_job(payload)
    _job_id = job["id"]
    return _job_id


def mark_watcher_off() -> None:
    global _watcher_off
    _watcher_off = True


def watcher_off() -> bool:
    return _watcher_off


def job_id() -> str | None:
    return _job_id


def _nudge_marker() -> Path:
    from services.storage.layout import sharing_dir
    return sharing_dir() / ".nudge"


def take_pending_nudge() -> bool:
    """True once for each nudge that arrived while a tick was running."""
    try:
        _nudge_marker().unlink()
        return True
    except FileNotFoundError:
        return False


def nudge() -> None:
    """Run the relay now. Without the job (the old loop), the old nudge."""
    if _job_id is None:
        poller.nudge()
        return
    from utils.commands import scheduler

    try:
        scheduler.run_now(_job_id)
    except scheduler.JobRunningError:
        try:
            marker = _nudge_marker()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as exc:
            log.warning("project_sharing: could not record a nudge: %s", exc)
    except scheduler.SchedulerError as exc:
        log.warning("project_sharing: nudge not started: %s", exc)


def local_change_check() -> None:
    """Called on every watcher tick. Every SCAN_INTERVAL seconds, the cheap
    filesystem signature of the clones and their origin refs; a change (a new
    clone, this machine's own push) starts the job at once. Never raises."""
    global _last_signature, _last_scan
    if _job_id is None:
        return
    now = time.monotonic()
    if now - _last_scan < SCAN_INTERVAL:
        return
    _last_scan = now
    try:
        signature = poller._local_signature()
    except Exception as exc:  # noqa: BLE001
        log.warning("project_sharing: local scan failed: %s", exc)
        return
    if _last_signature is not None and signature != _last_signature:
        nudge()
    _last_signature = signature
