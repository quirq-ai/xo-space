"""The telemetry module's routes: usage, session telemetry and its sources.

  GET /api/usage?days=&tz=                                UsageStats for the active agent
  GET /api/usage/analytics?days=|start=&end=&tz=          the time-series dashboard
  GET /api/usage/summary?days=|start=&end=&tz=            SessionCostSummary across every session
  GET /api/usage/summary/card?days=&tz=                   the headline card
  GET /api/usage/sessions                                 every discovered session
  GET /api/usage/sessions/{session_id}?start=&end=&tz=    one session
  GET /api/xo-projects/{id}/usage/summary/card?days=      one project: the headline card
  GET /api/xo-projects/{id}/usage/analytics?days=|start=&end=
  GET /api/xo-projects/{id}/usage/sessions?agent_id=
  GET /api/xo-projects/{id}/usage/summary?days=|start=&end=
  GET /api/xo-projects/{id}/usage/sessions/{session_id}
  GET /api/xo-projects/usage?days=                        every project combined: UsageStats
  GET /api/xo-projects/usage/summary/card?days=
  GET /api/xo-projects/usage/analytics?days=|start=&end=
  GET /api/xo-projects/usage/sessions
  GET /api/xo-projects/usage/summary?days=|start=&end=
  GET /api/xo-projects/usage/sessions/{session_id}
  GET /api/telemetry/sources                              every telemetry provider, its data path and switch
  PUT /api/telemetry/sources/{id}                         {path?, enabled?}; rebuilds sessions.json off the request
  GET /xo/sessions.json                                   session telemetry merged across every runtime

Thin over ``modules.telemetry.service``: every typed failure reaches the
wire through the app's service error handler (``routers/errors.py``) with
the code and status it always had. The handlers keep the names the legacy
``/openclaw/usage/*`` alias router binds (``routers/cowork_agent/usage.py``
re-exports them). No os/pathlib in this module (BFF rule P2).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from routers.errors import ForbidExtra

from . import service
from .models import (
    SessionCostSummary,
    SessionListResponse,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
)

router = APIRouter(tags=["telemetry"])

_NO_STORE = {"Cache-Control": "no-store"}


# ── /api/usage: the active agent ─────────────────────────────────────────────


@router.get("/api/usage")
def usage_dashboard(
    days: int = Query(30, description="Number of days to include (default 30)"),
    tz: Optional[str] = Query(None, description="Day-bucket timezone: 'local' (default, host TZ) or 'utc'."),
):
    """Aggregated UsageStats for the active agent.

    The shape the frontend's Settings, Usage tab consumes."""
    return service.usage_dashboard(days, tz)


@router.get("/api/usage/analytics")
def usage_analytics(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """The active agent's time-series dashboard payload.

    Stat cards, per-day cost and tokens, per-day messages, per-day
    performance, per-tool counts and per-model totals."""
    return service.usage_analytics(days=days, start=start, end=end, tz=tz)


@router.get("/api/usage/summary")
def usage_summary(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """The active agent's SessionCostSummary across every session in the window.

    Per-session sub-summaries ride along in ``sessions[]``."""
    return service.usage_summary(days=days, start=start, end=end, tz=tz)


@router.get("/api/usage/summary/card")
def usage_summary_card(
    days: int = Query(5, description="Number of days to include (default 5)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Lightweight headline card: totals plus per-day cost, tokens and assistant messages."""
    return service.usage_summary_card(days=days, tz=tz)


@router.get("/api/usage/sessions")
def usage_sessions():
    """List every discovered session with basic metadata.
    ``messageCount`` is assistant-only (preserved legacy contract)."""
    return service.usage_sessions()


@router.get("/api/usage/sessions/{session_id}")
def usage_session(
    session_id: str,
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Detailed SessionCostSummary for one session, optionally windowed."""
    return service.usage_session(session_id, start=start, end=end, tz=tz)


# ── /api/xo-projects/{id}/usage: one project ─────────────────────────────────


@router.get("/api/xo-projects/{project_id}/usage/summary/card", response_model=UsageSummaryCardResponse)
def project_usage_summary_card(
    project_id: str,
    days: int = Query(5, ge=1, le=365),
) -> UsageSummaryCardResponse:
    """Lightweight usage widget for one project."""
    return service.project_usage_summary_card(project_id, days=days)


@router.get("/api/xo-projects/{project_id}/usage/analytics", response_model=UsageAnalyticsResponse)
def project_usage_analytics(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Per-project analytics dashboard, the ``/api/usage/analytics`` shape."""
    return service.project_usage_analytics(project_id, days=days, start=start, end=end)


@router.get("/api/xo-projects/{project_id}/usage/sessions", response_model=SessionListResponse)
def project_usage_sessions(
    project_id: str,
    agent_id: Optional[str] = Query(None),
) -> SessionListResponse:
    """List sessions for one project, the ``/api/usage/sessions`` shape."""
    return service.project_usage_sessions(project_id, agent_id=agent_id)


@router.get("/api/xo-projects/{project_id}/usage/summary", response_model=SessionCostSummary)
def project_usage_summary(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """The aggregate SessionCostSummary across one project's sessions.

    A per-session sub-summary rides along in ``sessions[]``."""
    return service.project_usage_summary(project_id, days=days, start=start, end=end)


@router.get("/api/xo-projects/{project_id}/usage/sessions/{session_id}", response_model=SessionCostSummary)
def project_usage_one_session(project_id: str, session_id: str) -> SessionCostSummary:
    """One session of one project, by composite key or nativeSessionId."""
    return service.project_usage_session(project_id, session_id)


# ── /api/xo-projects/usage: every project combined ───────────────────────────


@router.get("/api/xo-projects/usage")
def workspace_usage_dashboard(days: int = Query(30, ge=1, le=365)) -> dict:
    """Workspace-aggregated ``UsageStats`` (the ``/api/usage`` shape)."""
    return service.workspace_usage_dashboard(days=days)


@router.get("/api/xo-projects/usage/summary/card", response_model=UsageSummaryCardResponse)
def workspace_usage_summary_card(days: int = Query(5, ge=1, le=365)) -> UsageSummaryCardResponse:
    """Workspace-wide usage card (all projects combined)."""
    return service.workspace_usage_summary_card(days=days)


@router.get("/api/xo-projects/usage/analytics", response_model=UsageAnalyticsResponse)
def workspace_usage_analytics(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Workspace-wide analytics dashboard, the ``/api/usage/analytics`` shape."""
    return service.workspace_usage_analytics(days=days, start=start, end=end)


@router.get("/api/xo-projects/usage/sessions", response_model=SessionListResponse)
def workspace_usage_sessions() -> SessionListResponse:
    """Every session across every project, each row tagged with its projectId."""
    return service.workspace_usage_sessions()


@router.get("/api/xo-projects/usage/summary", response_model=SessionCostSummary)
def workspace_usage_summary(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """The aggregate SessionCostSummary over every project.

    ``sessionId`` is ``"all-projects"`` and ``sessions[]`` lists every
    session of every project."""
    return service.workspace_usage_summary(days=days, start=start, end=end)


@router.get("/api/xo-projects/usage/sessions/{session_id}", response_model=SessionCostSummary)
def workspace_usage_one_session(session_id: str) -> SessionCostSummary:
    """One session anywhere in the workspace, by composite key or nativeSessionId."""
    return service.workspace_usage_session(session_id)


# ── /api/telemetry/sources ───────────────────────────────────────────────────


class SourceUpdate(ForbidExtra):
    path: Optional[str] = None
    enabled: Optional[bool] = None


def _rebuild_sessions_view() -> None:
    service.rebuild_sessions_view()


@router.get("/api/telemetry/sources")
async def list_sources():
    """Every telemetry provider with its effective data path and switch."""
    items = await asyncio.to_thread(service.list_sources)
    return JSONResponse({"items": items, "total": len(items)}, headers=_NO_STORE)


@router.put("/api/telemetry/sources/{source_id}")
async def update_source(source_id: str, body: SourceUpdate):
    """Save a source's path (empty clears the override) and/or flip its collection.

    The sessions view is rebuilt off the request path so the next Refresh
    reflects the change."""
    source = await asyncio.to_thread(service.save_source, source_id, path=body.path, enabled=body.enabled)
    asyncio.get_running_loop().run_in_executor(None, _rebuild_sessions_view)
    return JSONResponse({"item": source, "rebuilding": True}, headers=_NO_STORE)


# ── /xo/sessions.json ────────────────────────────────────────────────────────


@router.get("/xo/sessions.json")
async def sessions_json():
    """Session telemetry merged across every runtime that reports it."""
    return JSONResponse(await service.sessions_view(), headers=_NO_STORE)
