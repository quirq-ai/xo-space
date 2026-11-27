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

Agent jobs are storm's timers: the list holds the scheduler's command jobs
and storm's agent jobs, and an id is looked up in storm first (a job moved
from the scheduler keeps its id there; the scheduler's turned-off copy is
never shown). While storm isn't running, the list and the lookups serve the
command jobs. The person's default time zone, which agent-made jobs use, is
read and set at ``/api/schedules/time-zone``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from routers.browser_guard import is_local_mutation
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.engine import EngineUnavailable
from services.cowork_agent.storm.jobs import schedule as job_schedule
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


def _command_job(job_id: str) -> dict:
    """A command job. The scheduler's copy of an agent job is not one: storm,
    which runs agent jobs, didn't find it (``404``), or isn't running (``503``)."""
    job = _call(scheduler.get_job, job_id)
    if scheduler.is_agent(job):
        try:
            storm_jobs.current()
        except EngineUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"agent jobs run in storm: {exc.message}") from exc
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job


async def _storm(work):
    """Storm's answer, or None while storm isn't running here; its refusals in storm's shape."""
    try:
        return await work(storm_jobs.current())
    except EngineUnavailable:
        return None
    except ServiceError as exc:
        raise storm_error(exc) from exc


async def _agent_job(job_id: str, n: int = storm_jobs.UPCOMING) -> dict | None:
    return await _storm(lambda jobs: jobs.view(job_id, n))


@router.get("/api/schedules")
async def list_schedules(upcoming: int = Query(storm_jobs.UPCOMING, ge=0, le=storm_jobs.UPCOMING_MAX)) -> dict:
    commands = [job for job in await run_in_threadpool(_call, scheduler.list_jobs) if not scheduler.is_agent(job)]
    return {"jobs": commands + (await _storm(lambda jobs: jobs.views(upcoming)) or [])}


@router.get("/api/schedules/time-zone")
def get_time_zone() -> dict:
    return {"tz": job_schedule.default_tz()}


@router.put("/api/schedules/time-zone", dependencies=[Depends(_require_local)])
def set_time_zone(body: Any = Body(...)) -> dict:
    tz = body.get("tz") if isinstance(body, dict) else None
    if not isinstance(tz, str) or not tz:
        raise HTTPException(status_code=400, detail='the body is {"tz": "<IANA zone>"}, such as "Europe/London"')
    try:
        job_schedule.set_default_tz(tz)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tz": job_schedule.default_tz()}


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
async def get_schedule(job_id: str,
                       upcoming: int = Query(storm_jobs.UPCOMING, ge=0, le=storm_jobs.UPCOMING_MAX)) -> dict:
    return await _agent_job(job_id, upcoming) or await run_in_threadpool(_command_job, job_id)


@router.put("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
async def update_schedule(job_id: str, body: Any = Body(...)) -> dict:
    if _is_agent(body):
        return await _save_agent_job(body, job_id)
    return await run_in_threadpool(_call, scheduler.update_job, job_id, body)


@router.delete("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
async def delete_schedule(job_id: str) -> dict:
    if await _agent_job(job_id, 0):
        await _storm(lambda jobs: jobs.delete(job_id))
    else:
        await run_in_threadpool(_command_job, job_id)
        await run_in_threadpool(_call, scheduler.delete_job, job_id)
    return {"ok": True, "deleted": job_id}


@router.post("/api/schedules/{job_id}/run", status_code=202, dependencies=[Depends(_require_local)])
async def run_schedule_now(job_id: str) -> dict:
    if await _agent_job(job_id, 0):
        run = await _storm(lambda jobs: jobs.run_now(job_id))
        return {"ok": True, "started": True, "job": await _agent_job(job_id), **(run or {})}
    await run_in_threadpool(_command_job, job_id)
    job = await run_in_threadpool(_call, scheduler.run_now, job_id)
    return {"ok": True, "started": True, "job": job}


@router.get("/api/schedules/{job_id}/runs")
async def list_schedule_runs(job_id: str, limit: int = Query(20, ge=1, le=500),
                             before: str | None = Query(None)) -> dict:
    if await _agent_job(job_id, 0):
        return {"job_id": job_id, "runs": await _storm(lambda jobs: jobs.runs(job_id, limit, before)),
                "log_path": None}
    await run_in_threadpool(_command_job, job_id)
    return {"job_id": job_id, "runs": await run_in_threadpool(_call, scheduler.list_runs, job_id, limit),
            "log_path": str(scheduler.log_file(job_id))}
