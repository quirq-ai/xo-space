"""``/api/telemetry/sources``: what the Agents tab's Configure page reads
and writes. The service owns the rules; this router only shapes the wire.

  GET  /api/telemetry/sources        every telemetry provider with its
                                     effective data path and switch
  PUT  /api/telemetry/sources/{id}   save a path (empty clears the override)
                                     and/or flip collection; kicks off a
                                     background rebuild of sessions.json so
                                     the next Refresh reflects the change
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from routers.errors import ForbidExtra, http_error
from services import telemetry_sources
from services.errors import ServiceError

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

_NO_STORE = {"Cache-Control": "no-store"}


class SourceUpdate(ForbidExtra):
    path: Optional[str] = None
    enabled: Optional[bool] = None


def _rebuild_sessions_view() -> None:
    from services.cowork_agent.visualizer.workspace import views

    try:
        views.build("sessions")
    except Exception as exc:  # the next watcher tick retries; never fail the save
        print(f"⚠️ sessions view rebuild after source change failed ({exc})")


@router.get("/sources")
async def list_sources():
    sources = await asyncio.to_thread(telemetry_sources.list_sources)
    return JSONResponse({"items": sources, "total": len(sources)}, headers=_NO_STORE)


@router.put("/sources/{source_id}")
async def update_source(source_id: str, body: SourceUpdate):
    try:
        source = await asyncio.to_thread(
            telemetry_sources.save_source,
            source_id,
            path=body.path,
            enabled=body.enabled,
        )
    except ServiceError as exc:
        raise http_error(exc)
    except OSError as exc:
        raise http_error(
            ServiceError("store_unavailable", f"Could not write the env store: {exc}", 503)
        )
    # Collection status lives in sessions.json; rebuild it off the request
    # path so the save answers immediately and Refresh sees the new state.
    asyncio.get_running_loop().run_in_executor(None, _rebuild_sessions_view)
    return JSONResponse({"item": source, "rebuilding": True}, headers=_NO_STORE)
