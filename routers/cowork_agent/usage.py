"""Usage for the Space's agent (``/api/usage/*``) and each runtime by name; the legacy alias binds these handlers."""

from types import ModuleType
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from routers.runtime_scope import PREFIX, known_runtime, unsupported
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine.usage_loader import load_usage_module

_routes = APIRouter()


def _usage(request: Request) -> ModuleType:
    """The usage module of the runtime the path names, else of the Space's agent."""
    name = request.path_params.get("name")
    if name:
        mod = try_load_capability("usage", agent=name)
        if mod is None:
            raise unsupported(name, "usage")
        return mod
    try:
        return load_usage_module()
    except ModuleNotFoundError as e:
        raise HTTPException(
            status_code=501,
            detail=f"no usage module for active agent (tried services.cowork_agent.adapters.<AGENT_NAME>.usage): {e}",
        )


def _window_from_query(days: Optional[int], start: Optional[str],
                       end: Optional[str], tz: str) -> dict:
    """Translate HTTP query params into the uniform module window shape.

    Explicit ``start`` + ``end`` take precedence; otherwise ``days``
    (default 30) becomes a rolling gateway-aligned window.
    """
    if start or end:
        if not (start and end):
            raise HTTPException(400, "start and end must be passed together")
        return {"start": start, "end": end, "tz": tz}
    return {"days": days if days is not None else 30, "tz": tz}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@_routes.get("/usage")
def usage_dashboard(
    days: int = Query(30, description="Number of days to include (default 30)"),
    tz: Optional[str] = Query(None, description="Day-bucket timezone: 'local' (default, host TZ) or 'utc'."),
    usage: ModuleType = Depends(_usage),
):
    """Aggregated UsageStats for the active agent. The shape the frontend
    Settings → Usage tab consumes."""
    days = max(1, min(days, 365))
    tz_resolved = tz if tz in ("local", "utc") else "local"
    return usage.dashboard(window={"days": days, "tz": tz_resolved})


@_routes.get("/usage/analytics")
def usage_analytics(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
    usage: ModuleType = Depends(_usage),
):
    """Time-series dashboard payload: stat cards + per-day cost/tokens +
    per-day messages + per-day performance + per-tool counts + per-model totals."""
    return usage.analytics(window=_window_from_query(days, start, end, tz))


@_routes.get("/usage/summary")
def usage_summary(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
    usage: ModuleType = Depends(_usage),
):
    """Aggregated SessionCostSummary across all sessions in the window, plus
    per-session sub-summaries in ``sessions[]``."""
    return usage.summary(window=_window_from_query(days, start, end, tz))


@_routes.get("/usage/summary/card")
def usage_summary_card(
    days: int = Query(5, description="Number of days to include (default 5)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
    usage: ModuleType = Depends(_usage),
):
    """Lightweight headline card: totals + per-day cost/tokens/assistant-msgs."""
    return usage.summary_card(window={"days": days, "tz": tz})


@_routes.get("/usage/sessions")
def usage_sessions(usage: ModuleType = Depends(_usage)):
    """List every discovered session with basic metadata.
    ``messageCount`` is assistant-only (preserved legacy contract)."""
    return usage.list_sessions()


@_routes.get("/usage/sessions/{session_id}")
def usage_session(
    session_id: str,
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
    usage: ModuleType = Depends(_usage),
):
    """Detailed SessionCostSummary for one session, optionally windowed."""
    window = None
    if start or end:
        window = _window_from_query(None, start, end, tz)
    result = usage.get_session(session_id, window=window)
    if result is None:
        raise HTTPException(404, f"Session {session_id} not found")
    return result


router = APIRouter(tags=["usage"])
router.include_router(_routes, prefix="/api")
router.include_router(_routes, prefix=PREFIX, dependencies=[Depends(known_runtime)])
