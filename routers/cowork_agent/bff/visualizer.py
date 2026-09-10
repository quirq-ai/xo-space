"""Project-scope BFF endpoints over one project's state."""

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
    CreatePeerRequest,
    DeletePeerResponse,
    OpenSession,
    Peer,
    PeersResponse,
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
    UpdatePeerRequest,
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
from services.cowork_agent.connectors import github as github_connector
from services.cowork_agent.connectors.github import issue_actions as github_issue_actions
from services.cowork_agent.visualizer import workitem_projection as _projection
from services.cowork_agent.visualizer.peers_store import VALID_ROLES as _PEER_ROLES
from services.cowork_agent.visualizer.todo_status import VALID_TODO_STATUSES
from services.cowork_agent.visualizer.workitems_store import (
    VALID_STATE_REASONS as _WORKITEM_STATE_REASONS,
    VALID_STATUSES as _WORKITEM_STATUSES,
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
    """Convert the on-disk ``todos.json`` shape to the wire shape."""
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
    """Per-session task list for one project."""
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
# EVERY agent writes todos through this API rather than touching .xo/todos.json
# directly — including runtimes with a native todo tool, whose tool calls no
# longer reach any watcher sink.


#: What an unrecognised on-disk status renders as.
_STATUS_FALLBACK = "pending"


def _coerce_status(raw: object, *, todo_id: str) -> str:
    """Map an on-disk status onto the declared vocabulary."""
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
    """Fetch one todo by id."""
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
    """
    Soft delete — the record is tombstoned (``deleted_at`` set), never removed,
    so it cannot come back and the history stays readable.
    """
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_todo(todo_id, deleted_by=runtime)
    except Exception as exc:
        # An invalid ``runtime`` is a caller error, not a write failure.
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
# The project tier of workitems-plan §7.1, and deliberately the todos surface
# again: same ``runtime`` vocabulary, same tombstone semantics, same ``{"code":
# ..., "message": ...}`` 400 bodies, same optional ``?runtime=`` on DELETE.


#: Store codes that mean *the caller asked for something invalid*.
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

#: Store codes that mean *the document on disk cannot be acted on*, with the
#: message served in their place. Shared by every authored document.
_DOCUMENT_ERRORS: dict[str, str] = {
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


def _store_error(
    exc: Exception,
    *,
    failure: str,
    document: str,
    not_found: tuple[str, str],
    caller_errors: frozenset[str],
    conflicts: frozenset[str] = frozenset(),
) -> HTTPException:
    """Map a store's ``(code, message)`` failure onto its HTTP answer."""
    code = getattr(exc, "code", None)
    if code == not_found[0]:
        return HTTPException(
            status_code=404, detail={"code": code, "message": not_found[1]},
        )
    if code in caller_errors:
        return HTTPException(
            status_code=400, detail={"code": code, "message": str(exc)},
        )
    if code in conflicts:
        # The store's message names no path and says what to do instead, which
        # a generic 409 could not, so it is served as written.
        return HTTPException(
            status_code=409, detail={"code": code, "message": str(exc)},
        )
    if code in _DOCUMENT_ERRORS:
        # The store's message names the path; log it, don't serve it.
        logger.error("%s refused (%s): %s", document, code, exc)
        return HTTPException(
            status_code=409,
            detail={
                "code": code,
                "message": _DOCUMENT_ERRORS[code].format(document=document),
            },
        )
    return HTTPException(
        status_code=500,
        detail={"code": "scope_unavailable", "message": failure},
    )


def _workitem_error(
    exc: Exception, *, failure: str, document: str = "workitems.json",
) -> HTTPException:
    """Map a ``WorkitemsStoreError`` onto its HTTP answer."""
    return _store_error(
        exc,
        failure=failure,
        document=document,
        not_found=("workitem_not_found", "Workitem not found."),
        caller_errors=_WORKITEM_CALLER_ERRORS,
    )


def _coerce_workitem_choice(
    raw: object, *, allowed: frozenset[str], fallback: Optional[str],
    field: str, workitem_id: str,
) -> Optional[str]:
    """Map an on-disk enum value onto the declared vocabulary."""
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
    """Shape ``source`` for the wire."""
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


def _require_workitem(
    scope: scopes.VisualizerScope, workitem_id: str, *, include_deleted: bool = False,
) -> dict:
    """The stored record, or the 404/4xx the store's failure deserves."""
    try:
        found = scope.get_workitem(workitem_id, include_deleted=include_deleted)
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json is not readable.",
        ) from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workitem_not_found", "message": "Workitem not found."},
        )
    return found


def _in_progress_ids(scope: scopes.VisualizerScope) -> frozenset[str]:
    """The derived set of workitems an agent is working right now (§5.4)."""
    return scope.in_progress_workitem_ids()


def _mirror_issues(scope: scopes.VisualizerScope) -> dict[str, dict]:
    """The GitHub mirror's issue rows for this project, keyed by node id."""
    try:
        return _projection.mirror_issues(scope.read_github_mirror())
    except Exception:  # pragma: no cover - defensive; the read must be total
        logger.warning(
            "could not read the GitHub mirror for project %s; adopted "
            "workitems will render stale", scope.project_id, exc_info=True,
        )
        return {}


def _make_workitem_model(d: dict, *, in_progress: bool = False) -> Workitem:
    """Build the wire model from a stored record, tolerating a bad row."""
    workitem_id = str(d.get("id", ""))
    raw_assignees = d.get("assignees")
    raw_github_assignees = d.get("github_assignees")
    raw_labels = d.get("labels")
    raw_links = d.get("links")
    raw_links = raw_links if isinstance(raw_links, dict) else {}
    source = _make_workitem_source(d, workitem_id=workitem_id)
    assignee = d.get("assignee")
    assignee = assignee if isinstance(assignee, str) and assignee else None
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
        source=source,
        origin="github" if source.kind == "github" else "space",
        assignee=assignee,
        assigned=assignee is not None,
        assignees=(
            [str(x) for x in raw_assignees if isinstance(x, str)]
            if isinstance(raw_assignees, list) else []
        ),
        github_assignees=(
            [str(x) for x in raw_github_assignees if isinstance(x, str)]
            if isinstance(raw_github_assignees, list) else []
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
    """Every workitem in the project, oldest first."""
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
    """Create a workitem under the project (any runtime can call)."""
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
    # Projected even though a created item is always local and the mirror can
    # therefore say nothing about it: the projection is what fills
    # ``assignees``/``github_assignees`` beside the stored ``assignee``, and a
    # create that answered in a different shape from the following GET would be
    # a second wire contract for one record.
    return _make_workitem_model(_projection.project_workitem(new))


@router.get(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}",
    response_model=Workitem,
)
def project_workitems_get(project_id: str, workitem_id: str) -> Workitem:
    """Fetch one workitem by id."""
    scope = _require_project(project_id)
    found = _require_workitem(scope, workitem_id)
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
    """Update fields on an existing workitem."""
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
    """
    Soft delete — the record is tombstoned (``deleted_at`` set), never removed,
    so it cannot come back and the history stays readable.
    """
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_workitem(workitem_id, deleted_by=runtime)
    except Exception as exc:
        # An invalid ``runtime`` is a caller error, not a write failure: the
        # request never reached disk, and reporting "write failed" would send
        # the caller looking in the wrong place.
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc
    if deleted:
        # Same implicit release as closing (§5.4): a tombstoned workitem is not
        # work in progress.
        scope.release_workitem_quiet(workitem_id)
    return DeleteWorkitemResponse(
        project_id=project_id, workitem_id=workitem_id, deleted=deleted,
    )


# ── /api/xo-projects/{id}/workitems/{id}/claim — derived in_progress ────────
# §5.4, task W7b.


@router.post(
    "/api/xo-projects/{project_id}/workitems/{workitem_id}/claim",
    response_model=WorkitemClaim,
)
def project_workitems_claim(
    project_id: str, workitem_id: str, body: ClaimWorkitemRequest,
) -> WorkitemClaim:
    """Record that a session is working this workitem."""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id)
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
    """Drop the claim — the explicit half of "stopped working on this"."""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id, include_deleted=True)
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
# §7.2, tasks W7 and W8. Three facts shape everything below. (1) GitHub is
# read-only to this system: assignment writes a local annotation into
# ``.xo/workitems.json``, never an issue. (2) Adoption is explicit — the mirror
# holds every issue in the repo, ``.xo/workitems.json`` only what someone chose
# to track. (3) The poller is the mirror's only writer; these routes read it.


#: The cold-mirror fetch's bounds (issuesplan I1/Phase 2).
_COLD_FETCH_PAGES = 1

#: **Eight seconds**, chosen against the two facts that bound it: the poller
#: runs on a 60 s interval, so a caller who times out here waits at most one
#: tick for the same data to arrive anyway; and the ``gh`` client carries its
#: own timeout, so this is the ceiling on the *request*, not on the subprocess.
_COLD_FETCH_TIMEOUT_S = 8.0


#: Every kind in ``connectors/github_issues.ERROR_KINDS``, mapped onto the
#: answer it deserves.
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
    """One ``gh`` failure, as an HTTP answer."""
    status, code = _GITHUB_FAILURES.get(kind or "", (502, "github_unavailable"))
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message or f"GitHub call failed ({kind})."},
    )


def _require_github_repo(scope: scopes.VisualizerScope) -> str:
    """The project's ``owner/name``, or a 400 that says why there is none."""
    repo = scope.github_repo()
    if not repo:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_a_github_project",
                "message": (
                    "This project has no github.com remote in project.json, "
                    "so there are no issues to adopt. Set a GitHub origin, or "
                    "keep the work as local workitems."
                ),
            },
        )
    return repo


def _github_budget_gate() -> None:
    """Refuse an interactive GitHub call the poller has already stood down for."""
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


def _issues_state(
    *,
    repo: Optional[str],
    mirror: Optional[dict],
    error: Optional[GithubMirrorError],
    rows: int,
) -> str:
    """Which of the empty states this answer is (issuesplan I3)."""
    if not repo:
        return "no_remote"
    if rows:
        return "ok"
    if error is not None:
        return "error"
    doc = mirror or {}
    if doc.get("issues_enabled") is False:
        return "issues_disabled"
    if not doc.get("fetched_at"):
        return "never_polled"
    return "empty"


def _issue_number(row: dict) -> int:
    """A row's issue number, or ``0`` when it does not have a usable one."""
    number = row.get("number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return number
    return 0


def _issue_model(
    row: dict, tracked: dict[str, str], live: frozenset[str] = frozenset()
) -> GithubIssue:
    """One mirror row on the wire, tolerating a row that is not quite right."""
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
            # statement about who owes what.
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
        # Same derivation the workitem listing uses, resolved once per request
        # by the caller.
        in_progress=tracked.get(node_id) in live,
    )


@router.get(
    "/api/xo-projects/{project_id}/github/issues",
    response_model=GithubIssuesResponse,
)
async def project_github_issues(
    project_id: str,
    refresh: bool = Query(
        default=False,
        description=(
            "Force a refresh before answering, even when the mirror is warm. "
            "The cold-mirror fetch below happens without it."
        ),
    ),
) -> GithubIssuesResponse:
    """The GitHub issue mirror for this project, and what is untracked."""
    scope = _require_project(project_id)
    # D9's hook, and the whole "being looked at" signal. One small atomic write
    # in the runtime tier, idempotent, and it cannot raise — safe on a request
    # thread.
    github_poller.note_interest(project_id)

    mirror = scope.read_github_mirror()

    # **The cold-mirror fetch (I1).** Before this, the first request for a
    # project's issues could only ever answer "nothing": the mirror is written
    # by the background loop, so data appeared on a *later* request, >=60s on,
    # and >=80s after a restart.
    if refresh or not (mirror or {}).get("fetched_at"):
        outcome = await github_poller.poll_project_now(
            project_id,
            max_pages_override=_COLD_FETCH_PAGES,
            timeout=_COLD_FETCH_TIMEOUT_S,
        )
        if outcome.polled:
            mirror = scope.read_github_mirror()
    issues = _projection.mirror_issues(mirror)
    try:
        records = scope.list_workitems()
    except Exception:
        # The workitems document being unreadable is a real 409 on the
        # workitems surface, and it is answered there.
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
    # Resolved once for the whole page, not per row: it reads the claims file
    # and the presence snapshot, and doing that eighty-five times would turn a
    # browse into a storm.
    live = _in_progress_ids(scope) if tracked else frozenset()
    models = [_issue_model(row, tracked, live) for row in rows]
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
    repo = scope.github_repo()
    return GithubIssuesResponse(
        project_id=project_id,
        # The project's own remote, not the mirror's ``repo``: they differ
        # exactly when the remote has changed under a mirror the poller has not
        # reseeded yet, and the honest answer to "which repo is this project"
        # is the project's.
        repo=repo,
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        error=error,
        issues=models,
        untracked=untracked,
        tracked=sum(1 for row in models if row.adopted),
        state=_issues_state(
            repo=repo, mirror=mirror, error=error, rows=len(models),
        ),
    )


def _mirror_row_for_number(
    scope: scopes.VisualizerScope, number: int
) -> Optional[dict]:
    """The mirror's row for issue ``number``, or ``None``."""
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
    """Track a GitHub issue as a workitem (§7.2, D2)."""
    scope = _require_project(project_id)
    repo = _require_github_repo(scope)

    _github_budget_gate()
    fetched = await github_issue_actions.fetch_issue(repo, issue_number)
    issue = fetched.issue
    if issue is None:
        # The mirror is the fallback, and it is a good one: it is the same
        # reference, written by the same client, minus the labels this call
        # exists to add.
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
    """Stop mirroring the issue. **Keep the workitem** (§7.2)."""
    scope = _require_project(project_id)
    found = _require_workitem(scope, workitem_id)

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


# ── /api/xo-projects/{id}/workitems/{id}/assignee — W8, amended ─────────────
# **This endpoint used to write to GitHub. It does not any more.**
# GitHub is read-only to this system (workitems-plan §13, amendment 33):
# assignment is a local annotation in ``.xo/workitems.json``, for adopted
# and local items alike.


#: The spellings of "me", resolved to this Space's own identity. §4 is why no
#: mapping table is needed: each Space only has to recognise itself.
_SELF_ALIASES: frozenset[str] = frozenset({"me", "@me", "self"})


def _self_identities() -> list[str]:
    """The names this Space answers to."""
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
    """This Space's own GitHub login, or ``None``."""
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
    """Set (or clear) who owes this workitem. **Always a local write.**"""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id)

    wanted = (body.assignee or "").strip() or None
    if wanted is None:
        resolved: Optional[str] = None
    elif wanted.lower() in _SELF_ALIASES:
        # ``resolve_user_id`` is total — the authenticated user, the Coder
        # owner, or the literal ``"local"`` — so this list is never empty and
        # there is no "me is nobody" case to answer.
        resolved = _self_identities()[0]
    else:
        # A leading ``@`` is how people write a login and is not part of it.
        resolved = wanted.lstrip("@").strip() or None

    try:
        # Off the event loop: it is a locked read-modify-write of a file.
        updated = await asyncio.to_thread(
            scope.update_workitem, workitem_id, assignee=resolved,
        )
    except Exception as exc:
        raise _workitem_error(
            exc, failure="workitems.json write failed.",
        ) from exc

    stored = updated.get("assignee")
    stored = stored if isinstance(stored, str) and stored else None
    return WorkitemAssignment(
        project_id=project_id,
        workitem_id=workitem_id,
        # The workitem's own kind, as stored — not "where the write went",
        # which is now always the same place.
        kind="github" if _is_adopted(updated) else "local",
        assignee=stored,
        assignees=[stored] if stored else [],
        # Nothing is outstanding: the file is the record and it is written.
        pending=False,
    )


# ── /api/xo-projects/{id}/peers — the collaborator roster ───────────────────
# ``<project>/.xo/peers.json`` shipped in the project template as a stub with a
# schema already written and **no writer anywhere in the tree**.


#: Store codes that mean *the caller asked for something invalid* — 400 with
#: the store's own message, which names the field and the constraint and is
#: therefore worth forwarding rather than replacing.
_PEER_CALLER_ERRORS: frozenset[str] = frozenset({
    "invalid_user_id",
    "invalid_role",
    "invalid_label",
    "invalid_endpoint",
    "invalid_value",
})


def _peer_error(exc: Exception, *, failure: str) -> HTTPException:
    """Map a ``PeersStoreError`` onto its HTTP answer."""
    return _store_error(
        exc,
        failure=failure,
        document="peers.json",
        not_found=("peer_not_found", "Peer not found."),
        caller_errors=_PEER_CALLER_ERRORS,
        conflicts=frozenset({"peer_exists"}),
    )


def _make_peer_model(d: dict) -> Peer:
    """Shape one stored record for the wire."""
    role = d.get("role")
    if role not in _PEER_ROLES:
        logger.warning(
            "peer %s carries role %r, which is not in the declared vocabulary "
            "%s; rendering it as 'viewer', the least privileged role. Repair "
            "the record.",
            d.get("user_id") or "<no user_id>", role, sorted(_PEER_ROLES),
        )
        role = "viewer"
    return Peer(
        user_id=str(d.get("user_id", "")),
        role=role,
        added_at=d.get("added_at"),
        endpoint=d.get("endpoint"),
        label=d.get("label"),
    )


@router.get(
    "/api/xo-projects/{project_id}/peers",
    response_model=PeersResponse,
)
def project_peers(
    project_id: str,
    role: Optional[str] = Query(
        default=None, description="Filter to `owner`, `collaborator` or `viewer`."
    ),
) -> PeersResponse:
    """Everyone this project is shared with, oldest first."""
    scope = _require_project(project_id)
    try:
        updated_at, peers = scope.read_peer_roster(role=role)
    except Exception as exc:
        raise _peer_error(exc, failure="peers.json is not readable.") from exc
    return PeersResponse(
        project_id=project_id,
        updated_at=updated_at,
        peers=[_make_peer_model(row) for row in peers],
    )


@router.post(
    "/api/xo-projects/{project_id}/peers",
    response_model=Peer,
    status_code=201,
)
def project_peers_create(project_id: str, body: CreatePeerRequest) -> Peer:
    """Add a collaborator to the roster."""
    scope = _require_project(project_id)
    try:
        new = scope.create_peer(
            user_id=body.user_id,
            role=body.role,
            label=body.label,
            endpoint=body.endpoint,
        )
    except Exception as exc:
        raise _peer_error(exc, failure="peers.json write failed.") from exc
    return _make_peer_model(new)


@router.get(
    "/api/xo-projects/{project_id}/peers/{user_id}",
    response_model=Peer,
)
def project_peers_get(project_id: str, user_id: str) -> Peer:
    """Fetch one peer by ``user_id``."""
    scope = _require_project(project_id)
    try:
        found = scope.get_peer(user_id)
    except Exception as exc:
        raise _peer_error(exc, failure="peers.json is not readable.") from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "peer_not_found", "message": "Peer not found."},
        )
    return _make_peer_model(found)


@router.patch(
    "/api/xo-projects/{project_id}/peers/{user_id}",
    response_model=Peer,
)
def project_peers_update(
    project_id: str, user_id: str, body: UpdatePeerRequest,
) -> Peer:
    """Change a peer's ``role``, ``label`` or ``endpoint``."""
    scope = _require_project(project_id)
    try:
        updated = scope.update_peer(user_id, **body.store_kwargs())
    except Exception as exc:
        raise _peer_error(exc, failure="peers.json write failed.") from exc
    return _make_peer_model(updated)


@router.delete(
    "/api/xo-projects/{project_id}/peers/{user_id}",
    response_model=DeletePeerResponse,
)
def project_peers_delete(project_id: str, user_id: str) -> DeletePeerResponse:
    """Remove a collaborator. **A hard delete — there is no tombstone.**"""
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_peer(user_id)
    except Exception as exc:
        raise _peer_error(exc, failure="peers.json write failed.") from exc
    return DeletePeerResponse(
        project_id=project_id, user_id=user_id, deleted=deleted,
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
    response_model_exclude_unset=True,
)
def project_timeline(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Newest-first event stream for one project."""
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
            logger.warning("timeline: dropping an event that failed validation")
            continue

    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=project_id, events=out, next_cursor=next_cursor)
