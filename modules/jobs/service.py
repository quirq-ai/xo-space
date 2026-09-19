"""The jobs module's facade: what the routes, the tick task, the CLI
commands and other modules call. Thin over ``scheduler.py``; the typed
failures (``SchedulerError`` and its family, ``InvalidJobError``) carry
their own status and reach the wire through the app's service error
handler, as the bare message ``/api/schedules`` has always answered with.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import scheduler, store
from .scheduler import (  # noqa: F401  (re-exported: callers name them as service.<Error>)
    ConcurrencyLimitError,
    InvalidJobError,
    JobRunningError,
    SchedulerError,
    TickReport,
    UnknownJobError,
)


def create_job(payload: Any, *, now: Optional[datetime] = None) -> dict:
    return scheduler.create_job(payload, now=now)


def get_job(job_id: str) -> dict:
    return scheduler.get_job(job_id)


def list_jobs() -> list[dict]:
    return scheduler.list_jobs()


def update_job(job_id: str, payload: Any, *, now: Optional[datetime] = None) -> dict:
    return scheduler.update_job(job_id, payload, now=now)


def delete_job(job_id: str) -> None:
    scheduler.delete_job(job_id)


def list_runs(job_id: str, limit: int = 20) -> list[dict]:
    return scheduler.list_runs(job_id, limit)


def run_now(job_id: str, *, now: Optional[datetime] = None) -> dict:
    return scheduler.run_now(job_id, now=now)


def log_file(job_id: str) -> Path:
    return store.log_file(job_id)


def validate_definition(payload: Any) -> dict:
    return scheduler.validate_definition(payload)


def tick(now: Optional[datetime] = None) -> TickReport:
    """One pass: harvest finished runs, sweep lost ones, launch what is due."""
    return scheduler.tick(now=now)


def scheduler_enabled() -> bool:
    return scheduler.scheduler_enabled()


def max_concurrent() -> int:
    return scheduler.max_concurrent()


def attach_loop(loop) -> None:
    """The server's event loop, so a module job's coroutine runs on it."""
    scheduler.attach_loop(loop)


def detach_loop() -> None:
    scheduler.detach_loop()


def reset_state() -> None:
    """Forget in-memory runs. For tests."""
    scheduler.reset_state()
