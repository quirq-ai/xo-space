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

Thin over services.connections.service (typed errors become
HTTP through ``bff/errors.py``). The toolkit path value is checked against the id shape before
any service call so an unknown id is a 404 with {code, message} and never
reaches the filesystem. Bodies are strict: a string where an int or a bool
belongs is a 422, the same as an unknown key. No os/pathlib in this module
(BFF rule P2).
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import StrictBool, StrictInt, StrictStr

from services.connections import service

from routers.errors import ForbidExtra, http_error

router = APIRouter()

TOOLKIT_RE = re.compile(r"[a-z0-9_]{1,40}")


class ConfigureBody(ForbidExtra):
    enabled: Optional[StrictBool] = None
    interval_s: Optional[StrictInt] = None
    collectors: Optional[list[StrictStr]] = None


def _toolkit_or_404(toolkit: str) -> str:
    if not isinstance(toolkit, str) or not TOOLKIT_RE.fullmatch(toolkit):
        raise HTTPException(status_code=404, detail={"code": "unknown_toolkit", "message": "Unknown toolkit."})
    return toolkit


@router.get("")
def list_connections() -> dict:
    try:
        return {"signed_in": service.signed_in(), "poller_enabled": service.poller_enabled(),
                "connections": service.list_connections()}
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.get("/{toolkit}")
def get_connection(toolkit: str) -> dict:
    try:
        return service.get_connection(_toolkit_or_404(toolkit))
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.put("/{toolkit}")
def configure_connection(toolkit: str, body: ConfigureBody) -> dict:
    fields = {name: value for name, value in body.model_dump().items() if value is not None}
    try:
        return service.configure(_toolkit_or_404(toolkit), **fields)
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.delete("/{toolkit}")
def remove_connection(toolkit: str) -> dict:
    try:
        return {"toolkit": toolkit, "removed": service.remove(_toolkit_or_404(toolkit))}
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.post("/{toolkit}/poll")
async def poll_connection_now(toolkit: str) -> dict:
    try:
        return await service.poll_now(_toolkit_or_404(toolkit))
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.post("/{toolkit}/account")
async def refresh_connection_account(toolkit: str) -> dict:
    try:
        return await service.refresh_account(_toolkit_or_404(toolkit))
    except service.ConnectionsError as exc:
        raise http_error(exc)


@router.get("/{toolkit}/events")
def list_connection_events(toolkit: str, limit: int = Query(50, ge=1, le=500)) -> dict:
    try:
        return {"toolkit": toolkit, "events": service.events(_toolkit_or_404(toolkit), limit=limit)}
    except service.ConnectionsError as exc:
        raise http_error(exc)
