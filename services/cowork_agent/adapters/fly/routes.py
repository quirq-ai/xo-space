"""Fly-owned endpoints, mounted only when this adapter is active."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from services.cowork_agent.adapters.fly import artifacts, sessions
from services.errors import ServiceError

router = APIRouter()


@router.get("/api/fly/catalog")
async def catalog():
    return await artifacts.reload_catalog()


@router.post("/api/fly/reload")
async def reload_flies():
    return await artifacts.reload_catalog()


@router.get("/api/fly/runs/{run_id}")
def get_run(run_id: str):
    try:
        record = sessions.get_run(run_id)
    except ServiceError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc
    if not record:
        raise HTTPException(404, "Fly run not found.")
    return record
