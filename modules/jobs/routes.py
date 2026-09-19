"""``/api/schedules*``: saved commands on a fixed interval or on demand.

The seven paths keep their name: ``/api/schedules`` is the manifest alias
this module serves outside ``/api/jobs``, so every client and skill that
learned the old surface keeps working. ``server.py`` mounts the router
through the registry, behind the ``jobs`` api gate.

  GET    /api/schedules                  {jobs: [...]}
  POST   /api/schedules                  201, the job as the API shows it
  GET    /api/schedules/{job_id}         one job, definition merged with its state
  PUT    /api/schedules/{job_id}         replace the definition, keep the id
  DELETE /api/schedules/{job_id}         {ok, deleted}
  POST   /api/schedules/{job_id}/run     202, run now (trigger: manual)
  GET    /api/schedules/{job_id}/runs    {job_id, runs: [...], log_path}; limit 1..500

Thin handlers over ``modules.jobs.service``: parse, call it, return. Its
typed errors carry their own status (404 for an unknown job, 409 while a
run is in progress or the concurrency cap is hit, 400 for a definition that
cannot be stored, 500 for a jobs file that cannot be read or written) and
reach the wire through the app's service error handler, as the bare message
this API has always answered with. Writes and manual execution are
localhost-only, like Space's process controls.

The body of POST/PUT is passed to the service as a plain dict so that every
validation failure, a wrong type included, is one ``400`` with the
service's message, rather than a pydantic ``422`` for some and a ``400`` for
the rest.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from routers.browser_guard import is_local_mutation

from . import service

router = APIRouter()


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail="commands require a local client and same-origin browser request")


@router.get("/api/schedules")
def list_schedules() -> dict:
    return {"jobs": service.list_jobs()}


@router.post("/api/schedules", status_code=201, dependencies=[Depends(_require_local)])
def create_schedule(body: Any = Body(...)) -> dict:
    return service.create_job(body)


@router.get("/api/schedules/{job_id}")
def get_schedule(job_id: str) -> dict:
    return service.get_job(job_id)


@router.put("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
def update_schedule(job_id: str, body: Any = Body(...)) -> dict:
    return service.update_job(job_id, body)


@router.delete("/api/schedules/{job_id}", dependencies=[Depends(_require_local)])
def delete_schedule(job_id: str) -> dict:
    service.delete_job(job_id)
    return {"ok": True, "deleted": job_id}


@router.post("/api/schedules/{job_id}/run", status_code=202, dependencies=[Depends(_require_local)])
def run_schedule_now(job_id: str) -> dict:
    job = service.run_now(job_id)
    return {"ok": True, "started": True, "job": job}


@router.get("/api/schedules/{job_id}/runs")
def list_schedule_runs(job_id: str, limit: int = Query(20, ge=1, le=500)) -> dict:
    return {"job_id": job_id, "runs": service.list_runs(job_id, limit),
            "log_path": str(service.log_file(job_id))}
