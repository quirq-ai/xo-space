"""Project-scope BFF endpoints over one project's state.

This module never imports ``os`` or ``pathlib``. All filesystem reads
happen behind ``services.cowork_agent.scopes.VisualizerScope``, which
knows that a project's state spans two roots since T19 — the durable
``<project>/.xo/`` and the machine-local ``~/.quirq/projects/<pid>/`` —
and delegates every read to ``services.cowork_agent.visualizer.reader``.

Most endpoints are populated when the watcher has written the backing
file (``stats.json``, ``sessions-augment.json``, the session index,
``timeline.jsonl``, the activity snapshot). ``todos.json`` is the one
exception and the reason the todo handlers below are writers: it has no
watcher sink at all, and the ``POST/PATCH/DELETE /todos`` endpoints own
it outright (syncplan §7, T8). Files written under older schema versions
degrade gracefully — readers treat missing keys as zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AnalyticsStats,
    CostAndTokensEntry,
    CreateTodoRequest,
    DailyBreakdownEntry,
    DailyCostEntry,
    DailyModelUsageEntry,
    DeleteTodoResponse,
    MessageCounts,
    MessagesEntry,
    ModelUsageEntry,
    ModelUsageWithTotals,
    OpenSession,
    PerformanceEntry,
    SessionCostSummary,
    SessionListItem,
    SessionListResponse,
    SessionTodos,
    TimelineEvent,
    TimelineResponse,
    TokenTotals,
    Todo,
    TodosResponse,
    ToolUsage,
    ToolUsageEntry,
    UpdateTodoRequest,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
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
    model_usage_entries as _model_usage_from_stats,
    model_usage_with_totals as _model_usage_with_totals_from_stats,
    parse_types_param as _parse_types_param,
    performance_entry_for_day as _performance_entry_for_day,
    performance_for_dates as _performance_for_dates,
    provider_for_model as _provider_for_model,
    row_total_tokens as _row_total_tokens,
    tokens_from_stats as _tokens_from_stats,
    tool_usage_from_stats as _tool_usage_from_stats,
    zero_filled_dates as _zero_filled_dates,
)
from services.cowork_agent import scopes
from services.cowork_agent.visualizer.todo_status import VALID_TODO_STATUSES

router = APIRouter()


# ── Common helpers ────────────────────────────────────────────────────────────


def _require_project(project_id: str) -> scopes.VisualizerScope:
    """Resolve a project-scope visualizer handle or 404."""
    scope = scopes.resolve_scope("xo-projects-visualizer", project_id)
    if not scope.project_exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    return scope


# ── /api/xo-projects/{id}/usage/summary/card ─────────────────────────────────


def _sum_session_totals(sessionslist: dict[str, dict]) -> tuple[int, int]:
    """Return (totalTokens, totalMessages) summed across rows.

    Tokens are read from the adapter-owned ``usage`` block. Message
    count comes from the watcher-augment ``messageCount`` field if
    present, else ``0``.
    """
    total_tokens = 0
    total_messages = 0
    for row in sessionslist.values():
        usage = row.get("usage") or {}
        total_tokens += int(usage.get("input_tokens", 0) or 0)
        total_tokens += int(usage.get("output_tokens", 0) or 0)
        total_messages += int(row.get("messageCount", 0) or 0)
    return total_tokens, total_messages


def _bucket_daily_cost(
    sessionslist: dict[str, dict], *, days: int
) -> list[DailyCostEntry]:
    """Build the ``dailyCost`` array, zero-filled for the requested
    window, sorted oldest→newest. One bucket per session keyed by
    session ``updatedAt``; coarser than the per-event by_day rollup
    other endpoints use, but totals match either way.
    """
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        d = _date_from_ms(row.get("updatedAt"))
        if d is None:
            continue
        b = buckets.setdefault(d, {"cost": 0.0, "tokens": 0, "messages": 0})
        usage = row.get("usage") or {}
        b["tokens"] += int(usage.get("input_tokens", 0) or 0)
        b["tokens"] += int(usage.get("output_tokens", 0) or 0)
        b["messages"] += int(row.get("messageCount", 0) or 0)
        # Cost stays 0 — no pricing table.

    today = datetime.now(timezone.utc)
    out: list[DailyCostEntry] = []
    for i in range(days):
        d = (today.timestamp() - (days - 1 - i) * 86400)
        date_str = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%Y-%m-%d")
        b = buckets.get(date_str, {"cost": 0.0, "tokens": 0, "messages": 0})
        out.append(
            DailyCostEntry(
                date=date_str,
                cost=round(float(b["cost"]), 6),
                tokens=int(b["tokens"]),
                messages=int(b["messages"]),
            )
        )
    return out


@router.get(
    "/api/xo-projects/{project_id}/usage/summary/card",
    response_model=UsageSummaryCardResponse,
)
def project_usage_summary_card(
    project_id: str,
    days: int = Query(5, ge=1, le=365),
) -> UsageSummaryCardResponse:
    """Lightweight usage widget for one project.

    Token totals come from the watcher's per-project ``stats.json``
    (input + output, no cache_read / cache_creation) — same number
    users see in the UI. Message totals come from the per-session
    augment counters. Daily breakdown still buckets by session
    ``updatedAt`` for now.
    """
    scope = _require_project(project_id)
    stats = scope.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # no pricing table
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=_bucket_daily_cost(sessionslist, days=days),
    )


# ── /api/xo-projects/{id}/todos ──────────────────────────────────────────────


def _shape_todos(
    project_id: str, raw: Optional[dict], *, include_deleted: bool = False,
) -> TodosResponse:
    """Convert the on-disk ``todos.json`` shape to the wire shape.

    On-disk:  {schema, updated_at, sessions: {sid: {runtime, source_file,
                session_started_at, todos: [{id, content, status, ...}]}}}
    On wire:  {project_id, updated_at, sessions: {sid: SessionTodos}}

    Deleted todos are tombstones, not rows: the store keeps them
    forever, and this is the filter that keeps them off both UIs. Ask
    for them explicitly with ``include_deleted`` when you want the
    history rather than the work.

    Pydantic's ``extra="forbid"`` on ``Todo`` is the wire allowlist —
    unexpected keys raise 500 ``scope_unavailable`` (we'd rather fail
    closed than leak a writer's mistake).
    """
    if not raw:
        return TodosResponse(project_id=project_id, updated_at=None, sessions={})

    out_sessions: dict[str, SessionTodos] = {}
    for sid, entry in (raw.get("sessions") or {}).items():
        if not isinstance(entry, dict):
            continue
        todos: list[Todo] = []
        for t in entry.get("todos") or []:
            if not isinstance(t, dict):
                continue
            if not include_deleted and t.get("deleted_at") is not None:
                continue
            todos.append(_make_todo_model(t))
        out_sessions[str(sid)] = SessionTodos(
            runtime=str(entry.get("runtime", "")),
            source_file=None,  # never echo absolute paths back
            session_started_at=entry.get("session_started_at"),
            todos=todos,
        )

    return TodosResponse(
        project_id=project_id,
        updated_at=raw.get("updated_at"),
        sessions=out_sessions,
    )


@router.get(
    "/api/xo-projects/{project_id}/todos",
    response_model=TodosResponse,
)
def project_todos(
    project_id: str,
    include_deleted: bool = Query(
        False, description="Include soft-deleted todos (tombstones)."
    ),
) -> TodosResponse:
    """Per-session task list for one project.

    Empty ``{sessions: {}}`` when nothing has written ``todos.json``
    yet. Adapter-written ``sessionslist.json`` is NOT a source for
    todos — they live exclusively in ``todos.json``, which the CRUD
    endpoints below own.

    Deleted todos are hidden by default; ``?include_deleted=true``
    returns them with their ``deleted_at`` / ``deleted_by`` set.
    """
    scope = _require_project(project_id)
    try:
        raw = scope.read_todos()
    except Exception as exc:  # malformed JSON — fail closed
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable",
                    "message": "todos.json is not readable."},
        ) from exc
    return _shape_todos(project_id, raw, include_deleted=include_deleted)


# ── /api/xo-projects/{id}/todos — CRUD for any runtime ──────────────────────
#
# EVERY agent writes todos through this API rather than touching
# .xo/todos.json directly — including runtimes with a native todo tool,
# whose tool calls no longer reach any watcher sink. That is what makes
# todos.json, the timeline and the per-session taskCount identical
# whichever backend is active (syncplan §7). The handle's CRUD methods
# delegate to services.cowork_agent.visualizer.todos_store, the file's
# only writer, which emits the lifecycle events the timeline and counter
# sinks render. See docs/visualizer-overview.md for the full contract.


#: What an unrecognised on-disk status renders as. ``pending`` is the
#: least-committal *open* value: the row stays visible and actionable
#: rather than being hidden or claimed complete.
_STATUS_FALLBACK = "pending"


def _coerce_status(raw: object, *, todo_id: str) -> str:
    """Map an on-disk status onto the declared vocabulary.

    ``Todo.status`` is a strict ``Literal`` so the OpenAPI schema carries
    the enum (that is the whole point of T6 — the wire previously
    declared no enum at all). But the *read* path must tolerate what is
    already on disk: rows written before the vocabulary was enforced can
    carry anything, and this model is constructed per row inside
    :func:`_shape_todos`, which the route calls **outside** its
    ``try``/``except``. So one legacy row used to 500 the entire
    project's todo list — including every well-formed row beside it.

    Coerce and log instead. Silently dropping the row would be worse:
    a work item that vanishes with no signal is the failure mode the
    tombstone design (§5.5) exists to avoid.
    """
    value = str(raw) if raw is not None else ""
    if value in VALID_TODO_STATUSES:
        return value
    logger.warning(
        "todo %s carries status %r, which is not in the declared "
        "vocabulary %s; rendering it as %r. Repair the row or delete it.",
        todo_id or "<no id>", value, sorted(VALID_TODO_STATUSES),
        _STATUS_FALLBACK,
    )
    return _STATUS_FALLBACK


def _make_todo_model(d: dict) -> Todo:
    todo_id = str(d.get("id", ""))
    return Todo(
        id=todo_id,
        content=str(d.get("content", "")),
        status=_coerce_status(d.get("status", _STATUS_FALLBACK), todo_id=todo_id),
        description=d.get("description"),
        active_form=d.get("active_form"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        deleted_at=d.get("deleted_at"),
        deleted_by=d.get("deleted_by"),
    )


@router.post(
    "/api/xo-projects/{project_id}/todos",
    response_model=Todo,
    status_code=201,
)
def project_todos_create(project_id: str, body: CreateTodoRequest) -> Todo:
    """Create a new todo under the project (any runtime can call).

    ``session_id`` defaults to the ``"_project"`` pseudo-session so
    callers without a session concept don't have to invent one.
    """
    scope = _require_project(project_id)
    try:
        new = scope.create_todo(
            runtime=body.runtime,
            content=body.content,
            description=body.description,
            active_form=body.active_form,
            session_id=body.session_id,
            status=body.status,
        )
    except Exception as exc:
        # Surface validation errors from todos_store as 400; the store
        # raises TodosStoreError with .code set to a stable machine code.
        code = getattr(exc, "code", None)
        if code in {"invalid_runtime", "invalid_session_id", "invalid_value", "invalid_status"}:
            raise HTTPException(
                status_code=400,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return _make_todo_model(new)


@router.get(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=Todo,
)
def project_todos_get(project_id: str, todo_id: str) -> Todo:
    """Fetch one todo by id.

    A soft-deleted todo is ``404 todo_not_found`` here, matching the
    list view: the tombstone is history, reachable through
    ``GET /todos?include_deleted=true``.
    """
    scope = _require_project(project_id)
    found = scope.get_todo(todo_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "todo_not_found", "message": "Todo not found."},
        )
    _, todo = found
    return _make_todo_model(todo)


@router.patch(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=Todo,
)
def project_todos_update(
    project_id: str, todo_id: str, body: UpdateTodoRequest,
) -> Todo:
    """Update fields on an existing todo. Most common: status transition
    ``pending`` → ``in_progress`` → ``completed``.
    """
    scope = _require_project(project_id)
    try:
        updated = scope.update_todo(
            todo_id,
            status=body.status,
            content=body.content,
            description=body.description,
            active_form=body.active_form,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "todo_not_found":
            raise HTTPException(
                status_code=404,
                detail={"code": "todo_not_found", "message": "Todo not found."},
            ) from exc
        if code in {"invalid_status", "invalid_value"}:
            raise HTTPException(
                status_code=400,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return _make_todo_model(updated)


@router.delete(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=DeleteTodoResponse,
)
def project_todos_delete(project_id: str, todo_id: str) -> DeleteTodoResponse:
    """Soft delete — the record is tombstoned (``deleted_at`` set), never
    removed, so it cannot come back and the history stays readable.

    Idempotent: ``deleted: false`` if the todo was already absent or
    already tombstoned (never 404, matches the /api/secrets/{key}
    pattern). The response shape is unchanged."""
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_todo(todo_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return DeleteTodoResponse(project_id=project_id, todo_id=todo_id, deleted=deleted)


# ── /api/xo-projects/{id}/activity ───────────────────────────────────────────


def _shape_activity(project_id: str, raw: Optional[dict]) -> ActivityResponse:
    if not raw:
        return ActivityResponse(project_id=project_id, updated_at=None, open_sessions=[])

    open_sessions: list[OpenSession] = []
    for s in raw.get("open_sessions") or []:
        if not isinstance(s, dict):
            continue
        # Pydantic's extra="forbid" handles the allowlist; we only
        # pre-check required keys (schema requires them but a fresh-
        # boot empty file may omit). Missing required field → skip
        # the row.
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
                )
            )
        except (KeyError, ValueError):
            continue

    return ActivityResponse(
        project_id=project_id,
        updated_at=raw.get("updated_at"),
        open_sessions=open_sessions,
    )


@router.get(
    "/api/xo-projects/{project_id}/activity",
    response_model=ActivityResponse,
)
def project_activity(project_id: str) -> ActivityResponse:
    """Live presence — which sessions are open in this project right now.

    Empty ``{open_sessions: []}`` when the watcher hasn't written the
    machine-local presence snapshot yet. AGENTS.md's boot ritual calls
    this endpoint to answer "is anyone else working here right now?".
    """
    scope = _require_project(project_id)
    try:
        raw = scope.read_activity()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable",
                    "message": "activity state is not readable."},
        ) from exc
    return _shape_activity(project_id, raw)


def _message_counts_for_row(row: dict) -> MessageCounts:
    """Build a ``MessageCounts`` from one merged sessionslist row.

    Reads ``messageCountByRole`` for the role split; falls back to
    zeros for schema 1 augment rows that don't carry the field.
    ``total`` and ``toolCalls`` come from top-level augment fields.
    """
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


def _model_usage_for_session(
    stats: dict, native_session_id: str
) -> list[ModelUsageWithTotals]:
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
            provider=_provider_for_model(str(model)),
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


def _daily_model_usage_from_by_day(
    by_day: dict[str, dict], dates: list[str]
) -> list[DailyModelUsageEntry]:
    """Flatten ``by_day.<date>.by_model`` into one entry per
    (date, model) pair. Dates iterated in the requested order so the
    series stays time-ordered; within a date, models sorted by tokens
    desc for stable display order."""
    out: list[DailyModelUsageEntry] = []
    for d in dates:
        day = by_day.get(d) or {}
        models = (day.get("by_model") or {}) if isinstance(day, dict) else {}
        if not isinstance(models, dict):
            continue
        entries = [
            (model, mt) for model, mt in models.items()
            if isinstance(mt, dict)
        ]
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
                provider=_provider_for_model(model),
                model=model,
                tokens=tokens,
                cost=0.0,
                count=int(mt.get("count", 0) or 0),
            ))
    return out


def _daily_breakdown_for_dates(
    by_day: dict[str, dict], dates: list[str]
) -> list[DailyBreakdownEntry]:
    """``SessionCostSummary.dailyBreakdown`` shape — same per-date
    tokens as costAndTokens but a different Pydantic model."""
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
    """Look up one session's duration in ``stats.by_session.<nativeSid>``.
    Returns None when the watcher hasn't recorded a duration yet."""
    if not native_session_id:
        return None
    by_session = stats.get("by_session") or {}
    row = by_session.get(native_session_id)
    if not isinstance(row, dict):
        return None
    d = row.get("duration_ms")
    return int(d) if isinstance(d, (int, float)) else None


def _activity_dates(first_ms: Optional[int], last_ms: Optional[int]) -> list[str]:
    """List of ISO dates (UTC) spanned by the activity window. Typical
    session covers one or two dates; returns ``[]`` when either bound
    is missing."""
    if not first_ms or not last_ms:
        return []
    from datetime import timedelta
    if last_ms < first_ms:
        last_ms = first_ms
    start = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).date()
    end = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).date()
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def _row_to_list_item(composite_key: str, row: dict, *, project_id: Optional[str]) -> SessionListItem:
    """One sessionslist row → ``SessionListItem`` (no path leakage)."""
    usage = row.get("usage") or {}
    native = row.get("nativeSessionId") or ""
    return SessionListItem(
        sessionId=composite_key,
        sessionFile=f"{native}.jsonl" if native else "",
        messageCount=int(row.get("messageCount", 0) or 0),
        totalTokens=_row_total_tokens(usage),
        totalCost=0.0,
        firstActivity=row.get("firstActivity"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        projectId=project_id,
    )


def _aggregate_session_summary(
    composite_key: str, row: dict, *, single_session: bool,
    stats: Optional[dict] = None,
) -> SessionCostSummary:
    """Build a ``SessionCostSummary`` from one sessionslist row.

    Token totals come from the adapter ``usage`` block. ``durationMs``
    and ``activityDates`` are surfaced when ``stats`` is provided —
    duration from ``stats.by_session.<nativeSid>.duration_ms`` and
    dates derived from the row's first/last activity. Cost, per-day,
    per-tool, and per-model breakdowns remain empty until the watcher
    writes a per-day rollup.
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
            # dailyBreakdown is the by_day token block filtered to
            # the session's activity window. by_day buckets are
            # project-wide (they aggregate across sessions on the
            # same day), so this overstates a single session's
            # contribution on days where other sessions were also
            # active. Per-session daily breakdown would need per-
            # session daily tracking in sessions-augment.
            daily_breakdown = _daily_breakdown_for_dates(
                _by_day_from_stats(stats), dates,
            )

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
        modelUsage=(
            _model_usage_for_session(stats, native) if stats is not None else []
        ),
    )


def _bucket_by_date(sessionslist: dict[str, dict]) -> dict[str, dict[str, int | float]]:
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        d = _date_from_ms(row.get("updatedAt"))
        if d is None:
            continue
        b = buckets.setdefault(d, {"tokens": 0, "cost": 0.0})
        b["tokens"] += _row_total_tokens(row.get("usage") or {})
    return buckets


# ── /api/xo-projects/{id}/usage/analytics ────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/analytics",
    response_model=UsageAnalyticsResponse,
)
def project_usage_analytics(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Per-project analytics dashboard. Shape mirrors ``/openclaw/usage/analytics``."""
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    scope = _require_project(project_id)
    stats = scope.read_stats() or {}
    window_days = days or 5
    total_tokens = _tokens_from_stats(stats, window_days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
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
        modelUsage=_model_usage_from_stats(stats, window_days),
    )


# ── /api/xo-projects/{id}/usage/sessions ─────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/sessions",
    response_model=SessionListResponse,
)
def project_usage_sessions(
    project_id: str,
    agent_id: Optional[str] = Query(None),
) -> SessionListResponse:
    """List sessions for one project. Mirrors ``/openclaw/usage/sessions``."""
    scope = _require_project(project_id)
    sessionslist = scope.read_sessionslist()

    items: list[SessionListItem] = []
    for composite_key, row in sessionslist.items():
        if agent_id:
            # Composite key shape is "<backend>:<agent_id>:<surface>:<8hex>"
            parts = composite_key.split(":")
            if len(parts) < 2 or parts[1] != agent_id:
                continue
        items.append(_row_to_list_item(composite_key, row, project_id=None))

    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=agent_id, count=len(items), sessions=items)


# ── /api/xo-projects/{id}/usage/summary ──────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/summary",
    response_model=SessionCostSummary,
)
def project_usage_summary(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """Aggregate ``SessionCostSummary`` across this project's sessions.

    Mirrors ``/openclaw/usage/summary``: returns the combined summary
    plus a ``sessions[]`` array with per-session sub-summaries.
    """
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    scope = _require_project(project_id)
    sessionslist = scope.read_sessionslist()
    stats = scope.read_stats() or {}
    # Window for the by_model rollup. Mirrors _tokens_from_stats's
    # 7d/30d choice: prefer the longer window for aggregate views.
    summary_window_days = days or 30

    per_session = [
        _aggregate_session_summary(k, r, single_session=True, stats=stats)
        for k, r in sessionslist.items()
    ]

    # Combined totals
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
    first_act = min((s.firstActivity for s in per_session if s.firstActivity), default=None)
    last_act = max((s.lastActivity for s in per_session if s.lastActivity), default=None)
    # Aggregate messageCounts by summing each per-session breakdown.
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
        dailyBreakdown=_daily_breakdown_for_dates(_by_day_from_stats(stats), activity_dates),
        dailyLatency=[], dailyModelUsage=[],
        messageCounts=agg_msg,
        toolUsage=_tool_usage_from_stats(stats, summary_window_days),
        modelUsage=_model_usage_with_totals_from_stats(stats, summary_window_days),
        sessionCount=len(per_session),
        sessions=per_session,
    )


# ── /api/xo-projects/{id}/usage/sessions/{session_id} ────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/sessions/{session_id}",
    response_model=SessionCostSummary,
)
def project_usage_one_session(
    project_id: str,
    session_id: str,
) -> SessionCostSummary:
    """Single-session detail. Mirrors ``/openclaw/usage/sessions/{sid}``.

    ``session_id`` accepts either the composite key
    (``claude:blackhole:web:67a1ac06``) or the ``nativeSessionId``
    (``aa4b140b-…``) — same dual-id lookup as
    ``services/cowork_agent/sessions_io.py:107``.
    """
    scope = _require_project(project_id)
    found = scope.read_one_session(session_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "Session not found."},
        )
    composite_key, row = found
    stats = scope.read_stats() or {}
    return _aggregate_session_summary(
        composite_key, row, single_session=True, stats=stats,
    )


# ── /api/xo-projects/{id}/timeline ───────────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/timeline",
    response_model=TimelineResponse,
)
def project_timeline(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Newest-first event stream for one project.

    Reads the project's runtime ``timeline.jsonl``. Empty when the
    watcher hasn't emitted any events for this project yet.
    """
    if before is not None:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _bad_query("before must be an ISO-8601 timestamp") from exc

    type_set = _parse_types_param(types)
    scope = _require_project(project_id)
    events = scope.read_timeline(limit=limit, before=before, types=type_set)

    out: list[TimelineEvent] = []
    for ev in events:
        try:
            out.append(TimelineEvent(**ev))
        except Exception:
            # malformed event line — already logged by reader; skip
            continue

    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=project_id, events=out, next_cursor=next_cursor)
