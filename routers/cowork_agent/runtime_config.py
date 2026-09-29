"""Runtime setup routes for the local Quirq installation."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.cowork_agent.runtime_config import (
    runtime_status,
    save_root_settings,
    save_settings,
)

router = APIRouter()


class RuntimeConfigRequest(BaseModel):
    agent_name: str
    watcher_enabled: bool
    watcher_interval_seconds: float
    watcher_source_mode: str


class RootConfigRequest(BaseModel):
    xo_projects_root: str
    quirq_state_root: str


@router.get("/api/runtime-config")
async def get_runtime_config() -> dict:
    return await runtime_status()


@router.put("/api/runtime-config")
async def put_runtime_config(body: RuntimeConfigRequest) -> dict:
    try:
        saved = await asyncio.to_thread(save_settings, body.model_dump())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "saved": saved,
        "status": await runtime_status(),
    }


@router.put("/api/runtime-config/roots")
async def put_runtime_roots(body: RootConfigRequest) -> dict:
    try:
        saved = await asyncio.to_thread(save_root_settings, body.model_dump())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "saved": saved,
        "status": await runtime_status(),
    }


@router.post("/api/runtime-config/restart")
async def restart_runtime(request: Request) -> dict:
    """Compatibility alias for the Setup process control."""
    from routers.space import space_server_restart

    return await space_server_restart(request)
