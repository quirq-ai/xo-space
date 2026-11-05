"""Clone jobs: ``POST /api/xo-projects`` with ``"wait": false``.

A job runs :func:`services.project_management.run_clone` in the background
and keeps what the frontend polls (``GET /api/xo-projects/clones/{id}``)::

    {clone_id, project_id, state, phase, phase_percent, percent,
     received_bytes, idle_seconds, error, warning, started_at, finished_at}

``state`` is ``cloning``, then ``done``, ``failed`` or ``cancelled``.
``phase`` is ``counting``, ``receiving``, ``resolving``, ``checkout`` or
``finishing``; ``percent`` is overall and never goes down.
``idle_seconds`` is how long git has said nothing (0 once finished): a large
repository can sit in ``counting`` with no figure for tens of seconds while
the server prepares it, and this tells that apart from a frozen dialog. ``error`` is
``{code, message}`` once failed, plus ``detail`` (git's last line) when git
said something.

Jobs live in this process only. A finished job is kept for
:data:`RETENTION_S` so a poll that comes late still sees how it ended; a
server restart cancels running clones (their staging folders are removed as
the tasks are cancelled) and forgets every job.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from services import project_management
from services.errors import ServiceError
from services.timestamps import now_iso

log = logging.getLogger(__name__)

#: How long a finished job stays readable.
RETENTION_S = 15 * 60
#: Clones running at once; each holds a git process and a download.
MAX_RUNNING = 4
#: How long ``DELETE`` waits for git to stop and the staging folder to go.
CANCEL_WAIT_S = 15.0

RUNNING, DONE, FAILED, CANCELLED = "cloning", "done", "failed", "cancelled"


@dataclass
class CloneJob:
    clone_id: str
    project_id: str
    state: str = RUNNING
    progress: dict = field(default_factory=lambda: {"phase": "counting", "phase_percent": None,
                                                    "percent": 0, "received_bytes": 0})
    error: dict | None = None
    warning: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    finished_mono: float | None = None
    last_output_mono: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        idle = 0 if self.state != RUNNING else int(time.monotonic() - self.last_output_mono)
        return {"clone_id": self.clone_id, "project_id": self.project_id, "state": self.state,
                **self.progress, "idle_seconds": idle, "error": self.error, "warning": self.warning,
                "started_at": self.started_at, "finished_at": self.finished_at}

    def _finish(self, state: str) -> None:
        self.state, self.finished_at, self.finished_mono = state, now_iso(), time.monotonic()


_jobs: dict[str, CloneJob] = {}


def _prune() -> None:
    cutoff = time.monotonic() - RETENTION_S
    for clone_id in [k for k, job in _jobs.items() if job.finished_mono is not None and job.finished_mono < cutoff]:
        del _jobs[clone_id]


def _get(clone_id: str) -> CloneJob:
    _prune()
    job = _jobs.get(clone_id) if isinstance(clone_id, str) else None
    if job is None:
        raise ServiceError("clone_not_found", "No such clone. It may have finished more than 15 minutes ago.", 404)
    return job


def start_clone(project_id: str, repository_url: str, *, display_name: str | None = None,
                description: str | None = None) -> dict:
    """Validate now (the same 400/409 as the blocking form), clone in the background."""
    _prune()
    if sum(job.state == RUNNING for job in _jobs.values()) >= MAX_RUNNING:
        raise ServiceError("too_many_clones", f"{MAX_RUNNING} clones are already running. "
                           "Wait for one to finish and try again.", 429)
    request = project_management.prepare_clone(project_id, repository_url, display_name=display_name,
                                               description=description)
    job = CloneJob(clone_id="cl_" + secrets.token_hex(12), project_id=request.project_id)
    job.task = asyncio.create_task(_run(job, request), name=f"clone:{request.project_id}")
    # A task cancelled before it first runs never enters run_clone's finally.
    job.task.add_done_callback(lambda _: request.release())
    _jobs[job.clone_id] = job
    return job.snapshot()


async def _run(job: CloneJob, request: project_management.CloneRequest) -> None:
    def on_progress(progress: dict) -> None:
        job.progress = dict(progress)
        job.last_output_mono = time.monotonic()

    try:
        result = await project_management.run_clone(request, on_progress=on_progress)
    except asyncio.CancelledError:
        job._finish(CANCELLED)
        raise
    except ServiceError as exc:
        job.error = {"code": exc.code, "message": exc.message}
        if getattr(exc, "detail", None):
            job.error["detail"] = exc.detail
        job._finish(FAILED)
    except Exception:  # noqa: BLE001 - a job always ends in a state the poller can read
        log.exception("clone job %s failed unexpectedly", job.clone_id)
        job.error = {"code": "clone_failed", "message": "Could not clone the repository."}
        job._finish(FAILED)
    else:
        job.warning = result.get("warning")
        job.progress = {**job.progress, "phase": "finishing", "phase_percent": 100, "percent": 100}
        job._finish(DONE)


def clone_status(clone_id: str) -> dict:
    return _get(clone_id).snapshot()


async def cancel_clone(clone_id: str) -> dict:
    """Stop a running clone and wait until its staging folder is gone.

    Cancelling a cancelled clone returns it again; a clone that already
    finished cannot be cancelled (409 ``clone_finished``).
    """
    job = _get(clone_id)
    if job.state == CANCELLED:
        return job.snapshot()
    if job.state != RUNNING or job.task is None or job.task.done():
        raise ServiceError("clone_finished", f"This clone has already {'failed' if job.state == FAILED else 'finished'}.", 409)
    job.task.cancel()
    # git gets SIGTERM, then SIGKILL; the staging folder goes before the task ends.
    await asyncio.wait({job.task}, timeout=CANCEL_WAIT_S)
    if not job.task.done():
        log.warning("clone job %s: still stopping after %ss", clone_id, CANCEL_WAIT_S)
    elif job.state == RUNNING:
        job._finish(CANCELLED)  # cancelled before it ever started
    return job.snapshot()


async def shutdown() -> None:
    """Cancel every running clone (git stopped, staging removed) before the
    loop goes away. Never raises."""
    tasks = [job.task for job in _jobs.values() if job.task is not None and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=CANCEL_WAIT_S)
