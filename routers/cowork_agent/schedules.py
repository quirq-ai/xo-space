"""``/api/schedules*`` — fixed-interval command jobs (local Quirq).

Thin handlers over ``utils/commands/scheduler.py`` (the scheduling half of
the command utility): parse → call it → map its typed errors to HTTP. Same
auth posture as the other local
Quirq routes (``/api/runtime-config``, ``/api/quirq``): nothing beyond what
the server applies globally.

The body of POST/PUT is passed to the service as a plain dict so that every
validation failure — a wrong type included — is one ``400`` with the
service's message, rather than a pydantic ``422`` for some and a ``400`` for
the rest.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from utils.commands import scheduler
from utils.commands.scheduler import (
    JobRunningError,
    SchedulerError,
    UnknownJobError,
)

router = APIRouter()


def _call(fn, *args, **kwargs):
    """Run a service call and translate its errors."""
    try:
        return fn(*args, **kwargs)
    except UnknownJobError as exc:
        raise HTTPException(status_code=404, detail=f"no such job: {exc}") from exc
    except JobRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # includes CommandSpecError
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SchedulerError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/schedules")
def list_schedules() -> dict:
    return {"jobs": _call(scheduler.list_jobs)}


@router.post("/api/schedules", status_code=201)
def create_schedule(body: Any = Body(...)) -> dict:
    return _call(scheduler.create_job, body)


@router.get("/api/schedules/{job_id}")
def get_schedule(job_id: str) -> dict:
    return _call(scheduler.get_job, job_id)


@router.put("/api/schedules/{job_id}")
def update_schedule(job_id: str, body: Any = Body(...)) -> dict:
    return _call(scheduler.update_job, job_id, body)


@router.delete("/api/schedules/{job_id}")
def delete_schedule(job_id: str) -> dict:
    _call(scheduler.delete_job, job_id)
    return {"ok": True, "deleted": job_id}


@router.post("/api/schedules/{job_id}/run", status_code=202)
def run_schedule_now(job_id: str) -> dict:
    job = _call(scheduler.run_now, job_id)
    return {"ok": True, "started": True, "job": job}


@router.get("/api/schedules/{job_id}/runs")
def list_schedule_runs(job_id: str, limit: int = Query(20, ge=1, le=500)) -> dict:
    return {"job_id": job_id, "runs": _call(scheduler.list_runs, job_id, limit)}
