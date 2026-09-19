"""
Canonical usage router — `/api/usage/*`.

Body is pure URL → module-method dispatch. The active agent is resolved by
``services.cowork_agent.engine.usage_loader.load_usage_module()`` (single
``importlib.import_module`` call, no if/elif), and every endpoint forwards
into that module's view method. Zero agent-specific code lives here.

The legacy ``/openclaw/usage/*`` URLs are kept as backward-compat aliases
in ``api/usage/legacy.py``; they bind to the **same handler
functions** defined below, so the two prefixes return byte-identical JSON.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.cowork_agent.engine.usage_loader import load_usage_module

router = APIRouter(tags=["usage"])


def _load_or_501():
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


@router.get("")
def usage_dashboard(
    days: int = Query(30, description="Number of days to include (default 30)"),
    tz: Optional[str] = Query(None, description="Day-bucket timezone: 'local' (default, host TZ) or 'utc'."),
):
    """Aggregated UsageStats for the active agent. The shape the frontend
    Settings → Usage tab consumes."""
    days = max(1, min(days, 365))
    tz_resolved = tz if tz in ("local", "utc") else "local"
    return _load_or_501().dashboard(window={"days": days, "tz": tz_resolved})


@router.get("/analytics")
def usage_analytics(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Time-series dashboard payload: stat cards + per-day cost/tokens +
    per-day messages + per-day performance + per-tool counts + per-model totals."""
    return _load_or_501().analytics(window=_window_from_query(days, start, end, tz))


@router.get("/summary")
def usage_summary(
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Aggregated SessionCostSummary across all sessions in the window, plus
    per-session sub-summaries in ``sessions[]``."""
    return _load_or_501().summary(window=_window_from_query(days, start, end, tz))


@router.get("/summary/card")
def usage_summary_card(
    days: int = Query(5, description="Number of days to include (default 5)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Lightweight headline card: totals + per-day cost/tokens/assistant-msgs."""
    return _load_or_501().summary_card(window={"days": days, "tz": tz})


@router.get("/sessions")
def usage_sessions():
    """List every discovered session with basic metadata.
    ``messageCount`` is assistant-only (preserved legacy contract)."""
    return _load_or_501().list_sessions()


@router.get("/sessions/{session_id}")
def usage_session(
    session_id: str,
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tz: str = Query("local", description="Day-bucket timezone: 'local' or 'utc'"),
):
    """Detailed SessionCostSummary for one session, optionally windowed."""
    window = None
    if start or end:
        window = _window_from_query(None, start, end, tz)
    result = _load_or_501().get_session(session_id, window=window)
    if result is None:
        raise HTTPException(404, f"Session {session_id} not found")
    return result
