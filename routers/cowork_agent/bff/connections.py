"""BFF routes for polled connections (the Inbox's Connections section and
the Connectors tab's Polling drawer).

  GET    /api/connections                       {signed_in, poller_enabled, connections: [...]}
  GET    /api/connections/{toolkit}             one connection (defaults when not configured)
  PUT    /api/connections/{toolkit}             body {enabled?, interval_s?, collectors?}; creates the config on first call
  DELETE /api/connections/{toolkit}             {toolkit, removed}
  POST   /api/connections/{toolkit}/poll        poll now, ignoring enabled and interval; the poll summary
  GET    /api/connections/{toolkit}/events      {toolkit, events: [...]} newest-first, limit 1..500

Thin over services.cowork_agent.connections.service (typed errors become
HTTP here). The toolkit path value is checked against the id shape before
any service call so an unknown id is a 404 with {code, message} and never
reaches the filesystem. Bodies are strict: a string where an int or a bool
belongs is a 422, the same as an unknown key. No os/pathlib in this module
(BFF rule P2).
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from services.cowork_agent.connections import service

router = APIRouter()

TOOLKIT_RE = re.compile(r"[a-z0-9_]{1,40}")


class _ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigureBody(_ForbidExtra):
    enabled: Optional[StrictBool] = None
    interval_s: Optional[StrictInt] = None
    collectors: Optional[list[StrictStr]] = None


def _http(exc: service.ConnectionsError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


def _toolkit_or_404(toolkit: str) -> str:
    if not isinstance(toolkit, str) or not TOOLKIT_RE.fullmatch(toolkit):
        raise HTTPException(status_code=404, detail={"code": "unknown_toolkit", "message": "Unknown toolkit."})
    return toolkit


@router.get("/api/connections")
def list_connections() -> dict:
    try:
        return {"signed_in": service.signed_in(), "poller_enabled": service.poller_enabled(),
                "connections": service.list_connections()}
    except service.ConnectionsError as exc:
        raise _http(exc)


@router.get("/api/connections/{toolkit}")
def get_connection(toolkit: str) -> dict:
    try:
        return service.get_connection(_toolkit_or_404(toolkit))
    except service.ConnectionsError as exc:
        raise _http(exc)


@router.put("/api/connections/{toolkit}")
def configure_connection(toolkit: str, body: ConfigureBody) -> dict:
    fields = {name: value for name, value in body.model_dump().items() if value is not None}
    try:
        return service.configure(_toolkit_or_404(toolkit), **fields)
    except service.ConnectionsError as exc:
        raise _http(exc)


@router.delete("/api/connections/{toolkit}")
def remove_connection(toolkit: str) -> dict:
    try:
        return {"toolkit": toolkit, "removed": service.remove(_toolkit_or_404(toolkit))}
    except service.ConnectionsError as exc:
        raise _http(exc)


@router.post("/api/connections/{toolkit}/poll")
async def poll_connection_now(toolkit: str) -> dict:
    try:
        return await service.poll_now(_toolkit_or_404(toolkit))
    except service.ConnectionsError as exc:
        raise _http(exc)


@router.get("/api/connections/{toolkit}/events")
def list_connection_events(toolkit: str, limit: int = Query(50, ge=1, le=500)) -> dict:
    try:
        return {"toolkit": toolkit, "events": service.events(_toolkit_or_404(toolkit), limit=limit)}
    except service.ConnectionsError as exc:
        raise _http(exc)
