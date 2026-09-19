"""The telemetry module's facade: what the routes, the tasks, the CLI
commands and other modules call.

Three surfaces, one module:

* **Usage.** The active agent's ``usage`` capability (through the loader:
  ``/api/usage/*``), one project's usage from the watcher's ``stats.json``
  and session index (``/api/xo-projects/{id}/usage/*``) and every project
  combined from the ``cache/`` rollups (``/api/xo-projects/usage/*``). The
  scopes come from ``modules.projects.service.resolve_scope``; the shaping
  is ``presenter.py`` plus the assembly here.
* **Telemetry sources.** Which runtimes report session telemetry, where
  each is read from and whether it is collected (``sources.py``).
* **The sessions view.** ``/xo/sessions.json``, built by
  ``services/cowork_agent/visualizer/session_telemetry.py`` through the
  workspace views and served from ``~/.quirq/cache/sessions.json``.

Plus the entry points of the module's two tasks (the watcher and the usage
sync). Knows nothing about HTTP: a failure is a
:class:`services.errors.ServiceError` carrying its status. Three answers
keep the bare-message ``detail`` the ``/api/usage`` surface has always
had (``code`` is ``None`` there, as for ``/api/schedules``): no usage
module for the active agent (501), ``start`` without ``end`` (400) and an
unknown session (404).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import modules.projects.service as projects_service
from services.cowork_agent.visualizer.workspace import views as workspace_views
from services.cowork_agent.visualizer.workspace_index import list_project_ids
from services.errors import NotFound, ServiceError, Unavailable

from . import presenter, sources, usage_loader
from .models import (
    AnalyticsStats,
    DailyBreakdownEntry,
    DailyCostEntry,
    DailyModelUsageEntry,
    MessageCounts,
    ModelUsageWithTotals,
    SessionCostSummary,
    SessionListItem,
    SessionListResponse,
    TokenTotals,
    ToolUsage,
    ToolUsageEntry,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
)

logger = logging.getLogger(__name__)


# ── The active agent's usage (``/api/usage/*``) ───────────────────────────────


def usage_module() -> Any:
    """The active agent's usage module, or the 501 the route has always
    answered when the agent ships none (a bare message on the wire)."""
    try:
        return usage_loader.load_usage_module()
    except ModuleNotFoundError as exc:
        raise ServiceError(
            None,
            "no usage module for active agent (tried "
            f"services.cowork_agent.adapters.<AGENT_NAME>.usage): {exc}",
            501,
        ) from exc


def usage_window(days: Optional[int], start: Optional[str], end: Optional[str], tz: str) -> dict:
    """Translate query params into the uniform module window shape.

    Explicit ``start`` + ``end`` take precedence; otherwise ``days``
    (default 30) becomes a rolling gateway-aligned window.
    """
    if start or end:
        if not (start and end):
            raise ServiceError(None, "start and end must be passed together", 400)
        return {"start": start, "end": end, "tz": tz}
    return {"days": days if days is not None else 30, "tz": tz}


def usage_dashboard(days: int = 30, tz: Optional[str] = None) -> Any:
    """Aggregated UsageStats for the active agent."""
    days = max(1, min(days, 365))
    tz_resolved = tz if tz in ("local", "utc") else "local"
    return usage_module().dashboard(window={"days": days, "tz": tz_resolved})


def usage_analytics(*, days: Optional[int] = None, start: Optional[str] = None,
                    end: Optional[str] = None, tz: str = "local") -> Any:
    return usage_module().analytics(window=usage_window(days, start, end, tz))


def usage_summary(*, days: Optional[int] = None, start: Optional[str] = None,
                  end: Optional[str] = None, tz: str = "local") -> Any:
    return usage_module().summary(window=usage_window(days, start, end, tz))


def usage_summary_card(days: int = 5, tz: str = "local") -> Any:
    return usage_module().summary_card(window={"days": days, "tz": tz})


def usage_sessions() -> Any:
    return usage_module().list_sessions()


def usage_session(session_id: str, *, start: Optional[str] = None,
                  end: Optional[str] = None, tz: str = "local") -> Any:
    window = usage_window(None, start, end, tz) if (start or end) else None
    result = usage_module().get_session(session_id, window=window)
    if result is None:
        raise ServiceError(None, f"Session {session_id} not found", 404)
    return result


# ── Scopes and shared assembly ────────────────────────────────────────────────


def project_scope(project_id: str) -> projects_service.VisualizerScope:
    """One project's handle, or 404 ``project_not_found``."""
    scope = projects_service.resolve_scope("xo-projects-visualizer", project_id)
    if not scope.project_exists():
        raise NotFound("project_not_found", "Project not found.")
    return scope


def workspace_scope() -> projects_service.WorkspaceVisualizerScope:
    return projects_service.resolve_scope("xo-workspace-visualizer")


def check_dates(start: Optional[str], end: Optional[str]) -> None:
    """400 ``invalid_query`` unless both are absent or YYYY-MM-DD."""
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise presenter.bad_query("start / end must be YYYY-MM-DD") from exc


def _sum_session_totals(sessionslist: dict[str, dict]) -> tuple[int, int]:
    """``(totalTokens, totalMessages)`` summed across rows: tokens from the
    adapter-owned ``usage`` block, messages from the watcher-augment
    ``messageCount`` when present, else 0."""
    total_tokens = 0
    total_messages = 0
    for row in sessionslist.values():
        usage = row.get("usage") or {}
        total_tokens += int(usage.get("input_tokens", 0) or 0)
        total_tokens += int(usage.get("output_tokens", 0) or 0)
        total_messages += int(row.get("messageCount", 0) or 0)
    return total_tokens, total_messages


def _bucket_daily_cost(sessionslist: dict[str, dict], *, days: int) -> list[DailyCostEntry]:
    """The ``dailyCost`` array, zero-filled for the requested window, oldest
    first. One bucket per session keyed by its ``updatedAt``: coarser than
    the per-event by_day rollup other reads use, but the totals match."""
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        d = presenter.date_from_ms(row.get("updatedAt"))
        if d is None:
            continue
        b = buckets.setdefault(d, {"cost": 0.0, "tokens": 0, "messages": 0})
        usage = row.get("usage") or {}
        b["tokens"] += int(usage.get("input_tokens", 0) or 0)
        b["tokens"] += int(usage.get("output_tokens", 0) or 0)
        b["messages"] += int(row.get("messageCount", 0) or 0)
        # Cost stays 0: no pricing table.

    today = datetime.now(timezone.utc)
    out: list[DailyCostEntry] = []
    for i in range(days):
        d = (today.timestamp() - (days - 1 - i) * 86400)
        date_str = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%Y-%m-%d")
        b = buckets.get(date_str, {"cost": 0.0, "tokens": 0, "messages": 0})
        out.append(DailyCostEntry(
            date=date_str,
            cost=round(float(b["cost"]), 6),
            tokens=int(b["tokens"]),
            messages=int(b["messages"]),
        ))
    return out


def _message_counts_for_row(row: dict) -> MessageCounts:
    """``MessageCounts`` from one merged sessionslist row: the role split
    from ``messageCountByRole`` (zeros for schema 1 augment rows), ``total``
    and ``toolCalls`` from the top-level augment fields."""
    by_role_raw = row.get("messageCountByRole")
    by_role = by_role_raw if isinstance(by_role_raw, dict) else {}
    return MessageCounts(
        total=int(row.get("messageCount", 0) or 0),
        user=int(by_role.get("user", 0) or 0),
        assistant=int(by_role.get("assistant", 0) or 0),
        toolCalls=int(row.get("toolCallCount", 0) or 0),
        toolResults=int(by_role.get("toolResults", 0) or 0),
        errors=int(by_role.get("errors", 0) or 0),
    )


def _tool_usage_for_session(stats: dict, native_session_id: str) -> ToolUsage:
    """Per-session tool tally from ``stats.by_session.<sid>.tools``."""
    if not native_session_id:
        return ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
    by_session = stats.get("by_session") or {}
    row = by_session.get(native_session_id) or {}
    tools_raw = row.get("tools") if isinstance(row, dict) else None
    if not isinstance(tools_raw, dict):
        return ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
    entries = [
        ToolUsageEntry(name=str(n), count=int(c or 0))
        for n, c in tools_raw.items() if int(c or 0) > 0
    ]
    entries.sort(key=lambda t: t.count, reverse=True)
    total = sum(t.count for t in entries)
    return ToolUsage(totalCalls=total, uniqueTools=len(entries), tools=entries)


def _model_usage_for_session(stats: dict, native_session_id: str) -> list[ModelUsageWithTotals]:
    """Per-session model breakdown from ``stats.by_session.<sid>.by_model``."""
    if not native_session_id:
        return []
    by_session = stats.get("by_session") or {}
    row = by_session.get(native_session_id) or {}
    bm = row.get("by_model") if isinstance(row, dict) else None
    if not isinstance(bm, dict):
        return []
    entries: list[ModelUsageWithTotals] = []
    for model, t in bm.items():
        if not isinstance(t, dict):
            continue
        inp = int(t.get("input", 0) or 0)
        outp = int(t.get("output", 0) or 0)
        if inp + outp <= 0:
            continue
        entries.append(ModelUsageWithTotals(
            provider=presenter.provider_for_model(str(model)),
            model=str(model),
            count=0,  # stats.by_session.by_model carries tokens only
            totals=TokenTotals(
                input=inp, output=outp,
                cacheRead=0, cacheWrite=0, totalTokens=inp + outp,
                totalCost=0.0, inputCost=0.0, outputCost=0.0,
                cacheReadCost=0.0, cacheWriteCost=0.0, missingCostEntries=0,
            ),
        ))
    entries.sort(key=lambda e: e.totals.totalTokens, reverse=True)
    return entries


def _daily_model_usage_from_by_day(by_day: dict[str, dict], dates: list[str]) -> list[DailyModelUsageEntry]:
    """Flatten ``by_day.<date>.by_model`` into one entry per (date, model)
    pair, dates in the requested order, models by tokens descending."""
    out: list[DailyModelUsageEntry] = []
    for d in dates:
        day = by_day.get(d) or {}
        models = (day.get("by_model") or {}) if isinstance(day, dict) else {}
        if not isinstance(models, dict):
            continue
        entries = [(model, mt) for model, mt in models.items() if isinstance(mt, dict)]
        entries.sort(
            key=lambda kv: int(kv[1].get("input", 0) or 0) + int(kv[1].get("output", 0) or 0),
            reverse=True,
        )
        for model, mt in entries:
            tokens = int(mt.get("input", 0) or 0) + int(mt.get("output", 0) or 0)
            if tokens <= 0:
                continue
            out.append(DailyModelUsageEntry(
                date=d,
                provider=presenter.provider_for_model(model),
                model=model,
                tokens=tokens,
                cost=0.0,
                count=int(mt.get("count", 0) or 0),
            ))
    return out


def _daily_breakdown_for_dates(by_day: dict[str, dict], dates: list[str]) -> list[DailyBreakdownEntry]:
    """``SessionCostSummary.dailyBreakdown``: the same per-date tokens as
    costAndTokens in its own model."""
    out: list[DailyBreakdownEntry] = []
    for d in dates:
        day = by_day.get(d) or {}
        tk = (day.get("tokens") or {}) if isinstance(day, dict) else {}
        out.append(DailyBreakdownEntry(
            date=d,
            tokens=int(tk.get("input", 0) or 0) + int(tk.get("output", 0) or 0),
            cost=0.0,
        ))
    return out


def _duration_ms_for_session(stats: dict, native_session_id: str) -> Optional[int]:
    """One session's duration from ``stats.by_session.<nativeSid>``; ``None``
    until the watcher has recorded one."""
    if not native_session_id:
        return None
    by_session = stats.get("by_session") or {}
    row = by_session.get(native_session_id)
    if not isinstance(row, dict):
        return None
    d = row.get("duration_ms")
    return int(d) if isinstance(d, (int, float)) else None


def _activity_dates(first_ms: Optional[int], last_ms: Optional[int]) -> list[str]:
    """The ISO dates (UTC) an activity window spans; ``[]`` when either
    bound is missing."""
    if not first_ms or not last_ms:
        return []
    if last_ms < first_ms:
        last_ms = first_ms
    start = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).date()
    end = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).date()
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def _row_to_list_item(composite_key: str, row: dict, *, project_id: Optional[str]) -> SessionListItem:
    """One sessionslist row as a ``SessionListItem`` (no path leakage)."""
    usage = row.get("usage") or {}
    native = row.get("nativeSessionId") or ""
    return SessionListItem(
        sessionId=composite_key,
        sessionFile=f"{native}.jsonl" if native else "",
        messageCount=int(row.get("messageCount", 0) or 0),
        totalTokens=presenter.row_total_tokens(usage),
        totalCost=0.0,
        firstActivity=row.get("firstActivity"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        projectId=project_id,
    )


def _aggregate_session_summary(composite_key: str, row: dict, *, single_session: bool,
                               stats: Optional[dict] = None) -> SessionCostSummary:
    """A ``SessionCostSummary`` from one sessionslist row.

    Token totals come from the adapter ``usage`` block. ``durationMs`` and
    ``activityDates`` are surfaced when ``stats`` is given (duration from
    ``stats.by_session.<nativeSid>.duration_ms``, dates from the row's
    first and last activity). ``dailyBreakdown`` is the project-wide by_day
    block filtered to the session's activity window, so it overstates a
    single session's share on days other sessions were active too.
    """
    usage = row.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    total = inp + out + cache_read + cache_write
    native = row.get("nativeSessionId") or ""
    first_act = row.get("firstActivity") or row.get("updatedAt")
    last_act = row.get("lastActivity") or row.get("updatedAt")

    duration_ms: Optional[int] = None
    dates: list[str] = []
    daily_breakdown: list[DailyBreakdownEntry] = []
    if stats is not None:
        duration_ms = _duration_ms_for_session(stats, native)
        dates = _activity_dates(first_act, last_act)
        if dates:
            daily_breakdown = _daily_breakdown_for_dates(presenter.by_day_from_stats(stats), dates)

    return SessionCostSummary(
        sessionId=composite_key if single_session else "all",
        sessionFile=f"{native}.jsonl" if (single_session and native) else "1 file",
        firstActivity=first_act,
        lastActivity=last_act,
        durationMs=duration_ms,
        activityDates=dates,
        input=inp,
        output=out,
        cacheRead=cache_read,
        cacheWrite=cache_write,
        totalTokens=total,
        totalCost=0.0,
        inputCost=0.0,
        outputCost=0.0,
        cacheReadCost=0.0,
        cacheWriteCost=0.0,
        missingCostEntries=0,
        dailyBreakdown=daily_breakdown,
        dailyLatency=[],
        dailyModelUsage=[],
        messageCounts=_message_counts_for_row(row),
        toolUsage=(
            _tool_usage_for_session(stats, native)
            if stats is not None else ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
        ),
        modelUsage=(_model_usage_for_session(stats, native) if stats is not None else []),
    )


# ── One project (``/api/xo-projects/{id}/usage/*``) ──────────────────────────


def project_usage_summary_card(project_id: str, *, days: int = 5) -> UsageSummaryCardResponse:
    """Token totals from the project's ``stats.json`` (input + output, no
    cache classes), messages from the augment counters, the daily
    breakdown by session ``updatedAt``."""
    scope = project_scope(project_id)
    stats = scope.read_stats() or {}
    total_tokens = presenter.tokens_from_stats(stats, days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # no pricing table
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=_bucket_daily_cost(sessionslist, days=days),
    )


def project_usage_analytics(project_id: str, *, days: Optional[int] = None,
                            start: Optional[str] = None, end: Optional[str] = None) -> UsageAnalyticsResponse:
    """One project's analytics dashboard."""
    check_dates(start, end)
    scope = project_scope(project_id)
    stats = scope.read_stats() or {}
    window_days = days or 5
    total_tokens = presenter.tokens_from_stats(stats, window_days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
    dates = presenter.zero_filled_dates(window_days)
    by_day = presenter.by_day_from_stats(stats)
    return UsageAnalyticsResponse(
        stats=AnalyticsStats(
            totalCost=0.0,
            totalTokens=total_tokens,
            totalMessages=total_messages,
            avgLatencyMs=presenter.avg_latency_ms_from_by_day(by_day),
        ),
        costAndTokens=presenter.cost_and_tokens_for_dates(by_day, dates),
        messages=presenter.messages_for_dates(by_day, dates),
        performance=presenter.performance_for_dates(by_day, dates),
        toolUsage=presenter.tool_usage_from_stats(stats, window_days),
        modelUsage=presenter.model_usage_entries(stats, window_days),
    )


def project_usage_sessions(project_id: str, *, agent_id: Optional[str] = None) -> SessionListResponse:
    """One project's sessions, newest first; ``agent_id`` keeps the rows
    whose composite key (``<backend>:<agent_id>:<surface>:<8hex>``) names it."""
    scope = project_scope(project_id)
    sessionslist = scope.read_sessionslist()
    items: list[SessionListItem] = []
    for composite_key, row in sessionslist.items():
        if agent_id:
            parts = composite_key.split(":")
            if len(parts) < 2 or parts[1] != agent_id:
                continue
        items.append(_row_to_list_item(composite_key, row, project_id=None))
    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=agent_id, count=len(items), sessions=items)


def project_usage_summary(project_id: str, *, days: Optional[int] = None,
                          start: Optional[str] = None, end: Optional[str] = None) -> SessionCostSummary:
    """The combined ``SessionCostSummary`` across one project's sessions,
    with a per-session sub-summary in ``sessions[]``."""
    check_dates(start, end)
    scope = project_scope(project_id)
    sessionslist = scope.read_sessionslist()
    stats = scope.read_stats() or {}
    # The window for the by_model rollup: the longer one for aggregate views.
    summary_window_days = days or 30

    per_session = [
        _aggregate_session_summary(k, r, single_session=True, stats=stats)
        for k, r in sessionslist.items()
    ]
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
    first_act = min((s.firstActivity for s in per_session if s.firstActivity), default=None)
    last_act = max((s.lastActivity for s in per_session if s.lastActivity), default=None)
    agg_msg = MessageCounts(
        total=sum(s.messageCounts.total for s in per_session),
        user=sum(s.messageCounts.user for s in per_session),
        assistant=sum(s.messageCounts.assistant for s in per_session),
        toolCalls=sum(s.messageCounts.toolCalls for s in per_session),
        toolResults=sum(s.messageCounts.toolResults for s in per_session),
        errors=sum(s.messageCounts.errors for s in per_session),
    )
    activity_dates = _activity_dates(first_act, last_act)
    return SessionCostSummary(
        sessionId="all",
        sessionFile=f"{len(per_session)} files",
        firstActivity=first_act,
        lastActivity=last_act,
        durationMs=None,
        activityDates=activity_dates,
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        totalCost=0.0, inputCost=0.0, outputCost=0.0,
        cacheReadCost=0.0, cacheWriteCost=0.0, missingCostEntries=0,
        dailyBreakdown=_daily_breakdown_for_dates(presenter.by_day_from_stats(stats), activity_dates),
        dailyLatency=[], dailyModelUsage=[],
        messageCounts=agg_msg,
        toolUsage=presenter.tool_usage_from_stats(stats, summary_window_days),
        modelUsage=presenter.model_usage_with_totals(stats, summary_window_days),
        sessionCount=len(per_session),
        sessions=per_session,
    )


def project_usage_session(project_id: str, session_id: str) -> SessionCostSummary:
    """One session of one project: ``session_id`` is the composite key or
    the ``nativeSessionId``; 404 ``session_not_found`` otherwise."""
    scope = project_scope(project_id)
    found = scope.read_one_session(session_id)
    if found is None:
        raise NotFound("session_not_found", "Session not found.")
    composite_key, row = found
    stats = scope.read_stats() or {}
    return _aggregate_session_summary(composite_key, row, single_session=True, stats=stats)


# ── Every project combined (``/api/xo-projects/usage/*``) ────────────────────


def _all_projects() -> list[str]:
    """Every project the watcher tracks (scaffolded and bare), so the
    workspace reads see exactly what the watcher writes."""
    return list_project_ids()


def _union_sessionslist() -> dict[str, dict]:
    """The union of every project's merged sessionslist, each row tagged
    with ``_project_id``. Composite keys embed the agent id and a unique
    suffix, so they never collide across projects."""
    merged: dict[str, dict] = {}
    for pid in _all_projects():
        scope = projects_service.resolve_scope("xo-projects-visualizer", pid)
        for key, row in scope.read_sessionslist().items():
            r = dict(row)
            r["_project_id"] = pid
            merged[key] = r
    return merged


def _response_time_stats_from_by_day(by_day: dict[str, dict]) -> dict:
    """Every day's latency samples as the ``ResponseTimeStats`` shape the
    UI expects (seconds): the reservoirs concatenated into one pool, then
    min, median, avg, p95 and max from it."""
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
        return {"avg": 0.0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "count": 0}
    avg_s = (sum_ms / count) / 1000.0
    pool.sort()
    if pool:
        median_s = pool[len(pool) // 2] / 1000.0
        p95_s = pool[max(0, int(0.95 * (len(pool) - 1)))] / 1000.0
    else:
        median_s = 0.0
        p95_s = 0.0
    return {
        "avg": round(avg_s, 3),
        "median": round(median_s, 3),
        "p95": round(p95_s, 3),
        "min": round((min_ms_overall or 0) / 1000.0, 3),
        "max": round(max_ms_overall / 1000.0, 3),
        "count": count,
    }


def workspace_usage_dashboard(*, days: int = 30) -> dict:
    """Workspace-aggregated ``UsageStats``, the ``/api/usage`` shape over
    every project. Input and output tokens come from the workspace
    ``stats.json`` rolling window (so they line up with the model usage
    card); the cache classes and the message counts from the sessionslist
    union, which is the only place that tracks them."""
    stats = workspace_scope().read_stats() or {}
    sessionslist = _union_sessionslist()

    rolling = (stats.get("rolling") or {}).get(presenter.rolling_key_for(days)) or {}
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
    avg_tokens_per_session = round(total_tokens_sum / total_sessions, 2) if total_sessions else 0.0

    # by_model: tokens from rolling.<window>.by_model (windowed, watcher-
    # accurate), message counts from per-day by_model summed over the window.
    by_model_raw = rolling.get("by_model") or {}
    by_day = presenter.by_day_from_stats(stats)
    model_dates = presenter.zero_filled_dates(days)
    model_call_counts = presenter.model_call_counts_from_by_day(by_day, model_dates)
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
            "provider_id": presenter.provider_for_model(str(model)),
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

    # by_session: the top 10 by tokens.
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

    # daily: the workspace by_day block, all zeros before the first tick.
    daily: list[dict] = []
    for d in presenter.zero_filled_dates(days):
        day = by_day.get(d) or {}
        tk = (day.get("tokens") or {}) if isinstance(day, dict) else {}
        msgs = (day.get("messages") or {}) if isinstance(day, dict) else {}
        daily.append({
            "date": d,
            "cost": 0.0,
            "tokens": int(tk.get("input", 0) or 0) + int(tk.get("output", 0) or 0),
            "messages": int(msgs.get("total", 0) or 0),
        })

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


def workspace_usage_summary_card(*, days: int = 5) -> UsageSummaryCardResponse:
    """The usage card over every project: tokens from the workspace
    ``stats.json``, messages from every session's augment counter, the
    daily breakdown by session ``updatedAt``."""
    stats = workspace_scope().read_stats() or {}
    total_tokens = presenter.tokens_from_stats(stats, days)
    sessionslist = _union_sessionslist()
    total_messages = 0
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        msgs = int(row.get("messageCount", 0) or 0)
        total_messages += msgs
        d = presenter.date_from_ms(row.get("updatedAt"))
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
        for d in presenter.zero_filled_dates(days)
    ]
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # no pricing table
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=daily,
    )


def workspace_usage_analytics(*, days: Optional[int] = None, start: Optional[str] = None,
                              end: Optional[str] = None) -> UsageAnalyticsResponse:
    """The analytics dashboard over every project."""
    check_dates(start, end)
    window_days = days or 5
    stats = workspace_scope().read_stats() or {}
    total_tokens = presenter.tokens_from_stats(stats, window_days)
    sessionslist = _union_sessionslist()
    total_messages = sum(int(r.get("messageCount", 0) or 0) for r in sessionslist.values())
    dates = presenter.zero_filled_dates(window_days)
    by_day = presenter.by_day_from_stats(stats)
    return UsageAnalyticsResponse(
        stats=AnalyticsStats(
            totalCost=0.0,
            totalTokens=total_tokens,
            totalMessages=total_messages,
            avgLatencyMs=presenter.avg_latency_ms_from_by_day(by_day),
        ),
        costAndTokens=presenter.cost_and_tokens_for_dates(by_day, dates),
        messages=presenter.messages_for_dates(by_day, dates),
        performance=presenter.performance_for_dates(by_day, dates),
        toolUsage=presenter.tool_usage_from_stats(stats, window_days),
        modelUsage=presenter.model_usage_entries(stats, window_days),
    )


def workspace_usage_sessions() -> SessionListResponse:
    """One row per session across every project, each tagged with its
    ``projectId``, newest first."""
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
        native = row.get("nativeSessionId") or ""
        items.append(SessionListItem(
            sessionId=composite_key,
            sessionFile=f"{native}.jsonl" if native else "",
            messageCount=int(row.get("messageCount", 0) or 0),
            totalTokens=total_tokens,
            totalCost=0.0,
            firstActivity=row.get("firstActivity"),
            lastActivity=row.get("lastActivity") or row.get("updatedAt"),
            projectId=row.get("_project_id"),
        ))
    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=None, count=len(items), sessions=items)


def _row_to_summary(composite_key: str, row: dict, *, stats: Optional[dict] = None) -> SessionCostSummary:
    """One workspace row as a ``SessionCostSummary``: the tool tally from
    ``stats.by_session`` (the workspace file carries every project's rows
    verbatim); ``modelUsage`` stays empty on this shape."""
    usage = row.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    native = row.get("nativeSessionId") or ""

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


def workspace_usage_summary(*, days: Optional[int] = None, start: Optional[str] = None,
                            end: Optional[str] = None) -> SessionCostSummary:
    """The aggregate ``SessionCostSummary`` over every project:
    ``sessionId`` is ``"all-projects"`` and ``sessions[]`` lists every
    session of every project."""
    check_dates(start, end)
    stats = workspace_scope().read_stats() or {}
    summary_window_days = days or 30
    sessionslist = _union_sessionslist()
    per_session = [_row_to_summary(k, r, stats=stats) for k, r in sessionslist.items()]
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
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
        toolUsage=presenter.tool_usage_from_stats(stats, summary_window_days),
        modelUsage=presenter.model_usage_with_totals(stats, summary_window_days),
        sessionCount=len(per_session),
        sessions=per_session,
    )


def workspace_usage_session(session_id: str) -> SessionCostSummary:
    """One session anywhere in the workspace, by composite key or
    ``nativeSessionId``; the owning project's stats feed the tool tally."""
    for pid in _all_projects():
        scope = projects_service.resolve_scope("xo-projects-visualizer", pid)
        found = scope.read_one_session(session_id)
        if found is not None:
            composite_key, row = found
            stats = scope.read_stats() or {}
            return _row_to_summary(composite_key, row, stats=stats)
    raise NotFound("session_not_found", "Session not found.")


# ── Telemetry sources (``/api/telemetry/sources``) ───────────────────────────


def list_sources() -> list[dict]:
    """Every provider of the ``session_telemetry`` capability with its
    effective data path and its collection switch."""
    return sources.list_sources()


def save_source(source_id: str, *, path: Optional[str] = None,
                enabled: Optional[bool] = None) -> dict:
    """Persist a source's path (empty clears the override) and/or its
    switch, then describe it. 404 ``unknown_source``, 400 ``invalid_path``."""
    return sources.save_source(source_id, path=path, enabled=enabled)


# ── The sessions view (``/xo/sessions.json``) ────────────────────────────────

SESSIONS_VIEW = "sessions"
VIEW_MAX_AGE_S = float(os.getenv("XO_VIEW_MAX_AGE_S", "120"))
_SESSIONS_UNAVAILABLE = ("sessions_unavailable", "No session telemetry source is currently available.")


async def sessions_view() -> dict:
    """The session telemetry payload: the file under the cache when it is
    fresh, else a rebuild off the event loop; a stale file beats an absent
    one when the rebuild fails. 503 when there is nothing to serve."""
    try:
        # ``stale_ok``: keep the payload even when it is past the window, so a
        # failed rebuild has something correct to fall back on.
        payload, age = workspace_views.read(SESSIONS_VIEW, max_age_s=VIEW_MAX_AGE_S, stale_ok=True)
    except Exception as exc:  # noqa: BLE001 - unreadable file: fall through to a rebuild
        logger.warning("sessions view unreadable (%s)", exc)
        payload, age = None, None
    if payload is None or workspace_views.is_stale(age, VIEW_MAX_AGE_S):
        try:
            rebuilt = await asyncio.to_thread(workspace_views.build, SESSIONS_VIEW)
        except Exception as exc:  # noqa: BLE001 - the walk failed; the file may still be good
            logger.warning("sessions view rebuild failed (%s)", exc)
            rebuilt = None
        if rebuilt is not None:
            payload = rebuilt
        elif payload is not None:
            # Stale beats absent, and beats a 503 even harder.
            logger.warning("sessions view rebuild produced nothing; serving age=%s", age)
    if payload is None:
        raise Unavailable(*_SESSIONS_UNAVAILABLE)
    return payload


def rebuild_sessions_view() -> Optional[dict]:
    """Rebuild ``sessions.json`` now, after a source changed, so the next
    Refresh reflects it. Never raises: the next watcher tick retries."""
    try:
        return workspace_views.build(SESSIONS_VIEW)
    except Exception:  # noqa: BLE001 - never fail the save that asked for it
        logger.warning("sessions view rebuild after a source change failed; the next tick retries", exc_info=True)
        return None


# ── The tasks ────────────────────────────────────────────────────────────────

WATCHER_ENABLED_ENV = "QUIRQ_WATCHER_ENABLED"


def _env_flag(name: str, default: str = "true") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}


def watcher_enabled() -> bool:
    """``QUIRQ_WATCHER_ENABLED``: the env gate beside the module switch,
    kept for one release."""
    return _env_flag(WATCHER_ENABLED_ENV)


async def run_watcher() -> None:
    """The watcher loop (``services/cowork_agent/visualizer/watcher.py``),
    until cancelled."""
    from services.cowork_agent.visualizer.watcher import start_watcher

    await start_watcher()


async def run_usage_sync() -> None:
    """The daily usage report to XO (``usage_sync.py``), until cancelled."""
    from .usage_sync import start_usage_sync_scheduler

    await start_usage_sync_scheduler()


def usage_reporting_status() -> dict:
    """Whether anything is being reported to XO: ``off``, ``on``,
    ``blocked`` or ``pending``, with the last probe and the watermark."""
    from .usage_sync import usage_reporting_status as _status

    return _status()
