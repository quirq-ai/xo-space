"""xo-doctor: the state health report and its one action. Policy is in services/doctor."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services.doctor import leftovers, run
from services.errors import ServiceError

router = APIRouter()


def _require_mutation(request: Request) -> None:
    """JSON and the same origin, as project removal requires (bff/project_management.py)."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail={"code": "json_required", "message": "Send an application/json request."})
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail={"code": "same_origin_required", "message": "Manage runtime data from this Space's Setup page."})


class MoveAside(ForbidExtra):
    """No fields: the body exists so the JSON requirement applies."""


#: The run in progress, and the event loop it belongs to. Requests that arrive
#: while it runs share it: each run holds a thread from the pool the rest of
#: the server shares and costs memory while it parses a large state root, so
#: several open Quirq pages must not multiply either.
_in_flight: Optional[tuple[asyncio.AbstractEventLoop, "asyncio.Future[dict]"]] = None
#: How long one request waits for the report. A read hung on a dead network
#: mount can't be cancelled (to_thread work can't be), so past this the caller
#: gets a 503 instead of waiting for ever; the run itself stays shared.
REPORT_TIMEOUT_S = 60.0


def _retrieve(future: "asyncio.Future[dict]") -> None:
    """Mark a finished run's exception as seen, so a run whose every caller
    gave up doesn't log "Task exception was never retrieved"."""
    if not future.cancelled():
        future.exception()


@router.get("/api/doctor")
async def get_doctor_report() -> dict:
    global _in_flight
    loop = asyncio.get_running_loop()
    if _in_flight is None or _in_flight[0] is not loop or _in_flight[1].done():
        future = asyncio.ensure_future(asyncio.to_thread(run.run_checks))
        future.add_done_callback(_retrieve)
        _in_flight = (loop, future)
    else:
        future = _in_flight[1]
    try:
        # shield: one caller timing out or disconnecting must not cancel the
        # run the others are waiting on.
        return await asyncio.wait_for(asyncio.shield(future), REPORT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail={
            "code": "doctor_timeout",
            "message": "The checks are taking too long. A disk or network mount may not be responding.",
        }) from None


@router.post("/api/doctor/runtime-leftovers/{key}/move-aside")
async def move_runtime_leftover_aside(key: str, body: MoveAside, request: Request) -> dict:
    _require_mutation(request)
    try:
        return await asyncio.to_thread(leftovers.move_aside, key)
    except ServiceError as exc:
        raise http_error(exc) from exc
