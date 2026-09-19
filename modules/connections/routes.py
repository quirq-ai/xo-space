"""BFF routes for polled connections (the Inbox's Connections section and
the Connectors tab's Polling drawer).

  GET    /api/connections                       {signed_in, poller_enabled, connections: [...]}
  GET    /api/connections/{toolkit}             one connection (defaults when not configured)
  PUT    /api/connections/{toolkit}             body {enabled?, interval_s?, collectors?}; creates the config on first call
  DELETE /api/connections/{toolkit}             {toolkit, removed}
  POST   /api/connections/{toolkit}/poll        poll now, ignoring enabled and interval; the poll summary
  POST   /api/connections/{toolkit}/account     resolve the connected account's label now and cache it:
                                                {toolkit, account_label, account_checked_at, error, cached};
                                                a provider or session failure is 200 with error set
  GET    /api/connections/{toolkit}/events      {toolkit, events: [...]} newest-first, limit 1..500

Thin over ``modules.connections.service``: the toolkit path value goes through
as given and the service answers an unknown or malformed id with its own
404 ``{code, message}`` before any path is built; every typed failure
reaches the wire through the app's service error handler
(``routers/errors.py``). Bodies are strict: a string where an int or a bool
belongs is a 422, the same as an unknown key. No os/pathlib in this module
(BFF rule P2).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import StrictBool, StrictInt, StrictStr

from routers.errors import ForbidExtra
from . import service

router = APIRouter()


class ConfigureBody(ForbidExtra):
    enabled: Optional[StrictBool] = None
    interval_s: Optional[StrictInt] = None
    collectors: Optional[list[StrictStr]] = None


@router.get("/api/connections")
def list_connections() -> dict:
    return {"signed_in": service.signed_in(), "poller_enabled": service.poller_enabled(),
            "connections": service.list_connections()}


@router.get("/api/connections/{toolkit}")
def get_connection(toolkit: str) -> dict:
    return service.get_connection(toolkit)


@router.put("/api/connections/{toolkit}")
def configure_connection(toolkit: str, body: ConfigureBody) -> dict:
    return service.configure(toolkit, **body.given())


@router.delete("/api/connections/{toolkit}")
def remove_connection(toolkit: str) -> dict:
    return {"toolkit": toolkit, "removed": service.remove(toolkit)}


@router.post("/api/connections/{toolkit}/poll")
async def poll_connection_now(toolkit: str) -> dict:
    return await service.poll_now(toolkit)


@router.post("/api/connections/{toolkit}/account")
async def refresh_connection_account(toolkit: str) -> dict:
    return await service.refresh_account(toolkit)


@router.get("/api/connections/{toolkit}/events")
def list_connection_events(toolkit: str, limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"toolkit": toolkit, "events": service.events(toolkit, limit=limit)}
