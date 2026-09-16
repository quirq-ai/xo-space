"""xo-doctor: the state health report and its one action. Policy is in services/doctor."""

from __future__ import annotations

import asyncio

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


@router.get("/api/doctor")
async def get_doctor_report() -> dict:
    return await asyncio.to_thread(run.run_checks)


@router.post("/api/doctor/runtime-leftovers/{key}/move-aside")
async def move_runtime_leftover_aside(key: str, body: MoveAside, request: Request) -> dict:
    _require_mutation(request)
    try:
        return await asyncio.to_thread(leftovers.move_aside, key)
    except ServiceError as exc:
        raise http_error(exc) from exc
