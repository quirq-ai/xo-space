"""Workspace-scope BFF endpoints over ``~/xo-projects/.xo/``.

Aggregate-of-all-projects view. Same wire contract as the
project-scope endpoints; the aggregation is "union of all per-project
files" done by the watcher. Per-project ``sessionslist.json`` is the
fallback when a workspace-tier file isn't present yet.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AnalyticsStats,
    CostAndTokensEntry,
    DailyCostEntry,
    MessageCounts,
    MessagesEntry,
    ModelUsageEntry,
    ModelUsageWithTotals,
    OpenSession,
    PerformanceEntry,
    SessionCostSummary,
    SessionListItem,
    SessionListResponse,
    TimelineEvent,
    TimelineResponse,
    TokenTotals,
    ToolUsage,
    ToolUsageEntry,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
    Workitem,
    _ForbidExtra,
)
from routers.cowork_agent.bff._visualizer_presenter import (
    TIMELINE_TYPES as _TIMELINE_TYPES,
    avg_latency_ms_from_by_day as _avg_latency_ms_from_by_day,
    bad_query as _bad_query,
    by_day_from_stats as _by_day_from_stats,
    cost_and_tokens_for_dates as _cost_and_tokens_for_dates,
    date_from_ms as _date_from_ms,
    messages_for_dates as _messages_for_dates,
    model_call_counts_from_by_day as _model_call_counts_from_by_day,
    model_usage_entries as _model_usage_entries,
    model_usage_with_totals as _model_usage_with_totals,
    parse_types_param as _parse_types_param,
    performance_entry_for_day as _performance_entry_for_day,
    performance_for_dates as _performance_for_dates,
    provider_for_model as _provider_for_model,
    rolling_key_for as _rolling_key_for,
    row_total_tokens as _row_total_tokens,
    tokens_from_stats as _tokens_from_stats,
    tool_usage_from_stats as _tool_usage_from_stats,
    zero_filled_dates as _zero_filled_dates,
)
# The workspace rollup borrows five names from the project-tier router rather
# than restating them (workitems-plan §7.3, W9).
from routers.cowork_agent.bff.visualizer import (
    _SELF_ALIASES,
    _DOCUMENT_ERRORS,
    _make_workitem_model,
    _self_github_login,
    _self_identities,
)
from services.cowork_agent import scopes
from services.cowork_agent.visualizer.workitems_store import (
    VALID_STATUSES as _WORKITEM_STATUSES,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _all_projects() -> list[str]:
    """Names of every project the watcher tracks (scaffolded + bare).

    Workspace endpoints fan out over this list. We mirror the
    watcher's ``workspace_index.list_project_ids`` to keep BFF
    visibility aligned with what the watcher writes — otherwise a
    bare project with adapter-written ``sessionslist.json`` (the
    chat router can create one before scaffolding completes) would
    be invisible to the workspace endpoints."""
    return list_project_ids()


def _union_sessionslist() -> dict[str, dict]:
    """Union of every project's merged ``sessionslist`` map.

    Keys remain composite (e.g. ``claude:blackhole:web:67a1ac06``);
    duplicate keys across projects are impossible because the
    composite key embeds the agent id, and adapters allocate unique
    8-hex suffixes.
    """
    merged: dict[str, dict] = {}
    for pid in _all_projects():
        scope = scopes.resolve_scope("xo-projects-visualizer", pid)
        rows = scope.read_sessionslist()
        for key, row in rows.items():
            # Tag the row with its project for the workspace endpoints.
            r = dict(row)
            r["_project_id"] = pid
            merged[key] = r
    return merged


def _response_time_stats_from_by_day(by_day: dict[str, dict]) -> dict:
    """Aggregate every day's latency samples into the ``ResponseTimeStats``
    shape the FE expects (seconds, not ms).

    Concatenates each day's bounded reservoir into one pool, then
    derives min/median/avg/p95/max from the pool. Gives the dashboard
    the same shape the canonical ``/api/usage`` returns, so the
    "Response Time" card on the xo-coworker FE finds the fields it's
    looking for instead of N/A.
    """
    pool: list[int] = []
    sum_ms = 0
    count = 0
    min_ms_overall: Optional[int] = None
    max_ms_overall = 0
    for day in by_day.values():
        if not isinstance(day, dict):
            continue
        lat = day.get("latency") or {}
        if not isinstance(lat, dict):
            continue
        c = int(lat.get("count", 0) or 0)
        if c <= 0:
            continue
        count += c
        sum_ms += int(lat.get("sum_ms", 0) or 0)
        dmin = int(lat.get("min_ms", 0) or 0)
        dmax = int(lat.get("max_ms", 0) or 0)
        if dmin > 0 and (min_ms_overall is None or dmin < min_ms_overall):
            min_ms_overall = dmin
        if dmax > max_ms_overall:
            max_ms_overall = dmax
        sample = lat.get("p95_sample") or []
        if isinstance(sample, list):
            pool.extend(int(x) for x in sample if isinstance(x, (int, float)))
    if count == 0:
        return {"avg": 0.0, "median": 0.0, "p95": 0.0,
                "min": 0.0, "max": 0.0, "count": 0}
    avg_s = (sum_ms / count) / 1000.0
    pool.sort()
    if pool:
        median_s = pool[len(pool) // 2] / 1000.0
        p95_s = pool[max(0, int(0.95 * (len(pool) - 1)))] / 1000.0
    else:
        median_s = 0.0
        p95_s = 0.0
    return {
        "avg":    round(avg_s, 3),
        "median": round(median_s, 3),
        "p95":    round(p95_s, 3),
        "min":    round((min_ms_overall or 0) / 1000.0, 3),
        "max":    round(max_ms_overall / 1000.0, 3),
        "count":  count,
    }


# ── /api/xo-projects/usage — UsageStats-shaped aggregate ─────────────────────
#
# Mirrors the canonical ``/api/usage`` response shape so the xo-coworker
# dashboard's ``useUsage`` hook can consume the workspace-aggregated
# picture (across every xo-project) instead of the global active-agent
# picture.

@router.get("/api/xo-projects/usage")
def workspace_usage_dashboard(
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """Workspace-aggregated ``UsageStats`` (UI-facing shape)."""
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    sessionslist = _union_sessionslist()

    # ── Total tokens — mixed-source by design ──
    # input / output come from the watcher's view (stats.rolling.<window>.tokens)
    # so they line up byte-for-byte with the Model Usage card below.
    # The cowork-api adapter's sessionslist.usage sum is more partial
    # (misses CLI-direct chats and streaming-chunk dedup), which caused
    # the Token Breakdown "Output 238.5K" vs Model Usage "282.7K" mismatch.
    # cache_read / cache_write stay from the sessionslist sum because the
    # watcher's all-time view doesn't currently track cache classes in
    # _session_totals — separate follow-up.
    rolling = (stats.get("rolling") or {}).get(_rolling_key_for(days)) or {}
    roll_tokens = rolling.get("tokens") or {}
    tot_in = int(roll_tokens.get("input", 0) or 0)
    tot_out = int(roll_tokens.get("output", 0) or 0)
    tot_cr = tot_cw = 0
    tot_msg = 0
    for row in sessionslist.values():
        usage = row.get("usage") or {}
        tot_cr += int(usage.get("cache_read_input_tokens", 0) or 0)
        tot_cw += int(usage.get("cache_creation_input_tokens", 0) or 0)
        tot_msg += int(row.get("messageCount", 0) or 0)

    total_sessions = len(sessionslist)
    total_tokens_sum = tot_in + tot_out + tot_cr + tot_cw
    avg_tokens_per_session = (
        round(total_tokens_sum / total_sessions, 2) if total_sessions else 0.0
    )

    # ── by_model (from the same workspace stats.json rolling.<window> block) ──
    # Tokens come from rolling.<window>.by_model (windowed, watcher-accurate).
    # message_count comes from per-day by_model summed across the same window
    # — rolling doesn't carry call counts, per-day does.
    by_model_raw = rolling.get("by_model") or {}
    by_day = _by_day_from_stats(stats)
    model_dates = _zero_filled_dates(days)
    model_call_counts = _model_call_counts_from_by_day(by_day, model_dates)
    by_model: list[dict] = []
    for model, t in by_model_raw.items():
        if not isinstance(t, dict):
            continue
        m_in = int(t.get("input", 0) or 0)
        m_out = int(t.get("output", 0) or 0)
        if m_in + m_out <= 0:
            continue  # skip <synthetic> and other zero-token rows
        by_model.append({
            "model_id": str(model),
            "provider_id": _provider_for_model(str(model)),
            "total_cost": 0.0,  # no pricing table
            "total_tokens": {
                "input": m_in,
                "output": m_out,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            "message_count": int(model_call_counts.get(str(model), 0)),
        })
    by_model.sort(key=lambda m: m["total_tokens"]["input"] + m["total_tokens"]["output"], reverse=True)

    # ── by_session (top 10 by tokens) ──
    session_rows: list[dict] = []
    for composite_key, row in sessionslist.items():
        usage = row.get("usage") or {}
        total = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        first = row.get("firstActivity") or row.get("updatedAt")
        time_created = (
            datetime.fromtimestamp(first / 1000, tz=timezone.utc).isoformat()
            if isinstance(first, int) and first > 0 else ""
        )
        session_rows.append({
            "session_id": composite_key,
            "title": row.get("_project_id") or composite_key,
            "total_cost": 0.0,
            "total_tokens": total,
            "message_count": int(row.get("messageCount", 0) or 0),
            "time_created": time_created,
        })
    session_rows.sort(key=lambda s: s["total_tokens"], reverse=True)
    by_session = session_rows[:10]

    # ── daily (per-event-date accurate) ──
    # Reads the watcher-aggregated workspace by_day block. Falls back
    # to all-zero entries when no by_day data is present (schema 1
    # files or a fresh deployment that hasn't ticked yet).
    by_day = _by_day_from_stats(stats)
    daily: list[dict] = []
    for d in _zero_filled_dates(days):
        day = by_day.get(d) or {}
        tk = (day.get("tokens") or {}) if isinstance(day, dict) else {}
        msgs = (day.get("messages") or {}) if isinstance(day, dict) else {}
        daily.append({
            "date": d,
            "cost": 0.0,
            "tokens": int(tk.get("input", 0) or 0) + int(tk.get("output", 0) or 0),
            "messages": int(msgs.get("total", 0) or 0),
        })

    # Latency from the workspace by_day.latency blocks the watcher
    # writes. Aggregated across every project's samples (already
    # merged at the workspace tier). Reported in seconds to match the
    # canonical /api/usage UsageStats shape this endpoint mirrors.
    rt_stats = _response_time_stats_from_by_day(by_day)

    return {
        "total_cost": 0.0,
        "total_tokens": {
            "input": tot_in,
            "output": tot_out,
            "reasoning": 0,
            "cache_read": tot_cr,
            "cache_write": tot_cw,
        },
        "total_sessions": total_sessions,
        "total_messages": tot_msg,
        "avg_tokens_per_session": avg_tokens_per_session,
        "avg_response_time": rt_stats["avg"],
        "by_model": by_model,
        "by_session": by_session,
        "daily": daily,
        "response_time": rt_stats,
    }


# ── /api/xo-projects/usage/summary/card ──────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/summary/card",
    response_model=UsageSummaryCardResponse,
)
def workspace_usage_summary_card(
    days: int = Query(5, ge=1, le=365),
) -> UsageSummaryCardResponse:
    """Workspace-wide usage card (all projects combined).

    Token totals come from the watcher's workspace ``stats.json``
    (input + output, no cache_read / cache_creation) — same number
    users see in the UI. Message totals come from each session's
    augment counter summed. Daily breakdown is single-bucket by
    session ``updatedAt`` (coarser than the per-event by_day rollup).
    """
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, days)

    sessionslist = _union_sessionslist()
    total_messages = 0
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        msgs = int(row.get("messageCount", 0) or 0)
        total_messages += msgs
        d = _date_from_ms(row.get("updatedAt"))
        if d is not None:
            usage = row.get("usage") or {}
            tk_row = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
            b = buckets.setdefault(d, {"cost": 0.0, "tokens": 0, "messages": 0})
            b["tokens"] += tk_row
            b["messages"] += msgs

    daily = [
        DailyCostEntry(
            date=d,
            cost=round(float((buckets.get(d) or {"cost": 0.0})["cost"]), 6),
            tokens=int((buckets.get(d) or {"tokens": 0})["tokens"]),
            messages=int((buckets.get(d) or {"messages": 0})["messages"]),
        )
        for d in _zero_filled_dates(days)
    ]
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # no pricing table
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=daily,
    )


# ── /api/xo-projects/usage/analytics ─────────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/analytics",
    response_model=UsageAnalyticsResponse,
)
def workspace_usage_analytics(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Workspace-wide analytics dashboard. Mirrors
    ``/openclaw/usage/analytics`` shape verbatim."""
    # Date-range parse — keep error codes consistent with
    # routers/openclaw_usage.py.
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    window_days = days or 5
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, window_days)

    sessionslist = _union_sessionslist()
    total_messages = sum(int(r.get("messageCount", 0) or 0) for r in sessionslist.values())

    dates = _zero_filled_dates(window_days)
    by_day = _by_day_from_stats(stats)

    return UsageAnalyticsResponse(
        stats=AnalyticsStats(
            totalCost=0.0,
            totalTokens=total_tokens,
            totalMessages=total_messages,
            avgLatencyMs=_avg_latency_ms_from_by_day(by_day),
        ),
        costAndTokens=_cost_and_tokens_for_dates(by_day, dates),
        messages=_messages_for_dates(by_day, dates),
        performance=_performance_for_dates(by_day, dates),
        toolUsage=_tool_usage_from_stats(stats, window_days),
        modelUsage=_model_usage_entries(stats, window_days),
    )


# ── /api/xo-projects/usage/sessions ──────────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/sessions",
    response_model=SessionListResponse,
)
def workspace_usage_sessions() -> SessionListResponse:
    """Workspace-wide session list.

    One row per session across all projects; each row tagged with
    ``projectId``. Mirrors ``/openclaw/usage/sessions`` shape with the
    extra ``projectId`` discriminator.
    """
    sessionslist = _union_sessionslist()

    items: list[SessionListItem] = []
    for composite_key, row in sessionslist.items():
        usage = row.get("usage") or {}
        total_tokens = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        # ``nativeSessionId`` → ``sessionFile`` (basename, no path — P1).
        native = row.get("nativeSessionId") or ""
        session_file = f"{native}.jsonl" if native else ""
        items.append(
            SessionListItem(
                sessionId=composite_key,
                sessionFile=session_file,
                messageCount=int(row.get("messageCount", 0) or 0),
                totalTokens=total_tokens,
                totalCost=0.0,
                firstActivity=row.get("firstActivity"),
                lastActivity=row.get("lastActivity") or row.get("updatedAt"),
                projectId=row.get("_project_id"),
            )
        )

    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=None, count=len(items), sessions=items)


# ── /api/xo-projects/usage/summary ───────────────────────────────────────────


def _row_to_summary(
    composite_key: str, row: dict, *, stats: Optional[dict] = None,
) -> SessionCostSummary:
    usage = row.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    native = row.get("nativeSessionId") or ""

    # Per-session tool tally + model breakdown from workspace stats —
    # workspace `by_session` passes per-project rows through verbatim,
    # so the same lookup we use per-project applies here.
    tool_usage = ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
    model_usage: list = []
    if stats is not None and native:
        by_session = stats.get("by_session") or {}
        sess = by_session.get(native) if isinstance(by_session, dict) else None
        if isinstance(sess, dict):
            tools_raw = sess.get("tools")
            if isinstance(tools_raw, dict):
                entries = [
                    ToolUsageEntry(name=str(n), count=int(c or 0))
                    for n, c in tools_raw.items() if int(c or 0) > 0
                ]
                entries.sort(key=lambda t: t.count, reverse=True)
                tool_usage = ToolUsage(
                    totalCalls=sum(t.count for t in entries),
                    uniqueTools=len(entries),
                    tools=entries,
                )
            # by_model is intentionally not wired into modelUsage here —
            # the workspace summary's per-session shape doesn't currently
            # carry it; would need the same ModelUsageWithTotals wiring
            # the per-project BFF has.

    # Read role split from augment row's messageCountByRole;
    # legacy schema 1 rows degrade to zeros.
    by_role_raw = row.get("messageCountByRole")
    by_role = by_role_raw if isinstance(by_role_raw, dict) else {}
    return SessionCostSummary(
        sessionId=composite_key,
        sessionFile=f"{native}.jsonl" if native else "",
        firstActivity=row.get("firstActivity") or row.get("updatedAt"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        messageCounts=MessageCounts(
            total=int(row.get("messageCount", 0) or 0),
            user=int(by_role.get("user", 0) or 0),
            assistant=int(by_role.get("assistant", 0) or 0),
            toolCalls=int(row.get("toolCallCount", 0) or 0),
            toolResults=int(by_role.get("toolResults", 0) or 0),
            errors=int(by_role.get("errors", 0) or 0),
        ),
        toolUsage=tool_usage,
        modelUsage=model_usage,
    )


@router.get(
    "/api/xo-projects/usage/summary",
    response_model=SessionCostSummary,
)
def workspace_usage_summary(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """Workspace-wide aggregate ``SessionCostSummary``.

    Mirrors ``/openclaw/usage/summary`` shape. ``sessionId`` is the
    sentinel ``"all-projects"``; ``sessions[]`` lists every session
    across every project.
    """
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    summary_window_days = days or 30

    sessionslist = _union_sessionslist()
    per_session = [_row_to_summary(k, r, stats=stats) for k, r in sessionslist.items()]
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
    # Sum the per-session role-split into the aggregate.
    agg_msg = MessageCounts(
        total=sum(s.messageCounts.total for s in per_session),
        user=sum(s.messageCounts.user for s in per_session),
        assistant=sum(s.messageCounts.assistant for s in per_session),
        toolCalls=sum(s.messageCounts.toolCalls for s in per_session),
        toolResults=sum(s.messageCounts.toolResults for s in per_session),
        errors=sum(s.messageCounts.errors for s in per_session),
    )

    return SessionCostSummary(
        sessionId="all-projects",
        sessionFile=f"{len(per_session)} files",
        firstActivity=min((s.firstActivity for s in per_session if s.firstActivity), default=None),
        lastActivity=max((s.lastActivity for s in per_session if s.lastActivity), default=None),
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        messageCounts=agg_msg,
        toolUsage=_tool_usage_from_stats(stats, summary_window_days),
        modelUsage=_model_usage_with_totals(stats, summary_window_days),
        sessionCount=len(per_session),
        sessions=per_session,
    )


# ── /api/xo-projects/usage/sessions/{session_id} ─────────────────────────────


@router.get(
    "/api/xo-projects/usage/sessions/{session_id}",
    response_model=SessionCostSummary,
)
def workspace_usage_one_session(session_id: str) -> SessionCostSummary:
    """Single-session detail across the whole workspace.

    Scans every project's sessionslist until a match is found by
    composite key or ``nativeSessionId``.
    """
    for pid in _all_projects():
        scope = scopes.resolve_scope("xo-projects-visualizer", pid)
        found = scope.read_one_session(session_id)
        if found is not None:
            composite_key, row = found
            # Use the owning project's stats so tools/by_model lookups
            # find the right by_session entry (workspace stats also
            # contain it, but per-project is cheaper and equivalent).
            stats = scope.read_stats() or {}
            return _row_to_summary(composite_key, row, stats=stats)
    raise HTTPException(
        status_code=404,
        detail={"code": "session_not_found", "message": "Session not found."},
    )


# ── /api/xo-projects/activity ────────────────────────────────────────────────


@router.get(
    "/api/xo-projects/activity",
    response_model=ActivityResponse,
)
def workspace_activity() -> ActivityResponse:
    """Workspace-wide live presence — union of every project's open sessions.

    Empty when no project has live presence yet. Each open-session
    row carries ``project_id`` so the UI can group by project.
    """
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    raw = workspace.read_activity() or {}
    open_sessions: list[OpenSession] = []
    for s in raw.get("open_sessions") or []:
        if not isinstance(s, dict):
            continue
        try:
            open_sessions.append(
                OpenSession(
                    session_id=str(s["session_id"]),
                    runtime=s.get("runtime"),
                    agent=str(s["agent"]),
                    user_id=str(s["user_id"]),
                    opened_at=str(s["opened_at"]),
                    last_activity_at=str(s["last_activity_at"]),
                    host=s.get("host"),
                    project_id=s.get("project_id"),
                )
            )
        except (KeyError, ValueError):
            continue
    return ActivityResponse(
        project_id=None,
        updated_at=raw.get("updated_at"),
        open_sessions=open_sessions,
    )


# ── /api/xo-projects/timeline ────────────────────────────────────────────────


@router.get(
    "/api/xo-projects/timeline",
    response_model=TimelineResponse,
)
def workspace_timeline(
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Multiplexed workspace timeline. Each event tagged with ``project_id``.

    Reads from ``~/xo-projects/.xo/timeline.jsonl``. Empty if the
    workspace tier hasn't materialised it yet.
    """
    if before is not None:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _bad_query("before must be an ISO-8601 timestamp") from exc

    type_set = _parse_types_param(types)
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    events = workspace.read_timeline(limit=limit, before=before, types=type_set)

    out: list[TimelineEvent] = []
    for ev in events:
        try:
            out.append(TimelineEvent(**ev))
        except Exception:
            continue

    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=None, events=out, next_cursor=next_cursor)


# ── /api/workspace/workitems — the agent-pingable rollup ─────────────────────
# workitems-plan §7.3, task W9. The endpoint an agent polls to answer "what is
# assigned to me", across every project on this machine, in one call.


class WorkspaceWorkitem(Workitem):
    """One rollup row: a workitem plus where it lives."""

    #: The project's **directory name** — the id every other route, and every
    #: path helper, takes.
    project_id: str
    #: The project's durable identity from ``project.json``.
    pid: Optional[str] = None


class SkippedProject(_ForbidExtra):
    """A project the rollup could not read, and why."""

    project_id: str
    pid: Optional[str] = None
    #: The store's own code — ``corrupt_document``, ``unsupported_schema``, or
    #: ``unavailable`` for anything else (a directory that went away between
    #: the walk and the read, a permission change).
    code: str
    #: Path-free. The store's message names an absolute path, which is logged
    #: for the operator instead of being served to the caller.
    message: str


class WorkspaceWorkitemsResponse(_ForbidExtra):
    """``GET /api/workspace/workitems`` — the cross-project answer."""

    workitems: list[WorkspaceWorkitem]
    #: Rows in ``workitems`` — after ``?limit=``.
    count: int
    #: Rows that matched — before ``?limit=``. ``total > count`` is the only
    #: way a caller can tell it is seeing part of the answer.
    total: int
    truncated: bool
    #: The ``?assignee=`` value as it arrived, echoed verbatim so a caller can
    #: see what was interpreted. ``null`` when none was given.
    assignee: Optional[str] = None
    #: What that value resolved to, and what rows were actually matched against
    #: (case-insensitively).
    identities: list[str] = []
    #: Why ``me`` could not be resolved in full — currently only
    #: ``no_github_credential``. ``null`` when it resolved, and when no
    #: assignee filter was given.
    assignee_unresolved: Optional[str] = None
    #: How many projects were walked (read *and* skipped).
    projects: int
    skipped: list[SkippedProject] = []


#: What a caller may write for "me".
_ME = _SELF_ALIASES

#: The message served for a skipped project whose failure has no entry in the
#: document-error table — a directory that disappeared between the walk and the
#: read, a permission change, an unreadable pid.
_SKIPPED_FALLBACK = (
    "{document} could not be read for this project, so its workitems are "
    "missing from this answer. The rest of the workspace is unaffected."
)


async def _resolve_assignee(
    raw: Optional[str],
) -> tuple[Optional[list[str]], Optional[str]]:
    """``?assignee=`` → (identities to match, unresolved reason)."""
    value = (raw or "").strip()
    if not value:
        return None, None
    if value.casefold() in _ME:
        # The local half first: it is captured from the environment, needs no
        # network, and must keep working with GitHub switched off.
        identities = list(_self_identities())
        login = await _self_login_or_none()
        if not login:
            return identities, "no_github_credential"
        if login not in identities:
            identities.append(login)
        return identities, None
    login = value[1:].strip() if value.startswith("@") else value
    if not login:
        raise _bad_query("assignee must be `me`, `@login`, or a login")
    return [login], None


async def _self_login_or_none() -> Optional[str]:
    """``_self_github_login`` made total. Never raises, never 500s a poll."""
    try:
        return await _self_github_login()
    except Exception:
        logger.warning(
            "could not resolve this Space's GitHub login; `me` will match "
            "local identities only", exc_info=True,
        )
        return None


def _row_sort_key(row: dict) -> tuple[str, str, str]:
    """Newest first, deterministically."""
    stamp = row.get("updated_at") or row.get("created_at") or ""
    return (str(stamp), str(row.get("_project_id") or ""), str(row.get("id") or ""))


def _skipped_model(entry: dict) -> SkippedProject:
    """
    One skipped project, with the store's path-naming text logged, not served —
    the same split ``_workitem_error`` makes for a 409.
    """
    code = str(entry.get("code") or "unavailable")
    detail = entry.get("detail")
    project_id = str(entry.get("project_id") or "")
    if detail:
        logger.error(
            "workspace rollup skipped project %s (%s): %s",
            project_id, code, detail,
        )
    template = _DOCUMENT_ERRORS.get(code, _SKIPPED_FALLBACK)
    pid = entry.get("pid")
    return SkippedProject(
        project_id=project_id,
        pid=pid if isinstance(pid, str) and pid else None,
        code=code,
        message=template.format(document="workitems.json"),
    )


@router.get(
    "/api/workspace/workitems",
    response_model=WorkspaceWorkitemsResponse,
)
async def workspace_workitems(
    assignee: Optional[str] = Query(
        default=None,
        description="`me`, `@login`, or a login. Matched case-insensitively.",
    ),
    status: Optional[str] = Query(
        default=None, description="Filter to `open` or `closed`.",
    ),
    limit: int = Query(100, ge=1, le=500),
) -> WorkspaceWorkitemsResponse:
    """Every workitem in the workspace, filtered on the **projected** view."""
    if status is not None and status not in _WORKITEM_STATUSES:
        raise _bad_query(f"status must be one of {sorted(_WORKITEM_STATUSES)}")

    identities, unresolved = await _resolve_assignee(assignee)

    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    # Off the event loop: the fan-out is one walk plus a handful of small
    # blocking reads per project, and this route is ``async`` only because
    # resolving ``me`` is.
    rollup = await asyncio.to_thread(
        workspace.rollup_workitems, assignees=identities, status=status,
    )

    rows = sorted(rollup.rows, key=_row_sort_key, reverse=True)
    total = len(rows)
    shown = rows[:limit]
    return WorkspaceWorkitemsResponse(
        workitems=[
            WorkspaceWorkitem(
                **_make_workitem_model(
                    row, in_progress=bool(row.get("_in_progress")),
                ).model_dump(),
                project_id=str(row.get("_project_id") or ""),
                pid=row.get("_pid") if isinstance(row.get("_pid"), str) else None,
            )
            for row in shown
        ],
        count=len(shown),
        total=total,
        truncated=total > len(shown),
        assignee=assignee,
        identities=list(identities or []),
        assignee_unresolved=unresolved,
        projects=rollup.projects,
        skipped=[_skipped_model(entry) for entry in rollup.skipped],
    )
