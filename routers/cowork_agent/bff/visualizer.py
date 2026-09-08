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

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import ValidationError

logger = logging.getLogger(__name__)

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AdoptIssueRequest,
    AnalyticsStats,
    AssignWorkitemRequest,
    CostAndTokensEntry,
    ClaimWorkitemRequest,
    CreateTodoRequest,
    CreateWorkitemRequest,
    DailyBreakdownEntry,
    DailyCostEntry,
    DailyModelUsageEntry,
    DeleteTodoResponse,
    DeleteWorkitemResponse,
    GithubIssue,
    GithubIssueAssignee,
    GithubIssuesResponse,
    GithubMirrorError,
    MessageCounts,
    MessagesEntry,
    ModelUsageEntry,
    ModelUsageWithTotals,
    OpenSession,
    PerformanceEntry,
    ReleaseWorkitemClaimResponse,
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
    UpdateWorkitemRequest,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
    Workitem,
    WorkitemAssignment,
    WorkitemClaim,
    WorkitemLinks,
    WorkitemSource,
    WorkitemsResponse,
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
from services.cowork_agent import coder_identity, github_poller, scopes
from services.cowork_agent.connectors import github_connector, github_issue_actions
from services.cowork_agent.visualizer import workitem_projection as _projection
from services.cowork_agent.visualizer.ingest.events import WorkitemEvent
from services.cowork_agent.visualizer.todo_status import VALID_TODO_STATUSES
from services.cowork_agent.visualizer.workitems_store import (
    VALID_STATE_REASONS as _WORKITEM_STATE_REASONS,
    VALID_STATUSES as _WORKITEM_STATUSES,
    emit_workitem_events as _emit_workitem_events,
    is_adopted as _is_adopted,
)

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
def project_todos_delete(
    project_id: str,
    todo_id: str,
    runtime: Optional[str] = Query(
        default=None,
        description=(
            "Calling runtime, recorded as the tombstone's `deleted_by`. "
            "Optional; the todo is tombstoned either way. Same charset as "
            "the required `runtime` on create."
        ),
    ),
) -> DeleteTodoResponse:
    """Soft delete — the record is tombstoned (``deleted_at`` set), never
    removed, so it cannot come back and the history stays readable.

    Idempotent: ``deleted: false`` if the todo was already absent or
    already tombstoned (never 404, matches the /api/secrets/{key}
    pattern). The response shape is unchanged.

    ``runtime`` is what fills ``deleted_by``. The field has been in the
    tombstone design since syncplan §5.5 but was structurally unreachable
    over HTTP — the store parameter existed and no route could set it — so
    every tombstone recorded a null author. It is a query parameter rather
    than a body because DELETE bodies are widely dropped in transit, and
    optional so existing callers are unaffected: no ``runtime``, no
    attribution, same 200."""
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_todo(todo_id, deleted_by=runtime)
    except Exception as exc:
        # An invalid ``runtime`` is a caller error, not a write failure.
        # Without this branch the shared 500 below would report
        # "todos.json write failed" for a request that never reached disk.
        if getattr(exc, "code", None) == "invalid_runtime":
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_runtime", "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return DeleteTodoResponse(project_id=project_id, todo_id=todo_id, deleted=deleted)


# ── /api/xo-projects/{id}/workitems — CRUD for any runtime ──────────────────
#
# The project tier of workitems-plan §7.1, and deliberately the todos
# surface again: same ``runtime`` vocabulary, same tombstone semantics,
# same ``{"code": ..., "message": ...}`` 400 bodies, same optional
# ``?runtime=`` on DELETE. An agent that can drive todos can drive
# workitems without learning a second dialect (§7.1).
#
# What a workitem is *not* is a todo. A workitem is a coarse, durable
# unit of work with an owner and a lifecycle; a todo is an agent's step
# list inside one session (§9, D3). They are distinct records joined by
# ``links.todo_ids``, which is why this is a second surface rather than a
# flag on the first.
#
# These handlers are the only writer of ``<project>/.xo/workitems.json``,
# and they delegate every read and write to
# ``services.cowork_agent.visualizer.workitems_store`` through the scope
# handle. Reads additionally pass through
# ``visualizer.workitem_projection``, which joins the record with the
# GitHub mirror (§5.3, W7): for an adopted item ``status`` and
# ``assignee`` are the mirror's, always, and ``title``/``labels`` fall
# back to the snapshot taken at adoption. When the mirror has nothing to
# say — never polled, no ``gh``, offline, the issue deleted — the item
# still renders from the file, flagged ``stale``, with the four
# GitHub-owned fields ``null``, which is "unknown", not "unset". It is
# never a 404.


#: Store codes that mean *the caller asked for something invalid*. Each
#: maps to ``400`` with the store's own message, exactly as the todos
#: routes do — the message names the field and the constraint, so it is
#: worth forwarding rather than replacing.
#:
#: ``github_authoritative`` belongs here and is easy to misfile. It is
#: not a write failure: the write was well-formed and was refused because
#: it named a field GitHub owns for an adopted item (§5.3). The fix is on
#: the caller's side — change it on the issue — which is what makes it a
#: 4xx.
_WORKITEM_CALLER_ERRORS: frozenset[str] = frozenset({
    "invalid_runtime",
    "invalid_assignee",
    "invalid_todo_id",
    "invalid_session_id",
    "invalid_node_id",
    "invalid_value",
    "invalid_status",
    "invalid_state_reason",
    "invalid_source",
    "github_authoritative",
})

#: Store codes that mean *the document on disk cannot be acted on*, with
#: the message served in their place. The store's own text names the
#: absolute path, which this layer does not echo back (the same reason
#: ``_shape_todos`` blanks ``source_file``); the full text is logged
#: instead, where the operator who can act on it will see it.
#: ``{document}`` because the claim routes below run the same mapping
#: over a *different* file (``claims.json``, runtime tier). Naming the
#: wrong document in the message would send an operator to repair a file
#: that is fine.
_WORKITEM_DOCUMENT_ERRORS: dict[str, str] = {
    "corrupt_document": (
        "{document} is not a readable document of its kind. It is refused "
        "rather than read as empty or overwritten, so nothing it holds is "
        "discarded. Repair or move the file."
    ),
    "unsupported_schema": (
        "{document} declares a schema version this Space does not write. "
        "It is refused rather than rewritten, so no key a newer version added "
        "is silently dropped. Upgrade the Space, or restore the document."
    ),
}


def _workitem_error(
    exc: Exception, *, failure: str, document: str = "workitems.json",
) -> HTTPException:
    """Map a ``WorkitemsStoreError`` onto its HTTP answer.

    ``workitem_not_found`` → **404**, the ``invalid_*`` family and
    ``github_authoritative`` → **400**, anything unrecognised → **500**.
    That much is the todos surface, restated.

    ``corrupt_document`` and ``unsupported_schema`` → **409 Conflict**,
    which is the one choice here that needed making rather than
    inheriting. Neither is a caller error: the request was well-formed
    and no field of it is at fault, so 400 would blame the wrong party
    and tell the caller to fix something they cannot see. Neither is a
    server fault either: nothing crashed and nothing is broken in this
    process, so 500 would say "we have a bug", invite a retry that cannot
    work, and page whoever watches the 5xx rate for a file a human left
    on disk. 503 would be a lie of a third kind — it promises that
    waiting helps, and waiting never repairs a truncated JSON file or
    downgrades a schema.

    409 is what is left, and it fits rather than merely surviving
    elimination: RFC 9110 defines it as a conflict with *the current
    state of the target resource*, generated where "the user might be
    able to resolve the conflict and resubmit". That is exactly the
    situation — the workitems document is in a state this revision
    refuses to act on, the resolution is to repair, move or restore it,
    and the same request then succeeds unchanged. The body carries the
    store's ``code`` so a client can tell the two apart without parsing
    prose.
    """
    code = getattr(exc, "code", None)
    if code == "workitem_not_found":
        return HTTPException(
            status_code=404,
            detail={"code": code, "message": "Workitem not found."},
        )
    if code in _WORKITEM_CALLER_ERRORS:
        return HTTPException(
            status_code=400, detail={"code": code, "message": str(exc)},
        )
    if code in _WORKITEM_DOCUMENT_ERRORS:
        # The store's message names the path; log it, don't serve it.
        logger.error("%s refused (%s): %s", document, code, exc)
        return HTTPException(
            status_code=409,
            detail={
                "code": code,
                "message": _WORKITEM_DOCUMENT_ERRORS[code].format(document=document),
            },
        )
    return HTTPException(
        status_code=500,
        detail={"code": "scope_unavailable", "message": failure},
    )


def _coerce_workitem_choice(
    raw: object, *, allowed: frozenset[str], fallback: Optional[str],
    field: str, workitem_id: str,
) -> Optional[str]:
    """Map an on-disk enum value onto the declared vocabulary.

    ``Workitem.status`` and ``.state_reason`` are ``Literal``s so the
    OpenAPI schema carries the enums, and the store never writes anything
    outside them. But a synced ``.xo/`` is restored wholesale from
    somewhere else, so "the store wrote it" is not the same claim as
    "this process wrote it" — and this model is built *after* the route's
    ``try``/``except``, so one unexpected value would raise a
    ``ValidationError`` that took the entire project's workitem list with
    it. That is the todos defect (``_coerce_status``) on a second
    document; it is cheaper to not repeat it than to rediscover it.

    ``None`` is always legal — for ``status`` it is how an adopted item
    reads (§5.3), for ``state_reason`` it is the ordinary empty value.
    """
    if raw is None:
        return None
    value = str(raw)
    if value in allowed:
        return value
    logger.warning(
        "workitem %s carries %s %r, which is not in the declared vocabulary "
        "%s; rendering it as %r. Repair the record or delete it.",
        workitem_id or "<no id>", field, value, sorted(allowed), fallback,
    )
    return fallback


def _make_workitem_source(d: dict, *, workitem_id: str) -> WorkitemSource:
    """Shape ``source`` for the wire.

    ``kind`` comes from the store's own :func:`is_adopted` predicate
    rather than from a second reading of the record, so "adopted" means
    here exactly what it means to the store's ``?kind=`` filter and to
    its refusal of GitHub-owned writes. A record whose ``source`` block
    is missing or malformed therefore renders as ``local`` — the same
    answer the store's filter gives it — instead of inventing a third.

    The ``github`` reference is populated only when it is complete and
    well typed; a partial one is logged and dropped rather than served as
    a half-reference that a client would try to link to.
    """
    if not _is_adopted(d):
        return WorkitemSource(kind="local")
    ref = (d.get("source") or {}).get("github")
    if isinstance(ref, dict):
        try:
            return WorkitemSource(kind="github", github=ref)
        except ValidationError:
            pass
    logger.warning(
        "workitem %s is adopted but its source.github reference is not "
        "usable; serving the adoption without it.", workitem_id or "<no id>",
    )
    return WorkitemSource(kind="github")


def _in_progress_ids(scope: scopes.VisualizerScope) -> frozenset[str]:
    """The derived set of workitems an agent is working right now (§5.4).

    A thin pass-through, kept as a named seam because the property that
    matters is *where the answer comes from*: a claim in the runtime
    tier, joined against the sessions the watcher currently observes in
    ``open_sessions``. Nothing is stored, so nothing has to be cleaned
    up when an agent dies — its session leaves the presence snapshot and
    the claim stops counting, with the claim itself untouched on disk.

    The scope method is total by contract (it answers ``frozenset()``
    rather than raising), which is what lets the callers below build
    :class:`Workitem` models outside their ``try``/``except`` without
    reintroducing the failure ``_coerce_workitem_choice`` guards
    against — one unavailable runtime file must not take down a list.
    """
    return scope.in_progress_workitem_ids()


def _mirror_issues(scope: scopes.VisualizerScope) -> dict[str, dict]:
    """The GitHub mirror's issue rows for this project, keyed by node id.

    The other half of the read-time projection (§5.3), and total for the
    same reason :func:`_in_progress_ids` is: one runtime file that is
    absent, unreadable or has never been written must not take down a
    surface whose local half is perfectly fine. ``{}`` is the honest
    reading of every one of those states — GitHub has told us nothing —
    and it is what makes an adopted item render stale rather than 404.
    """
    try:
        return _projection.mirror_issues(scope.read_github_mirror())
    except Exception:  # pragma: no cover - defensive; the read must be total
        logger.warning(
            "could not read the GitHub mirror for project %s; adopted "
            "workitems will render stale", scope.project_id, exc_info=True,
        )
        return {}


def _make_workitem_model(d: dict, *, in_progress: bool = False) -> Workitem:
    """Build the wire model from a stored record, tolerating a bad row.

    Total by construction: every field is coerced or defaulted, because
    the alternative is one malformed record 500-ing a list that is mostly
    fine. Dropping the row instead would be worse still — a unit of work
    that vanishes with no signal is the failure mode the tombstone design
    exists to avoid.

    ``in_progress`` arrives as a plain ``bool`` from
    :func:`_in_progress_ids` rather than being read off ``d``: it is not
    a stored field and there is nothing on the record to read.

    ``d`` is expected to have been through
    :func:`workitem_projection.project_workitem` — so ``status``,
    ``assignee`` and friends may already be the mirror's answer rather
    than the file's, and ``assignees`` / ``stale`` are present. It reads
    a *raw* record just as happily: both projected keys default, which is
    what keeps this function usable on a record the projection never saw.
    """
    workitem_id = str(d.get("id", ""))
    raw_assignees = d.get("assignees")
    raw_labels = d.get("labels")
    raw_links = d.get("links")
    raw_links = raw_links if isinstance(raw_links, dict) else {}
    return Workitem(
        id=workitem_id,
        title=str(d.get("title", "")),
        body=d.get("body"),
        labels=[str(x) for x in raw_labels] if isinstance(raw_labels, list) else [],
        status=_coerce_workitem_choice(
            d.get("status"), allowed=_WORKITEM_STATUSES,
            # The least-committal *open* value: the row stays visible and
            # actionable rather than reading as work someone finished.
            fallback="open", field="status", workitem_id=workitem_id,
        ),
        state_reason=_coerce_workitem_choice(
            d.get("state_reason"), allowed=_WORKITEM_STATE_REASONS,
            fallback=None, field="state_reason", workitem_id=workitem_id,
        ),
        source=_make_workitem_source(d, workitem_id=workitem_id),
        assignee=d.get("assignee"),
        assignees=(
            [str(x) for x in raw_assignees if isinstance(x, str)]
            if isinstance(raw_assignees, list) else []
        ),
        stale=bool(d.get("stale")),
        in_progress=in_progress,
        links=WorkitemLinks(
            todo_ids=[str(x) for x in raw_links.get("todo_ids") or []],
            session_ids=[str(x) for x in raw_links.get("session_ids") or []],
        ),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        created_by=d.get("created_by"),
        deleted_at=d.get("deleted_at"),
        deleted_by=d.get("deleted_by"),
    )


@router.get(
    "/api/xo-projects/{project_id}/workitems",
    response_model=WorkitemsResponse,
)
def project_workitems_list(
    project_id: str,
    status: Optional[str] = Query(
        default=None, description="Filter to `open` or `closed`."
    ),
    assignee: Optional[str] = Query(
        default=None, description="Filter to one assignee."
    ),
    kind: Optional[str] = Query(
        default=None, description="Filter to `local` or `github` (adopted) items."
    ),
    include_deleted: bool = Query(
        False, description="Include soft-deleted workitems (tombstones)."
    ),
) -> WorkitemsResponse:
    """Every workitem in the project, oldest first.

    Empty ``{workitems: []}`` when nothing has written ``workitems.json``
    yet — an absent document is a legitimate state, and the only one that
    reads as empty. A document that exists but cannot be read is a 409,
    never an empty list (see :func:`_workitem_error`).

    A **list**, not the raw map: §7.3's workspace rollup unions these
    across projects, and a list has no union key, so the collision class
    that lost rows in the workspace view (O-C) cannot occur there at all.

    ``status`` and ``assignee`` filter on what is *stored*, so an adopted
    item never matches either — it stores neither field, by design
    (§5.3). The rendered rows *do* carry the mirror's answer for those
    two, because the read-time projection has joined them in; the filter
    deliberately still does not, since a filter that answered from the
    mirror would silently drop every adopted item on a machine that has
    never polled. ``?kind=`` is how you narrow to one population, and the
    workspace rollup (§7.3, W9) is where "assigned to me" is answered
    across projects. **A filtered list therefore has fewer rows than a
    filtered client-side pass over the unfiltered one.**

    Deleted workitems are hidden by default; ``?include_deleted=true``
    returns them with their ``deleted_at`` / ``deleted_by`` set.

    Each row carries the derived ``in_progress`` (§5.4): true iff an
    agent holds a claim on it and that agent's session is still present
    in the watcher's snapshot. There is no ``?in_progress=`` filter,
    deliberately — the store filters on what is *stored*, and this is
    the one field that never is.
    """
    scope = _require_project(project_id)
    try:
        rows = scope.list_workitems(
            status=status,
            kind=kind,
            assignee=assignee,
            include_deleted=include_deleted,
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    live = _in_progress_ids(scope)
    issues = _mirror_issues(scope)
    return WorkitemsResponse(
        project_id=project_id,
        workitems=[
            _make_workitem_model(
                _projection.project_workitem(row, issues=issues),
                in_progress=row.get("id") in live,
            )
            for row in rows
        ],
    )


@router.post(
    "/api/xo-projects/{project_id}/workitems",
    response_model=Workitem,
    status_code=201,
)
def project_workitems_create(
    project_id: str, body: CreateWorkitemRequest,
) -> Workitem:
    """Create a workitem under the project (any runtime can call).

    Creates a **local** item: ``status`` defaults to ``open``, and
    ``assignee`` is local-only — a local workitem is assignable to
    yourself and only to yourself, permanently, because assignment across
    Spaces *is* a GitHub assignee (D1) and a local item never becomes an
    issue (D8). Work that needs to reach a peer is created as an issue
    from the start.

    There is no way to create an *adopted* item here; that is the
    adoption endpoint's job (§7.2, W7) and needs the mirror. See
    ``CreateWorkitemRequest``.
    """
    scope = _require_project(project_id)
    try:
        new = scope.create_workitem(
            runtime=body.runtime,
            title=body.title,
            body=body.body,
            labels=body.labels,
            status=body.status,
            state_reason=body.state_reason,
            assignee=body.assignee,
            todo_ids=body.todo_ids,
            session_ids=body.session_ids,
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    # Projected even though a created item is always local and the mirror
    # can therefore say nothing about it: the projection is what fills
    # ``assignees`` from the single stored ``assignee``, and a create that
    # answered in a different shape from the following GET would be a
    # second wire contract for one record.
    return _make_workitem_model(_projection.project_workitem(new))


@router.get(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}",
    response_model=Workitem,
)
def project_workitems_get(project_id: str, workitem_id: str) -> Workitem:
    """Fetch one workitem by id.

    A soft-deleted workitem is ``404 workitem_not_found`` here, matching
    the list view: the tombstone is history, reachable through
    ``GET /workitems?include_deleted=true``.
    """
    scope = _require_project(project_id)
    try:
        found = scope.get_workitem(workitem_id)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )
    return _make_workitem_model(
        _projection.project_workitem(found, issues=_mirror_issues(scope)),
        in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.patch(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}",
    response_model=Workitem,
)
def project_workitems_update(
    project_id: str, workitem_id: str, body: UpdateWorkitemRequest,
) -> Workitem:
    """Update fields on an existing workitem.

    Only the keys the request carries are touched — and for the three
    nullable ones (``body``, ``state_reason``, ``assignee``) *carrying
    the key with a null* is a different request from omitting it:
    ``{"assignee": null}`` un-assigns, ``{}`` leaves the assignee where
    it was. ``UpdateWorkitemRequest.store_kwargs`` is where that
    distinction is preserved; flattening it would leave no way to
    un-assign a workitem over HTTP at all.

    An idempotent PATCH is genuinely idempotent: a call that changes
    nothing writes nothing and does not advance ``updated_at``.

    For an **adopted** item the four GitHub-owned fields are refused with
    ``400 github_authoritative`` rather than accepted and left to go
    stale (§5.3). Closing an adopted workitem means closing the issue
    (W8), not writing ``closed`` into a file GitHub does not read.

    **Closing releases the claim** (§5.4). Work that is finished is not
    work in progress, and leaving the claim would keep the item reading
    as in progress for as long as the agent's session happened to
    outlive the close. The release is best-effort: it cannot fail a
    PATCH that already succeeded, and a claim that survives it lapses
    with its session anyway.
    """
    scope = _require_project(project_id)
    try:
        updated = scope.update_workitem(workitem_id, **body.store_kwargs())
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    projected = _projection.project_workitem(updated, issues=_mirror_issues(scope))
    if updated.get("status") == "closed":
        scope.release_workitem_quiet(workitem_id)
        return _make_workitem_model(projected, in_progress=False)
    return _make_workitem_model(
        projected, in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.delete(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}",
    response_model=DeleteWorkitemResponse,
)
def project_workitems_delete(
    project_id: str,
    workitem_id: str,
    runtime: Optional[str] = Query(
        default=None,
        description=(
            "Calling runtime, recorded as the tombstone's `deleted_by`. "
            "Optional; the workitem is tombstoned either way. Same charset "
            "as the required `runtime` on create."
        ),
    ),
) -> DeleteWorkitemResponse:
    """Soft delete — the record is tombstoned (``deleted_at`` set), never
    removed, so it cannot come back and the history stays readable. The
    tombstone is deliberately not a ``status``: "we decided not to do
    this" is ``closed`` + ``state_reason: not_planned``, and "this should
    not have existed" is this. Collapsing them would make either one
    unanswerable.

    Idempotent: ``deleted: false`` if the workitem was already absent or
    already tombstoned, never a 404 — same contract as ``DELETE /todos``.

    ``runtime`` fills ``deleted_by``, and it is a query parameter rather
    than a body because DELETE bodies are widely dropped in transit. It
    is here from the start for the O-A reason: on todos this field sat in
    the tombstone design for a release with no route able to set it, so
    every tombstone recorded a null author.
    """
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_workitem(workitem_id, deleted_by=runtime)
    except Exception as exc:
        # An invalid ``runtime`` is a caller error, not a write failure:
        # the request never reached disk, and reporting "write failed"
        # would send the caller looking in the wrong place.
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    if deleted:
        # Same implicit release as closing (§5.4): a tombstoned workitem
        # is not work in progress. Best-effort — the tombstone is
        # already written and a stranded claim lapses with its session.
        scope.release_workitem_quiet(workitem_id)
    return DeleteWorkitemResponse(
        project_id=project_id, workitem_id=workitem_id, deleted=deleted,
    )


# ── /api/xo-projects/{id}/workitems/{id}/claim — derived in_progress ────────
#
# §5.4, task W7b. "In progress" is **not** a status: ``status`` is
# GitHub's ``open``/``closed`` and nothing else (D7), so there is no
# third value to write and no overlay to reconcile. A workitem is in
# progress iff an agent is currently working it, and that is derived on
# every read from a claim in the runtime tier joined against the
# sessions the watcher currently observes.
#
# The API is therefore a **claim, not a status write**. The distinction
# is the whole point. A status write leaves a flag someone has to
# un-write; when the process holding it dies, the flag stays and lies,
# and correcting it needs a cleanup path that has to survive the same
# crash. A claim needs none: liveness comes from ``open_sessions``,
# which the watcher rebuilds each tick from ``poll_presence()``, so a
# session that stops being present stops appearing and the derived state
# evaporates with the claim left exactly as it was written. This is the
# principle T22 established for the watcher's own ``alive``.
#
# Liveness is deliberately **not** taken from ``ended_at``: that field
# is documented in ``sinks/sessions_augment.py`` as "currently always
# null (filled once session-close detection lands)", and session-close
# detection does not exist here — waiting for it would leave every claim
# live forever, which is the exact failure this design avoids.
#
# Claims live at ``~/.quirq/projects/<pid>/workitems/claims.json``:
# machine-local, disposable, never ``.xo/``. Two Spaces working the same
# GitHub issue each keep their own file, so each shows its own agent's
# progress and neither can overwrite the other's.


@router.post(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}/claim",
    response_model=WorkitemClaim,
)
def project_workitems_claim(
    project_id: str, workitem_id: str, body: ClaimWorkitemRequest,
) -> WorkitemClaim:
    """Record that a session is working this workitem.

    An upsert: re-claiming refreshes the claim, and a claim from another
    session replaces it. There is no conflict status — the document is
    machine-local, so the cross-Space case cannot reach it, and a claim
    held by a session that has since died would otherwise need a
    takeover rule before the workitem could be claimed again.

    The workitem must exist and not be tombstoned (``404
    workitem_not_found``), which keeps ``claims.json`` from accumulating
    keys that name nothing.

    The response re-derives ``in_progress`` rather than asserting
    ``true``. Usually it is true, on the strength of the grace window
    for a session whose presence row has not appeared yet; if it is
    false, something is wrong with the session id and saying so beats a
    confident lie.
    """
    scope = _require_project(project_id)
    try:
        found = scope.get_workitem(workitem_id)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )
    try:
        claim = scope.claim_workitem(
            workitem_id, session_id=body.session_id, runtime=body.runtime,
        )
    except Exception as exc:
        raise _workitem_error(
            exc,
            failure="the workitem claim could not be recorded.",
            document="claims.json",
        ) from exc
    return WorkitemClaim(
        project_id=project_id,
        workitem_id=workitem_id,
        session_id=claim["session_id"],
        runtime=claim["runtime"],
        started_at=claim["started_at"],
        in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.delete(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}/claim",
    response_model=ReleaseWorkitemClaimResponse,
)
def project_workitems_release(
    project_id: str, workitem_id: str,
) -> ReleaseWorkitemClaimResponse:
    """Drop the claim — the explicit half of "stopped working on this".

    Idempotent (``released: false`` when there was nothing to drop),
    never a 404 on the claim, matching ``DELETE /workitems``. It is a
    courtesy rather than a requirement: an agent that never calls it,
    or that dies before it can, loses the claim's effect anyway as soon
    as its session stops being observed. That is the acceptance
    criterion for W7b — no cleanup path.

    A workitem that does not exist is still a ``404``, so a typo in the
    id is reported rather than silently answered ``released: false``.
    """
    scope = _require_project(project_id)
    try:
        found = scope.get_workitem(workitem_id, include_deleted=True)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )
    try:
        released = scope.release_workitem(workitem_id)
    except Exception as exc:
        raise _workitem_error(
            exc,
            failure="the workitem claim could not be released.",
            document="claims.json",
        ) from exc
    return ReleaseWorkitemClaimResponse(
        project_id=project_id, workitem_id=workitem_id, released=released,
    )


# ── /api/xo-projects/{id}/github/* — the mirror, and adoption ───────────────
#
# §7.2, tasks W7 and W8. Three facts shape everything below.
#
# **1. Coordination lives in GitHub** (D1). Assignment is a GitHub assignee,
# because GitHub is already a shared, concurrent, conflict-free, audited store
# with an assignment primitive and this system already has `gh` auth. Two
# Spaces assigning at once is GitHub's problem, and GitHub has solved it. The
# cost, stated plainly: work that is not a GitHub issue cannot be assigned
# across Spaces, and there is no mechanism to make it one (D8 removed
# promotion). A local workitem is self-assignable, permanently.
#
# **2. Adoption is explicit** (D2). The mirror holds every issue in the repo;
# `.xo/workitems.json` holds only what someone *chose* to track. So this
# surface has a browse view (the mirror, with an untracked count) and an
# adopt verb, rather than an importer.
#
# **3. The poller is the mirror's only writer.** These routes read it and
# never write it — not even to save a UI 60 seconds of staleness after an
# assignment. `github_mirror`'s merge rules assume one writer, and an
# assignment answered with `pending: true` is honest where a forged row would
# not be.
#
# `GET /github/issues` is also where D9's lazy-polling hook is called.
# `github_poller.note_interest()` existed with no caller — there is no viewing
# signal anywhere in this system — and this route is the closest thing to one:
# asking for a project's issues is what a person does when they are looking at
# it. Marking it interesting is what makes the poller refresh a repo that has
# no adopted items yet, which is exactly the state someone browsing to adopt
# is in.


#: Every kind in ``connectors/github_issues.ERROR_KINDS``, mapped onto the
#: answer it deserves. A table rather than a chain of ``if``s because the
#: vocabulary is closed and a test asserts this covers it — a new failure kind
#: must not reach a caller as an unhandled 500.
#:
#: The choices that needed making: ``not_authenticated`` is **503, not 401** —
#: the caller of *this* API is fine, it is the Space's GitHub credential that
#: is missing, and a 401 here would send a UI to re-authenticate the wrong
#: thing. ``rate_limited`` is 503 because waiting genuinely does help, and it
#: is the one case where that is true. ``network``/``timeout``/``bad_response``
#: are 502: we are a gateway to GitHub and GitHub is what failed.
_GITHUB_FAILURES: dict[str, tuple[int, str]] = {
    "no_cli": (503, "github_unavailable"),
    "not_authenticated": (503, "github_not_connected"),
    "forbidden": (403, "github_forbidden"),
    "not_found": (404, "issue_not_found"),
    "rate_limited": (503, "github_rate_limited"),
    "network": (502, "github_unavailable"),
    "timeout": (502, "github_unavailable"),
    "bad_remote": (400, "not_a_github_project"),
    "bad_response": (502, "github_unavailable"),
    "unknown": (502, "github_unavailable"),
}


def _github_error(kind: Optional[str], message: Optional[str]) -> HTTPException:
    """One ``gh`` failure, as an HTTP answer.

    The message is forwarded rather than replaced: it is composed from gh's
    own text, which never contains the token (the client puts it in the
    child's environment and nowhere else), and it says the thing the operator
    needs — which repository, which credential, which limit.
    """
    status, code = _GITHUB_FAILURES.get(kind or "", (502, "github_unavailable"))
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message or f"GitHub call failed ({kind})."},
    )


def _require_github_repo(scope: scopes.VisualizerScope) -> str:
    """The project's ``owner/name``, or a 400 that says why there is none.

    Read from the durable ``project.json:git.remote_url`` — the same input
    the poller uses, so "a project the poller polls" and "a project these
    routes can act on" are the same set by construction rather than by
    coincidence.
    """
    repo = scope.github_repo()
    if not repo:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_a_github_project",
                "message": (
                    "This project has no github.com remote in project.json, so "
                    "there are no issues to adopt and no assignee to write. "
                    "Set a GitHub origin, or keep the work as local workitems."
                ),
            },
        )
    return repo


def _github_budget_gate() -> None:
    """Refuse an interactive GitHub call the poller has already stood down for.

    The budget is **one global thing** (§6.3) and this is how an interactive
    call shares it. The poller stops spending at
    ``BUDGET_RESERVE_POINTS`` remaining *specifically* to leave room for these
    calls, so this gate does not stop at the reserve — it stops at nothing
    left, and at a pause the poller has taken. A pause means one of the two
    states that are true of the whole machine rather than of one repository:
    GitHub is rate-limiting us until ``resetAt``, or there is no usable
    credential. Spending into either would be hammering, and hammering a
    secondary rate limit is how the primary one arrives.

    What it deliberately does *not* do is charge the poller's budget for what
    it spends. That would need a public write on ``github_poller``'s global,
    and the accounting self-corrects anyway: the poller reads GitHub's own
    ``rateLimit.remaining`` from inside its very next query, so an interactive
    point is visible within one tick. One or two points per human action
    against 5,000/hour is not what moves that number.
    """
    snapshot = github_poller.budget_snapshot()
    if snapshot.get("paused"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "github_rate_limited",
                "message": (
                    "GitHub calls are paused: "
                    f"{snapshot.get('pause_reason') or 'the poller backed off'}. "
                    "This affects the whole machine, not this project, and it "
                    "clears on its own."
                ),
            },
        )
    remaining = snapshot.get("remaining")
    if isinstance(remaining, int) and remaining <= 0:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "github_rate_limited",
                "message": (
                    "The GraphQL budget is spent; it refills at "
                    f"{snapshot.get('reset_at') or 'the next reset'}."
                ),
            },
        )


def _issue_number(row: dict) -> int:
    """A row's issue number, or ``0`` when it does not have a usable one.

    Its own helper because it is read twice — once to render and once to
    sort — and a sort key that could be a string next to an int raises
    ``TypeError`` on a route whose whole job is to keep rendering.
    """
    number = row.get("number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return number
    return 0


def _issue_model(row: dict, tracked: dict[str, str]) -> GithubIssue:
    """One mirror row on the wire, tolerating a row that is not quite right.

    Total by construction, like ``_make_workitem_model``: the mirror is
    rebuilt by a background loop and read on a request, and one row this
    revision does not understand must not cost the caller the other eighty.
    """
    node_id = str(row.get("node_id") or "")
    assignees = []
    raw_assignees = row.get("assignees")
    for person in raw_assignees if isinstance(raw_assignees, list) else []:
        if not isinstance(person, dict):
            continue
        login = person.get("login")
        if isinstance(login, str) and login:
            avatar = person.get("avatar_url")
            assignees.append(GithubIssueAssignee(
                login=login,
                avatar_url=avatar if isinstance(avatar, str) else None,
            ))
    labels = row.get("labels")
    return GithubIssue(
        node_id=node_id,
        number=_issue_number(row),
        title=str(row.get("title") or ""),
        state=_coerce_workitem_choice(
            row.get("state"), allowed=_WORKITEM_STATUSES,
            # ``None``, not ``open``: this is the mirror's own claim about an
            # issue, and inventing "open" for a row we cannot read would be a
            # statement about who owes what. Absent beats wrong (§5.3).
            fallback=None, field="state", workitem_id=f"{node_id} (mirror row)",
        ),
        state_reason=_coerce_workitem_choice(
            row.get("state_reason"), allowed=_WORKITEM_STATE_REASONS,
            fallback=None, field="state_reason",
            workitem_id=f"{node_id} (mirror row)",
        ),
        assignees=assignees,
        # ``None`` when the poller wrote the row, because it does not fetch
        # labels — and an empty list would be the claim that the issue has
        # none, which the poll never establishes (§5.2, amendment 2).
        labels=(
            [str(x) for x in labels] if isinstance(labels, list) else None
        ),
        url=str(row.get("url") or ""),
        updated_at=row.get("updated_at") if isinstance(row.get("updated_at"), str) else None,
        adopted=node_id in tracked,
        workitem_id=tracked.get(node_id),
    )


@router.get(
    "/api/xo-projects/{project_id}/github/issues",
    response_model=GithubIssuesResponse,
)
def project_github_issues(project_id: str) -> GithubIssuesResponse:
    """The GitHub issue mirror for this project, and what is untracked.

    A read of the **runtime** tier — ``~/.quirq/projects/<pid>/github/
    issues.json``, refreshed by the poller, never by this route. It makes no
    network call, so it answers the same whether GitHub is reachable or not:
    the newest rows the poller managed to fetch, ``fetched_at`` saying when
    that was, and ``error`` saying what went wrong last time it tried. Those
    are two different facts and a single timestamp would collapse them into a
    lie, which is why the mirror keeps both.

    Before the first successful poll everything is empty and ``fetched_at`` is
    null — a real state, not a failure. It is also the state this route exists
    to end: it calls ``github_poller.note_interest()``, D9's lazy-polling hook,
    which had no caller at all. Only projects that are being looked at, hold
    adopted items, or have a live agent session are polled, and *being looked
    at* had no signal in this system until here. Asking for a project's issues
    is that signal. The mark expires on its own
    (``XO_GITHUB_POLL_INTEREST_TTL_S``), so a project someone opened once does
    not consume budget forever.

    ``untracked`` counts the **open** issues no workitem tracks — the number a
    UI puts on a badge. Closed issues nobody adopted are not work anyone is
    being asked to notice, so counting them would make the badge grow forever
    on an old repository.
    """
    scope = _require_project(project_id)
    # D9's hook, and the whole "being looked at" signal. Cheap, idempotent,
    # touches no file, and cannot raise — safe on a request thread.
    github_poller.note_interest(project_id)

    mirror = scope.read_github_mirror()
    issues = _projection.mirror_issues(mirror)
    try:
        records = scope.list_workitems()
    except Exception:
        # The workitems document being unreadable is a real 409 on the
        # workitems surface, and it is answered there. Here it would cost the
        # caller the browse view as well, over a field — ``adopted`` — that
        # degrades to "nothing is tracked yet". The refusal that matters is
        # not skipped, it is just not this route's to make.
        logger.warning(
            "project %s: workitems.json is unreadable; serving the issue "
            "mirror with nothing marked as tracked", project_id, exc_info=True,
        )
        records = []
    tracked = _projection.tracked_node_ids(records)

    rows = sorted(
        issues.values(),
        key=lambda row: (str(row.get("updated_at") or ""), _issue_number(row)),
        reverse=True,
    )
    models = [_issue_model(row, tracked) for row in rows]
    untracked = sum(
        1 for row in models if row.state == "open" and not row.adopted
    )

    error = None
    raw_error = (mirror or {}).get("error")
    if isinstance(raw_error, dict) and isinstance(raw_error.get("kind"), str):
        error = GithubMirrorError(
            kind=str(raw_error.get("kind")),
            message=str(raw_error.get("message") or raw_error.get("kind")),
            at=raw_error.get("at") if isinstance(raw_error.get("at"), str) else None,
        )

    fetched_at = (mirror or {}).get("fetched_at")
    return GithubIssuesResponse(
        project_id=project_id,
        # The project's own remote, not the mirror's ``repo``: they differ
        # exactly when the remote has changed under a mirror the poller has
        # not reseeded yet, and the honest answer to "which repo is this
        # project" is the project's.
        repo=scope.github_repo(),
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        error=error,
        issues=models,
        untracked=untracked,
        tracked=sum(1 for row in models if row.adopted),
    )


def _mirror_row_for_number(
    scope: scopes.VisualizerScope, number: int
) -> Optional[dict]:
    """The mirror's row for issue ``number``, or ``None``.

    A linear scan, because the mirror is keyed by ``node_id`` — deliberately,
    since that is the identifier that survives a repository rename — and the
    number is what a URL carries. At one repository's page of issues the scan
    is not worth an index.
    """
    for row in _mirror_issues(scope).values():
        if row.get("number") == number:
            return dict(row)
    return None


@router.post(
    "/api/xo-projects/{project_id}/github/issues/{issue_number}/adopt",
    response_model=Workitem,
    status_code=201,
)
async def project_github_issue_adopt(
    project_id: str,
    issue_number: int,
    body: AdoptIssueRequest,
    response: Response,
) -> Workitem:
    """Track a GitHub issue as a workitem (§7.2, D2).

    What lands in the synced document is the **adoption record**: the issue
    reference, plus ``title`` and ``labels`` as a one-time snapshot that is
    never refreshed. ``status``, ``state_reason``, ``assignee`` and ``body``
    are not stored at all — GitHub owns them, and a stale ``closed`` or a
    stale assignee is a false statement about who owes what (§5.3). The
    snapshot is what keeps the item readable when the mirror is gone; without
    it a stale adopted item renders as ``repo#42``, which is not a work item
    anyone can act on.

    **The labels cost one GraphQL point, and that is the whole reason for the
    call.** The poll deliberately does not fetch labels — a nested ``labels``
    connection on a page of 100 issues was measured to double its cost and
    halve the project ceiling from ~83 repos to ~41 (§6.2) — so they are
    fetched *lazily, at adoption*, which is a human act and therefore rare.
    One extra point per adoption buys a mirror that stays cheap and
    single-writer.

    Falls back to the mirror when GitHub cannot be reached and the issue is
    already there: adoption then succeeds without labels rather than failing,
    because the reference is the part that matters and labels are cosmetic.
    With neither, it is a 502/503 that names which of the two states it is.

    Idempotent on the issue: adopting one that is already tracked returns the
    existing workitem with **200** instead of minting a second record. The
    check is inside the store's lock, because "look it up, then create it"
    across two calls is how one issue ends up with two workitems.

    ``workitem_id`` in the body points adoption at an existing **local**
    workitem instead, so a note someone has already been keeping — with its
    todo links and its history — becomes the record for the issue. That is
    not D8's promotion, which runs the other way: nothing is created on
    GitHub, and the issue was already public.
    """
    scope = _require_project(project_id)
    repo = _require_github_repo(scope)

    _github_budget_gate()
    fetched = await github_issue_actions.fetch_issue(repo, issue_number)
    issue = fetched.issue
    if issue is None:
        # The mirror is the fallback, and it is a good one: it is the same
        # reference, written by the same client, minus the labels this call
        # exists to add. Adoption without labels beats no adoption.
        issue = _mirror_row_for_number(scope, issue_number)
        if issue is None:
            raise _github_error(fetched.error_kind, fetched.error)
        logger.warning(
            "project %s: adopting %s#%s from the mirror (%s: %s); the label "
            "snapshot will be empty",
            project_id, repo, issue_number, fetched.error_kind, fetched.error,
        )

    try:
        record, created = await asyncio.to_thread(
            scope.adopt_workitem,
            runtime=body.runtime,
            github={
                "repo": issue.get("repo") or repo,
                "number": issue.get("number") or issue_number,
                "node_id": issue.get("node_id"),
                "url": issue.get("url"),
            },
            title=issue.get("title") or f"{repo}#{issue_number}",
            labels=[str(x) for x in issue.get("labels") or []],
            workitem_id=body.workitem_id,
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc

    if not created:
        # Already tracked, or an existing workitem now mirrors the issue.
        # Nothing was minted, so 201 would be a lie a client may act on.
        response.status_code = 200
    return _make_workitem_model(
        _projection.project_workitem(record, issues=_mirror_issues(scope)),
        in_progress=record.get("id") in _in_progress_ids(scope),
    )


@router.delete(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}/adoption",
    response_model=Workitem,
)
def project_workitem_unadopt(project_id: str, workitem_id: str) -> Workitem:
    """Stop mirroring the issue. **Keep the workitem** (§7.2).

    The inverse of adoption and not a delete: the record, its links and its
    history stay, and only the issue reference goes. That is why it is
    ``DELETE …/adoption`` rather than ``DELETE …/workitems/{id}``, which
    tombstones.

    It **materialises** the four GitHub-owned fields in the same write that
    drops ``source.github``, because the schema forbids them on an adopted
    record and requires ``status`` on a local one — the transition cannot be
    two writes without an invalid document in between. The values come from
    the projection the caller was just being served, so the item looks the
    same across the transition; with no mirror to read, ``status`` falls back
    to ``open``, the least-committal value.

    The **assignee is not materialised**, deliberately. A local workitem is
    self-assignable and permanently so (D1, D8), so importing a peer's GitHub
    login into it would leave a record asserting an assignment that nothing
    coordinates and nobody would ever see change. Un-adopting drops it.

    Idempotent: a workitem that is already local comes back unchanged, which
    matches every other DELETE on this surface. A workitem that does not exist
    is still a 404, so a typo is reported rather than silently succeeding.
    """
    scope = _require_project(project_id)
    try:
        found = scope.get_workitem(workitem_id)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )

    projected = _projection.project_workitem(found, issues=_mirror_issues(scope))
    try:
        record = scope.unadopt_workitem(
            workitem_id,
            status=projected.get("status"),
            state_reason=projected.get("state_reason"),
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    return _make_workitem_model(
        _projection.project_workitem(record),
        in_progress=workitem_id in _in_progress_ids(scope),
    )


# ── /api/xo-projects/{id}/workitems/{id}/assignee — W8 ──────────────────────
#
# Assignment is the one field this system does not own. For an adopted item
# it is written to GitHub and read back by the next poll (D1); for a local
# item it is written to the file and can only ever name yourself (D8). The
# two are one endpoint because they are one question to a caller, and the
# response says which of the two happened rather than pretending they are
# the same.


#: The spellings of "me". Accepted for both kinds, resolved differently:
#: to this Space's GitHub login for an adopted item, to its own user id for
#: a local one. §4 is why no mapping table is needed for either — each Space
#: only has to recognise itself, and for adopted items GitHub does the
#: routing.
_SELF_ALIASES: frozenset[str] = frozenset({"me", "@me", "self"})


def _self_identities() -> list[str]:
    """The names this Space answers to for a **local** assignment.

    No network and no subprocess: a local workitem must be assignable with
    GitHub switched off entirely, which is most of the point of it existing.
    ``resolve_user_id`` is the authenticated user when there is one, the Coder
    workspace owner otherwise, and ``"local"`` off Coder — the last of which
    identifies nobody, but it is the honest answer and it is what every other
    record in this system already writes.
    """
    out: list[str] = []
    for value in (
        coder_identity.resolve_user_id(),
        coder_identity.space_id(),
        coder_identity.owner_name(),
    ):
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


async def _self_github_login() -> Optional[str]:
    """This Space's own GitHub login, or ``None``.

    Two paths because there are two populations. A user who pasted a PAT has
    a token in ``mcp-tokens.json`` and possibly no ``gh`` session at all, and
    ``validate_token`` answers for them without a subprocess (§4 — it already
    returns ``username`` from ``GET /user``). A user who ran the device-flow
    login has a ``gh`` session, and may have no stored token; ``gh api user``
    answers for them. Trying the cheap one first and falling through is what
    makes "assign to me" work in both.

    Not cached. The call happens only when someone assigns with ``me``, which
    is rare, and a cached login that went stale after a re-auth would assign
    work to the wrong person — the one failure mode this whole design exists
    to avoid.
    """
    try:
        token = github_connector.get_github_token()
    except Exception:  # pragma: no cover - the token store is best-effort here
        token = None
    if token:
        try:
            result = await github_connector.validate_token(token)
        except Exception:  # pragma: no cover - validate_token catches its own
            result = {}
        login = result.get("username") if isinstance(result, dict) else None
        if result.get("valid") and isinstance(login, str) and login:
            return login
    found = await github_issue_actions.authenticated_login()
    return found.login if found.ok else None


@router.put(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}/assignee",
    response_model=WorkitemAssignment,
)
async def project_workitem_assign(
    project_id: str, workitem_id: str, body: AssignWorkitemRequest,
) -> WorkitemAssignment:
    """Set (or clear) who owes this workitem.

    **Adopted item — the write goes to GitHub.** ``PATCH
    /repos/{owner}/{repo}/issues/{n}`` with an ``assignees`` array (§3),
    through ``gh`` (D5). Nothing is written into ``.xo/workitems.json``: the
    store refuses the field outright for an adopted item, and a local copy
    would be the stale assignee §5.3 forbids. The response carries
    ``pending: true`` — GitHub has it, and this Space learns it back from the
    next poll of the mirror, up to a minute later.

    GitHub *accepts* a login that cannot be assigned — someone without access
    to the repository — and silently drops it from the array, answering 200
    with the assignees unchanged. So the result set is compared with what was
    asked for and a silent drop becomes ``400 assignee_not_assignable``
    rather than a success the caller has to notice for themselves.

    **Local item — anyone but yourself is refused.** Assignment across Spaces
    *is* a GitHub assignee (D1), and D8 removed promotion, so a local workitem
    is permanently self-assignable only. The refusal is a ``400`` that names
    the accepted values, not a silent success and not a shrug: work that needs
    to reach a peer has to be created as a GitHub issue in the first place,
    and discovering that after the fact is the failure this endpoint exists to
    prevent.

    ``null`` clears the assignee in both cases.
    """
    scope = _require_project(project_id)
    try:
        found = scope.get_workitem(workitem_id)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )

    wanted = (body.assignee or "").strip() or None
    if _is_adopted(found):
        return await _assign_on_github(scope, project_id, workitem_id, found, wanted)
    return await _assign_locally(scope, project_id, workitem_id, wanted)


async def _assign_locally(
    scope: scopes.VisualizerScope,
    project_id: str,
    workitem_id: str,
    wanted: Optional[str],
) -> WorkitemAssignment:
    """The local half: yourself, or nobody. No network, ever."""
    identities = _self_identities()
    if wanted is not None:
        resolved = identities[0] if wanted.lower() in _SELF_ALIASES else wanted
        if resolved not in identities:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "local_assignee_only",
                    "message": (
                        f"A local workitem is assignable only to yourself, "
                        f"permanently. Assignment across Spaces is a GitHub "
                        f"assignee, and a local workitem never becomes a GitHub "
                        f"issue — so {wanted!r} is a name nothing would route "
                        f"to. Accepted: 'me', or {identities}. To hand this "
                        f"work to someone else, create it as a GitHub issue "
                        f"and adopt that."
                    ),
                },
            )
    else:
        resolved = None

    try:
        updated = await asyncio.to_thread(
            scope.update_workitem, workitem_id, assignee=resolved,
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    stored = updated.get("assignee")
    return WorkitemAssignment(
        project_id=project_id,
        workitem_id=workitem_id,
        kind="local",
        assignee=stored,
        assignees=[stored] if isinstance(stored, str) and stored else [],
        # The file is the record for a local item, and it is already written.
        pending=False,
    )


async def _assign_on_github(
    scope: scopes.VisualizerScope,
    project_id: str,
    workitem_id: str,
    record: dict,
    wanted: Optional[str],
) -> WorkitemAssignment:
    """The adopted half: GitHub is the store, and the poll is the read-back."""
    ref = (record.get("source") or {}).get("github")
    number = ref.get("number") if isinstance(ref, dict) else None
    repo = ref.get("repo") if isinstance(ref, dict) else None
    if not isinstance(number, int) or not isinstance(repo, str) or not repo:
        # The record says it mirrors an issue but cannot say which. Refusing
        # is the only safe answer: guessing a repository would assign work on
        # somebody else's.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "corrupt_document",
                "message": (
                    "This workitem is adopted but its issue reference is "
                    "unusable, so there is no issue to assign. Un-adopt it "
                    "(DELETE its /adoption) and adopt the issue again."
                ),
            },
        )

    # Before anything that reaches GitHub — resolving ``me`` is itself a
    # call, and a paused poller means the machine has been told to stand
    # down, not that this one request is special.
    _github_budget_gate()

    login = wanted
    if login is not None and login.lower() in _SELF_ALIASES:
        login = await _self_github_login()
        if not login:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "github_not_connected",
                    "message": (
                        "This Space does not know its own GitHub login, so "
                        "'me' cannot be resolved. Connect GitHub, or assign by "
                        "login explicitly."
                    ),
                },
            )
    if login is not None:
        login = login.lstrip("@")

    result = await github_issue_actions.set_assignees(
        repo, number, [login] if login else [],
    )
    if not result.ok:
        raise _github_error(result.error_kind, result.error)
    if login and login not in result.assignees:
        # GitHub answered 200 and did not assign them. That is its documented
        # behaviour for a login without access to the repository, and it is
        # exactly the "appears to succeed" this task refuses to reproduce.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "assignee_not_assignable",
                "message": (
                    f"GitHub accepted the request but did not assign "
                    f"{login!r} to {repo}#{number}: it silently drops an "
                    f"assignee who cannot be assigned. They need access to "
                    f"the repository. Assignees are now "
                    f"{result.assignees or 'none'}."
                ),
            },
        )

    # **The one workitem event emitted from a route, and only because
    # there is no write to hang it on.** Every other ``workitem.*`` line
    # comes out of the store, so it cannot be missed by a caller taking a
    # different path. This assignment wrote to GitHub and deliberately
    # wrote nothing locally (§5.3), yet D1 makes the GitHub assignee the
    # one that actually routes work across Spaces — so without this the
    # timeline would carry ``workitem.assigned`` for local items only,
    # which is the half that matters least. Addressed by ``project_id``
    # so this module still resolves no paths of its own.
    _emit_workitem_events(scope.project_id, [
        WorkitemEvent(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            action="assigned",
            workitem_id=workitem_id,
            assignee=result.assignees[0] if result.assignees else None,
        )
    ])
    return WorkitemAssignment(
        project_id=project_id,
        workitem_id=workitem_id,
        kind="github",
        assignee=result.assignees[0] if result.assignees else None,
        assignees=list(result.assignees),
        # GitHub has it; this Space reads it back from the next poll. Nothing
        # was written to ``.xo/`` and nothing was written to the mirror — the
        # poller is its only writer, and a forged row to save 60 seconds is
        # the stale value §5.3 forbids, arrived at on purpose.
        pending=True,
    )


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
