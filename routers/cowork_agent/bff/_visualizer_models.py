"""Pydantic response models for the visualizer BFF endpoints."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from services.cowork_agent.visualizer.peers_store import (
    VALID_ROLES as _PEER_ROLES,
)
from services.cowork_agent.visualizer.todo_status import TodoStatus
from services.cowork_agent.visualizer.workitems_store import (
    VALID_STATE_REASONS as _WORKITEM_STATE_REASONS,
    VALID_STATUSES as _WORKITEM_STATUSES,
)


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
    # Declared, not just documented: the wire used to accept any string here,
    # so the one enumeration the OpenAPI consumers see was absent.
    status: TodoStatus
    # Optional runtime-specific extensions allowed by the schema's
    # ``additionalProperties: true`` on the todo definition.
    description: Optional[str] = None
    active_form: Optional[str] = None
    # Schema 2 (syncplan §5.5).
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Soft delete: the tombstone is deliberately NOT a status value.
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
    # Deliberately a plain string, NOT ``TodoStatus``: the store validates it
    # and the routes map that to the documented ``400 invalid_status``.
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


# ── /peers ────────────────────────────────────────────────────────────────────
# The wire shape of ``<project>/.xo/peers.json`` — the collaborator roster —
# and the request bodies for its CRUD surface.


#: The role vocabulary, *derived* from the store's own frozenset rather than
#: re-typed here, for the reason ``WorkitemStatus`` is: the store is already
#: the one definition, so the enum the OpenAPI schema publishes cannot drift
#: from the one the store validates against.
PeerRole = Literal[*sorted(_PEER_ROLES)]


class Peer(_ForbidExtra):
    """One collaborator, as served."""

    user_id: str
    # ``Literal`` so the OpenAPI schema carries the enum, and coerced rather
    # than trusted on the read path (``_coerce_peer_role``): a synced ``.xo/``
    # is restored wholesale from somewhere else, so "the store wrote it" is not
    # the same claim as "this process wrote it", and one odd row must not take
    # the whole roster down with it.
    role: PeerRole
    added_at: Optional[str] = None
    endpoint: Optional[str] = None
    label: Optional[str] = None


class PeersResponse(_ForbidExtra):
    """``GET /peers`` — the roster, oldest first."""

    project_id: str
    updated_at: Optional[str] = None
    peers: list[Peer]


class CreatePeerRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/peers — add a collaborator."""

    user_id: str
    role: str
    label: Optional[str] = None
    endpoint: Optional[str] = None


#: The nullable pair — the fields for which "absent" and "null" are different
#: requests. ``role`` is not nullable, so absent and null mean the same thing
#: there.
_PEER_NULLABLE_FIELDS: tuple[str, ...] = ("label", "endpoint")


class UpdatePeerRequest(_ForbidExtra):
    """PATCH /api/xo-projects/{id}/peers/{user_id}."""

    role: Optional[str] = None
    label: Optional[str] = None
    endpoint: Optional[str] = None

    def store_kwargs(self) -> dict:
        """The keyword arguments to hand ``peers_store.update_peer``."""
        supplied = self.model_fields_set
        kwargs: dict = {"role": self.role}
        for field in _PEER_NULLABLE_FIELDS:
            if field in supplied:
                kwargs[field] = getattr(self, field)
        return kwargs


class DeletePeerResponse(_ForbidExtra):
    """
    DELETE /api/xo-projects/{id}/peers/{user_id} — idempotent (returns
    ``deleted: false`` when the peer was not on the roster).
    """

    project_id: str
    user_id: str
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


# ── /workitems ────────────────────────────────────────────────────────────────
# The wire shape of ``<project>/.xo/workitems.json`` (workitems-plan §5.1) and
# the request bodies for the project-tier CRUD surface (§7.1).


#: The wire vocabularies, *derived* from the store's own frozensets rather than
#: re-typed here.
WorkitemStatus = Literal[*sorted(_WORKITEM_STATUSES)]
WorkitemStateReason = Literal[*sorted(_WORKITEM_STATE_REASONS)]


class GithubRef(_ForbidExtra):
    """``source.github`` — the issue this workitem mirrors."""

    repo: str
    number: int
    node_id: str
    url: str


class WorkitemSource(_ForbidExtra):
    """
    ``local`` or ``github``. ``github`` is populated only for an adopted item,
    and only when the stored reference is well formed — see
    ``_make_workitem_model``.
    """

    kind: Literal["local", "github"]
    github: Optional[GithubRef] = None


class WorkitemLinks(_ForbidExtra):
    """
    The join to the fine-grained records (§9, D3). An agent working a workitem
    creates todos under it; ``todo_ids`` is the join to ``todos.json``.
    """

    todo_ids: list[str] = []
    session_ids: list[str] = []


class Workitem(_ForbidExtra):
    """One workitem, as served."""

    id: str
    title: str
    body: Optional[str] = None
    labels: list[str] = []
    # ``Literal`` so the OpenAPI schema carries the enum — the same reason
    # ``Todo.status`` is one.
    status: Optional[WorkitemStatus] = None
    state_reason: Optional[WorkitemStateReason] = None
    source: WorkitemSource
    # Where the workitem came from, in this API's own words. ``"github"`` iff
    # ``source.kind == "github"``; ``"space"`` otherwise — i.e. ``origin:
    # "space"`` IS ``source.kind: "local"`` on disk.
    origin: Literal["github", "space"] = "space"
    # Who this Space says owes the work — from ``.xo/workitems.json``, for both
    # kinds. ``null`` when nobody does.
    assignee: Optional[str] = None
    # "Is this assigned to anyone or not", so a caller does not have to null-
    # check an identity to ask a yes/no question.
    assigned: bool = False
    # ``assignee`` as a list, at most one long. Kept because it has always been
    # on the wire; it is the same fact, not a second one.
    assignees: list[str] = []
    # Who **GitHub** has on the issue, flattened to logins.
    github_assignees: list[str] = []
    # "This item is adopted and its issue is not in the mirror" — deleted,
    # transferred, or the poller has never run (§5.3).
    stale: bool = False
    # Derived, never stored (§5.4, D7).
    in_progress: bool = False
    links: WorkitemLinks
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    # Soft delete, exactly as for todos: the tombstone is not a status value.
    deleted_at: Optional[str] = None
    deleted_by: Optional[str] = None


class WorkitemsResponse(_ForbidExtra):
    """``GET /workitems`` — a **list**, never the raw map."""

    project_id: str
    workitems: list[Workitem]


class CreateWorkitemRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/workitems — create a **local** workitem."""

    runtime: str
    title: str
    body: Optional[str] = None
    labels: Optional[list[str]] = None
    status: Optional[str] = None
    state_reason: Optional[str] = None
    assignee: Optional[str] = None
    todo_ids: Optional[list[str]] = None
    session_ids: Optional[list[str]] = None


#: The nullable trio — the fields for which "absent" and "null" are different
#: requests. Everything else on the PATCH body is two-state.
_WORKITEM_NULLABLE_FIELDS: tuple[str, ...] = ("body", "state_reason", "assignee")


class UpdateWorkitemRequest(_ForbidExtra):
    """PATCH /api/xo-projects/{id}/workitems/{workitem_id}."""

    title: Optional[str] = None
    labels: Optional[list[str]] = None
    status: Optional[str] = None
    todo_ids: Optional[list[str]] = None
    session_ids: Optional[list[str]] = None
    body: Optional[str] = None
    state_reason: Optional[str] = None
    assignee: Optional[str] = None

    def store_kwargs(self) -> dict:
        """The keyword arguments to hand ``workitems_store.update_workitem``."""
        supplied = self.model_fields_set
        kwargs: dict = {
            "title": self.title,
            "labels": self.labels,
            "status": self.status,
            "todo_ids": self.todo_ids,
            "session_ids": self.session_ids,
        }
        for field in _WORKITEM_NULLABLE_FIELDS:
            if field in supplied:
                kwargs[field] = getattr(self, field)
        return kwargs


class DeleteWorkitemResponse(_ForbidExtra):
    """
    DELETE /api/xo-projects/{id}/workitems/{workitem_id} — idempotent (returns
    ``deleted: false`` when the workitem was already absent or already
    tombstoned).
    """

    project_id: str
    workitem_id: str
    deleted: bool


# ── /workitems/{id}/claim ───────────────────────────────────────────────────
# The API for "an agent is working this" is a **claim, not a status write**
# (§5.4).


class ClaimWorkitemRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/workitems/{workitem_id}/claim."""

    session_id: str
    runtime: str


class WorkitemClaim(_ForbidExtra):
    """The claim as recorded, plus what it currently means."""

    project_id: str
    workitem_id: str
    session_id: str
    runtime: str
    started_at: str
    in_progress: bool


class ReleaseWorkitemClaimResponse(_ForbidExtra):
    """
    DELETE /api/xo-projects/{id}/workitems/{workitem_id}/claim — idempotent
    (``released: false`` when there was no claim to drop), matching the
    tombstoning DELETE above.
    """

    project_id: str
    workitem_id: str
    released: bool


# ── /github/issues, /adoption, /assignee ────────────────────────────────────
# The GitHub-facing half of the surface (workitems-plan §7.2, W7 and W8).


class GithubIssueAssignee(_ForbidExtra):
    """
    One assignee as the mirror holds them. The avatar is optional because
    GitHub does not always return one, and a missing picture must not cost the
    login it belongs to.
    """

    login: str
    avatar_url: Optional[str] = None


class GithubIssue(_ForbidExtra):
    """One row of the mirror, plus whether it is already tracked."""

    node_id: str
    number: int
    title: str
    state: Optional[WorkitemStatus] = None
    state_reason: Optional[WorkitemStateReason] = None
    assignees: list[GithubIssueAssignee] = []
    labels: Optional[list[str]] = None
    url: str
    updated_at: Optional[str] = None
    adopted: bool = False
    workitem_id: Optional[str] = None
    #: Whether an agent is working this issue **right now**, derived from a
    #: live claim on its workitem (never stored).
    in_progress: bool = False


class GithubMirrorError(_ForbidExtra):
    """The last poll failure, structured rather than a bare string."""

    kind: str
    message: str
    at: Optional[str] = None


class GithubIssuesResponse(_ForbidExtra):
    """``GET /github/issues`` — the mirror, and how much of it is untracked."""

    project_id: str
    repo: Optional[str] = None
    fetched_at: Optional[str] = None
    error: Optional[GithubMirrorError] = None
    issues: list[GithubIssue] = []
    untracked: int = 0
    tracked: int = 0
    # **Which empty this is** (issuesplan I3).
    state: Literal[
        "ok", "empty", "never_polled", "issues_disabled", "no_remote", "error"
    ] = "never_polled"


class AdoptIssueRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/github/issues/{number}/adopt."""

    runtime: str
    workitem_id: Optional[str] = None


class AssignWorkitemRequest(_ForbidExtra):
    """PUT /api/xo-projects/{id}/workitems/{workitem_id}/assignee."""

    assignee: Optional[str]


class WorkitemAssignment(_ForbidExtra):
    """The result of an assignment."""

    project_id: str
    workitem_id: str
    kind: Literal["local", "github"]
    assignee: Optional[str] = None
    assignees: list[str] = []
    pending: bool = False
