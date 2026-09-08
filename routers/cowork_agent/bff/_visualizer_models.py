"""Pydantic response models for the visualizer BFF endpoints.

Two naming conventions on purpose:

* **Usage** models (``analytics``, ``summary/card``, ``summary``,
  ``sessions``, ``sessions/{id}``) keep the existing
  ``/openclaw/usage/*`` wire shape — camelCase field names, no
  aliasing — so the frontend's existing usage-tab client works
  unchanged. See ``routers/openclaw_usage.py``.

* **Visualizer** models (``todos``, ``activity``, ``timeline``) use
  snake_case field names that match their JSON Schemas under
  ``services/cowork_agent/visualizer/schema/``.

Every model declares ``extra="forbid"``. An unexpected key surfacing
from disk fails the route closed with 500 ``scope_unavailable``
rather than leaking — the wire allowlist enforcement from
docs/watcher-design.md §7.3 / §7.4.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from services.cowork_agent.visualizer.todo_status import TodoStatus


# ── Base config: extra=forbid everywhere ──────────────────────────────────────


class _ForbidExtra(BaseModel):
    """Base for every response model — extra keys raise."""

    model_config = ConfigDict(extra="forbid")


# ── /usage/analytics ──────────────────────────────────────────────────────────


class AnalyticsStats(_ForbidExtra):
    totalCost: float
    totalTokens: int
    totalMessages: int
    avgLatencyMs: int


class CostAndTokensEntry(_ForbidExtra):
    date: str
    tokens: int
    cost: float


class MessagesEntry(_ForbidExtra):
    date: str
    total: int
    user: int
    assistant: int
    toolCalls: int


class PerformanceEntry(_ForbidExtra):
    date: str
    avgMs: int
    p95Ms: int
    minMs: int
    maxMs: int


class ToolUsageEntry(_ForbidExtra):
    name: str
    count: int


class ToolUsage(_ForbidExtra):
    totalCalls: int
    uniqueTools: int
    tools: list[ToolUsageEntry]


class ModelUsageEntry(_ForbidExtra):
    model: str
    provider: str
    calls: int
    tokens: int
    cost: float


class UsageAnalyticsResponse(_ForbidExtra):
    stats: AnalyticsStats
    costAndTokens: list[CostAndTokensEntry]
    messages: list[MessagesEntry]
    performance: list[PerformanceEntry]
    toolUsage: ToolUsage
    modelUsage: list[ModelUsageEntry]


# ── /usage/summary/card ───────────────────────────────────────────────────────


class DailyCostEntry(_ForbidExtra):
    date: str
    cost: float
    tokens: int
    messages: int


class UsageSummaryCardResponse(_ForbidExtra):
    days: int
    totalCost: float
    totalMessages: int
    totalTokens: int
    dailyCost: list[DailyCostEntry]


# ── /usage/summary (the full SessionCostSummary) ──────────────────────────────


class TokenTotals(_ForbidExtra):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    totalTokens: int
    totalCost: float
    inputCost: float
    outputCost: float
    cacheReadCost: float
    cacheWriteCost: float
    missingCostEntries: int


class DailyBreakdownEntry(_ForbidExtra):
    date: str
    tokens: int
    cost: float


class DailyLatencyEntry(_ForbidExtra):
    date: str
    count: int
    avgMs: int
    p95Ms: int
    minMs: int
    maxMs: int


class DailyModelUsageEntry(_ForbidExtra):
    date: str
    provider: str
    model: str
    tokens: int
    cost: float
    count: int


class MessageCounts(_ForbidExtra):
    total: int
    user: int
    assistant: int
    toolCalls: int
    toolResults: int
    errors: int


class ModelUsageWithTotals(_ForbidExtra):
    provider: str
    model: str
    count: int
    totals: TokenTotals


class SessionCostSummary(_ForbidExtra):
    """One session's full cost+activity summary OR the aggregate
    across many. Mirrors ``routers/openclaw_usage.py:372-427``."""

    sessionId: str
    sessionFile: str
    firstActivity: Optional[int] = None
    lastActivity: Optional[int] = None
    durationMs: Optional[int] = None
    activityDates: list[str] = []

    input: int = 0
    output: int = 0
    cacheRead: int = 0
    cacheWrite: int = 0
    totalTokens: int = 0
    totalCost: float = 0.0
    inputCost: float = 0.0
    outputCost: float = 0.0
    cacheReadCost: float = 0.0
    cacheWriteCost: float = 0.0
    missingCostEntries: int = 0

    dailyBreakdown: list[DailyBreakdownEntry] = []
    dailyLatency: list[DailyLatencyEntry] = []
    dailyModelUsage: list[DailyModelUsageEntry] = []

    messageCounts: MessageCounts = MessageCounts(
        total=0, user=0, assistant=0, toolCalls=0, toolResults=0, errors=0
    )
    toolUsage: ToolUsage = ToolUsage(totalCalls=0, uniqueTools=0, tools=[])
    modelUsage: list[ModelUsageWithTotals] = []

    # Only populated on the workspace/aggregate ``/usage/summary`` —
    # absent on a single-session ``/usage/sessions/{id}`` response.
    sessionCount: Optional[int] = None
    sessions: Optional[list["SessionCostSummary"]] = None


# ── /usage/sessions ───────────────────────────────────────────────────────────


class SessionListItem(_ForbidExtra):
    sessionId: str
    sessionFile: str
    messageCount: int
    totalTokens: int
    totalCost: float
    firstActivity: Optional[int] = None
    lastActivity: Optional[int] = None
    # Workspace scope adds projectId; project scope omits.
    projectId: Optional[str] = None


class SessionListResponse(_ForbidExtra):
    # Present on project scope (filterable by ``agent_id``); omitted on
    # workspace scope where multiple agents may appear in one list.
    agentId: Optional[str] = None
    count: int
    sessions: list[SessionListItem]


# ── /todos ────────────────────────────────────────────────────────────────────


class Todo(_ForbidExtra):
    id: str
    content: str
    # Declared, not just documented: the wire used to accept any string
    # here, so the one enumeration the OpenAPI consumers see was absent.
    # ``TodoStatus`` is the shared vocabulary, so this is the same set the
    # store validates against and the schemas declare. Strictness here is
    # the wire allowlist this module already applies to keys (see the
    # module docstring): a status outside the set is a writer's mistake
    # and fails closed rather than leaking to the UI.
    status: TodoStatus
    # Optional runtime-specific extensions allowed by the schema's
    # ``additionalProperties: true`` on the todo definition.
    description: Optional[str] = None
    active_form: Optional[str] = None
    # Schema 2 (syncplan §5.5). These are ``Optional`` rather than
    # required because documents written before the store stamped them
    # are still legal on disk and must render, not 500 — this model
    # forbids extra keys, so a field the store writes and this class
    # omits is a guaranteed 500 on the next read.
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Soft delete: the tombstone is deliberately NOT a status value.
    # ``status`` records what we decided about the work; ``deleted_at``
    # records that the item should not have existed. Collapsing them
    # would make "everything ever closed" unanswerable. List reads hide
    # tombstones unless ``?include_deleted=true``.
    deleted_at: Optional[str] = None
    deleted_by: Optional[str] = None


class SessionTodos(_ForbidExtra):
    runtime: str
    source_file: Optional[str] = None
    session_started_at: Optional[str] = None
    todos: list[Todo]


class TodosResponse(_ForbidExtra):
    project_id: str
    updated_at: Optional[str] = None
    sessions: dict[str, SessionTodos]


# Request bodies for the agent-facing todos CRUD endpoints.


class CreateTodoRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/todos — create a new todo.

    ``runtime`` is required so the watcher's read path can keep them
    discriminable from Claude-derived rows. ``session_id`` defaults
    to the ``"_project"`` pseudo-session so callers that don't have
    a session concept don't have to invent one. Both fields are
    sanitised (alnum + a small set of separators) before any FS
    touch.
    """

    runtime: str
    content: str
    description: Optional[str] = None
    active_form: Optional[str] = None
    session_id: Optional[str] = None
    # Deliberately a plain string, NOT ``TodoStatus``: the store validates
    # it and the routes map that to the documented ``400 invalid_status``.
    # Typing it as a Literal would turn the same request into a 422 with a
    # different body — a wire change the docs (todos-http-api.md:44) and
    # every existing client would have to follow.
    status: Optional[str] = None  # defaults to "pending" server-side


class UpdateTodoRequest(_ForbidExtra):
    """PATCH /api/xo-projects/{id}/todos/{todo_id}.

    All fields optional — only those provided are touched. ``status``
    is the common case; ``content`` / ``description`` / ``active_form``
    let runtimes refine the todo after creation.
    """

    status: Optional[str] = None
    content: Optional[str] = None
    description: Optional[str] = None
    active_form: Optional[str] = None


class DeleteTodoResponse(_ForbidExtra):
    """DELETE /api/xo-projects/{id}/todos/{todo_id} — idempotent
    (returns ``deleted: false`` when the todo wasn't present)."""

    project_id: str
    todo_id: str
    deleted: bool


# ── /activity ─────────────────────────────────────────────────────────────────


class OpenSession(_ForbidExtra):
    session_id: str
    runtime: Optional[str] = None
    agent: str
    user_id: str
    opened_at: str
    last_activity_at: str
    host: Optional[str] = None
    # Workspace scope tags each row with its project; project scope omits.
    project_id: Optional[str] = None


class ActivityResponse(_ForbidExtra):
    # Project scope sets project_id; workspace scope omits.
    project_id: Optional[str] = None
    updated_at: Optional[str] = None
    open_sessions: list[OpenSession]


# ── /timeline ─────────────────────────────────────────────────────────────────


class TimelineEvent(_ForbidExtra):
    """One line from ``.xo/timeline.jsonl``. Permissive shape because
    the schema's ``oneOf`` lets each event type carry its own extras —
    a strict union here would require 12 subclasses. The schema-side
    ``oneOf`` is the canonical validator; the route-side check is the
    backstop (path-bearing events get a relative-path assertion at
    serialise time)."""

    model_config = ConfigDict(extra="allow")  # see docstring

    ts: str
    type: str
    session_id: Optional[str] = None
    runtime: Optional[str] = None
    # workspace scope tags events with project_id; project scope omits.
    project_id: Optional[str] = None


class TimelineResponse(_ForbidExtra):
    project_id: Optional[str] = None
    events: list[TimelineEvent]
    next_cursor: Optional[str] = None
