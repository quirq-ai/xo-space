"""``/api/schedules*`` — fixed-interval command jobs (local layer).

Thin handlers over ``utils/commands/scheduler.py`` (the scheduling half of
the command utility): parse → call it → map its typed errors to HTTP. Lives at
the top of ``routers/`` beside ``space.py`` and ``xo_data.py`` because it is
part of xo-space's local layer, not the broker's Plane B surface; ``server.py``
mounts it directly. Writes and manual execution are localhost-only, like
Space's process controls.

The body of POST/PUT is passed to the service as a plain dict so that every
validation failure — a wrong type included — is one ``400`` with the
service's message, rather than a pydantic ``422`` for some and a ``400`` for
the rest.

An agent job (``kind: "agent"``, storm design ch 17 §2.8) is the person's
own, so it opens no review item; storm checks its project chat, agent, model
and effort before it is saved, and refuses with storm's error shape
(``{"detail": {"code", "message"}}``). Command jobs keep the plain ``400``s.
Running an agent job now needs storm running in this server (``503``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from routers.browser_guard import is_local_mutation
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import jobs as storm_jobs
from services.errors import ServiceError
from utils.commands import scheduler
from utils.commands.scheduler import (
    AgentJobsUnavailable,
    ConcurrencyLimitError,
    JobRunningError,
    SchedulerError,
    UnknownJobError,
)

router = APIRouter()


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail="commands require a local client and same-origin browser request")


def _call(fn, *args, **kwargs):
    """Run a service call and translate its errors."""
    try:
        return fn(*args, **kwargs)
    except UnknownJobError as exc:
        raise HTTPException(status_code=404, detail=f"no such job: {exc}") from exc
    except (JobRunningError, ConcurrencyLimitError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # includes CommandSpecError
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentJobsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SchedulerError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/schedules")
def list_schedules() -> dict:
    return {"jobs": _call(scheduler.list_jobs)}


def _is_agent(body: Any) -> bool:
    return isinstance(body, dict) and body.get("kind") == scheduler.KIND_AGENT


async def _save_agent_job(body: dict, job_id: str | None = None) -> dict:
    try:
        return await storm_jobs.current().save(body, job_id)
    except ServiceError as exc:
        raise storm_error(exc) from exc


@router.post("/api/schedules", status_code=201, dependencies=[Depends(_require_local)])
async def create_schedule(body: Any = Body(...)) -> dict:
    if _is_agent(body):
        return await _save_agent_job(body)
    return await run_in_threadpool(_call, scheduler.create_job, body)


@router.get("/api/schedules/{job_id}")
def get_schedule(job_id: str) -> dict:
    return _call(scheduler.get_job, job_id)


@router.put("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
async def update_schedule(job_id: str, body: Any = Body(...)) -> dict:
    if _is_agent(body):
        return await _save_agent_job(body, job_id)
    return await run_in_threadpool(_call, scheduler.update_job, job_id, body)


@router.delete("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
def delete_schedule(job_id: str) -> dict:
    _call(scheduler.delete_job, job_id)
    return {"ok": True, "deleted": job_id}


@router.post("/api/schedules/{job_id}/run", status_code=202, dependencies=[Depends(_require_local)])
def run_schedule_now(job_id: str) -> dict:
    job = _call(scheduler.run_now, job_id)
    return {"ok": True, "started": True, "job": job}


@router.get("/api/schedules/{job_id}/runs")
def list_schedule_runs(job_id: str, limit: int = Query(20, ge=1, le=500)) -> dict:
    return {"job_id": job_id, "runs": _call(scheduler.list_runs, job_id, limit),
            "log_path": str(scheduler.log_file(job_id))}
