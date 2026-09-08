"""Pydantic response models for the visualizer BFF endpoints.

Two naming conventions on purpose:

* **Usage** models (``analytics``, ``summary/card``, ``summary``,
  ``sessions``, ``sessions/{id}``) keep the existing
  ``/openclaw/usage/*`` wire shape — camelCase field names, no
  aliasing — so the frontend's existing usage-tab client works
  unchanged. See ``routers/openclaw_usage.py``.

* **Visualizer** models (``todos``, ``peers``, ``activity``,
  ``timeline``) use snake_case field names that match their JSON Schemas
  under ``services/cowork_agent/visualizer/schema/``.

Every model declares ``extra="forbid"``. An unexpected key surfacing
from disk fails the route closed with 500 ``scope_unavailable``
rather than leaking — the wire allowlist enforcement from
docs/watcher-design.md §7.3 / §7.4.
"""

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


# ── /peers ────────────────────────────────────────────────────────────────────
#
# The wire shape of ``<project>/.xo/peers.json`` — the collaborator
# roster — and the request bodies for its CRUD surface. It is the todos
# and workitems dialect again on purpose: same ``{"code", "message"}``
# 400 bodies, same idempotent DELETE, same "plain ``str`` on the way in,
# ``Literal`` on the way out" split, so an agent that can drive either of
# those can drive this one.
#
# Two things are deliberately absent and are worth naming, because their
# absence is a decision rather than an oversight.
#
# There is **no ``runtime`` field** on any body here. ``peers.schema.json``
# is ``additionalProperties: false`` and declares nothing to attribute a
# roster edit to, so a ``runtime`` this layer accepted would be a value
# the store had nowhere to put — accepted, validated, and then dropped.
#
# There are **no tombstone fields** — no ``deleted_at``, no
# ``deleted_by``. Removing a peer really removes them. ``peers.json`` is
# in the synced tier, so a tombstone would carry "this person used to
# have access" to every Space the project ever reaches; a roster answers
# who is on the project now, and the record of who was is not this
# document's job.


#: The role vocabulary, *derived* from the store's own frozenset rather
#: than re-typed here, for the reason ``WorkitemStatus`` is: the store is
#: already the one definition, so the enum the OpenAPI schema publishes
#: cannot drift from the one the store validates against. ``sorted`` only
#: fixes an order for a set that has none.
PeerRole = Literal[*sorted(_PEER_ROLES)]


class Peer(_ForbidExtra):
    """One collaborator, as served.

    ``user_id`` is the identity — the roster is an array with no separate
    id, so the ``user_id`` is the path segment, the match key and the
    thing that makes the roster a set. It is also, deliberately,
    validated against the same charset as a workitem ``assignee``: a
    ``user_id`` that is a legal peer is always a legal assignee, which is
    the point of keeping a roster at all.

    ``added_at`` is server-set at create and is never editable. It
    records when *this Space* learned of the peer, which is an
    observation rather than a claim the caller gets to make.

    ``endpoint`` and ``label`` are ``Optional`` because the schema types
    them nullable, and ``null`` means "not set" for both — unlike the
    workitem fields where ``null`` can mean "unknown, ask GitHub". There
    is nothing else to ask.
    """

    user_id: str
    # ``Literal`` so the OpenAPI schema carries the enum, and coerced
    # rather than trusted on the read path (``_coerce_peer_role``): a
    # synced ``.xo/`` is restored wholesale from somewhere else, so "the
    # store wrote it" is not the same claim as "this process wrote it",
    # and one odd row must not take the whole roster down with it.
    role: PeerRole
    added_at: Optional[str] = None
    endpoint: Optional[str] = None
    label: Optional[str] = None


class PeersResponse(_ForbidExtra):
    """``GET /peers`` — the roster, oldest first.

    ``updated_at`` is the document's own stamp, served beside the list
    because "when did this roster last change" is a question about the
    document rather than about any one peer. It is ``null`` for a project
    whose ``peers.json`` has never been written.

    An empty ``peers`` list is a real answer and the common one: the
    schema says so in as many words — "Empty list = solo project".
    """

    project_id: str
    updated_at: Optional[str] = None
    peers: list[Peer]


class CreatePeerRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/peers — add a collaborator.

    ``added_at`` is not a field: the server sets it. Neither is
    ``runtime`` — see the section comment above.

    ``role`` is a plain ``str``, not :data:`PeerRole`, for the same
    reason ``CreateTodoRequest.status`` is: the store validates it and
    the route maps that to the documented ``400 invalid_role``. Typing it
    as a ``Literal`` would turn the same request into a 422 with a
    different body.
    """

    user_id: str
    role: str
    label: Optional[str] = None
    endpoint: Optional[str] = None


#: The nullable pair — the fields for which "absent" and "null" are
#: different requests. ``role`` is not nullable, so absent and null mean
#: the same thing there. Module level rather than a class attribute so it
#: is a plain constant and not something Pydantic has to be told to
#: ignore.
_PEER_NULLABLE_FIELDS: tuple[str, ...] = ("label", "endpoint")


class UpdatePeerRequest(_ForbidExtra):
    """PATCH /api/xo-projects/{id}/peers/{user_id}.

    **Three-way for two of its fields**, exactly like
    ``UpdateWorkitemRequest``: ``label`` and ``endpoint`` are nullable,
    so this model has to express three distinct requests where a plain
    ``Optional`` expresses two:

    ========================  =========================  ==============
    request                   meaning                    store kwarg
    ========================  =========================  ==============
    key absent                leave it alone             *not passed*
    ``{"label": null}``       clear it                   ``None``
    ``{"label": "Ada"}``      set it                     ``"Ada"``
    ========================  =========================  ==============

    Collapsing the first two would make removing a display name or a sync
    endpoint impossible over HTTP.

    **``user_id`` is not here, and that is the contract.** It is the
    identity, so changing it is a delete plus a create — a PATCH that
    re-keyed a record would silently hand whatever the old id meant to a
    different person. ``added_at`` is absent for a milder reason: it is
    an observation this Space made, not a value a caller revises.
    """

    role: Optional[str] = None
    label: Optional[str] = None
    endpoint: Optional[str] = None

    def store_kwargs(self) -> dict:
        """The keyword arguments to hand ``peers_store.update_peer``.

        A nullable field the caller did not mention is **omitted**, not
        passed as ``None``: the store's own parameter default is its
        ``UNSET`` sentinel, so omission is how "not supplied" is spelled
        and the sentinel never has to cross this layer.
        """
        supplied = self.model_fields_set
        kwargs: dict = {"role": self.role}
        for field in _PEER_NULLABLE_FIELDS:
            if field in supplied:
                kwargs[field] = getattr(self, field)
        return kwargs


class DeletePeerResponse(_ForbidExtra):
    """DELETE /api/xo-projects/{id}/peers/{user_id} — idempotent
    (returns ``deleted: false`` when the peer was not on the roster).

    ``deleted: true`` means the record is **gone**, not tombstoned. There
    is no ``?include_deleted=`` on the list route to read it back,
    because there is nothing to read back.
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
#
# The wire shape of ``<project>/.xo/workitems.json`` (workitems-plan §5.1)
# and the request bodies for the project-tier CRUD surface (§7.1). It
# mirrors the todos models above on purpose — same ``runtime`` vocabulary,
# same tombstone fields, same "plain ``str`` on the way in, ``Literal`` on
# the way out" split — so an agent that can drive todos can drive
# workitems without learning a second dialect.


#: The wire vocabularies, *derived* from the store's own frozensets
#: rather than re-typed here. ``todo_status.py`` exists because the todo
#: set was spelled out in fourteen places; for workitems the store is
#: already the one definition, so the enum the OpenAPI schema publishes
#: is computed from it and cannot drift. ``sorted`` only fixes an order
#: for a set that has none.
WorkitemStatus = Literal[*sorted(_WORKITEM_STATUSES)]
WorkitemStateReason = Literal[*sorted(_WORKITEM_STATE_REASONS)]


class GithubRef(_ForbidExtra):
    """``source.github`` — the issue this workitem mirrors.

    ``node_id`` rides alongside ``number`` because it survives a repo
    rename or transfer, which ``repo``/``number`` do not; it is the
    match key against the mirror (§5.1).
    """

    repo: str
    number: int
    node_id: str
    url: str


class WorkitemSource(_ForbidExtra):
    """``local`` or ``github``. ``github`` is populated only for an
    adopted item, and only when the stored reference is well formed —
    see ``_make_workitem_model``."""

    kind: Literal["local", "github"]
    github: Optional[GithubRef] = None


class WorkitemLinks(_ForbidExtra):
    """The join to the fine-grained records (§9, D3). An agent working a
    workitem creates todos under it; ``todo_ids`` is the join to
    ``todos.json``. Ids are not resolved — a todo may be tombstoned while
    the link survives as history."""

    todo_ids: list[str] = []
    session_ids: list[str] = []


class Workitem(_ForbidExtra):
    """One workitem, as served.

    Three fields are ``Optional`` for a reason that is not "they might be
    missing": for an adopted item (``source.kind == "github"``) the store
    does not hold ``status``, ``state_reason`` or ``body`` at all, because
    GitHub owns them and a stale ``closed`` is a false statement about
    whether the work is done (§5.3). They therefore serialise as ``null`` —
    *unknown*, not *unset*; the projection fills them in from the mirror
    when the poller has seen the issue, and leaves them ``null`` with
    ``stale: true`` when it has not.

    ``assignee`` is **not** one of them any more (§13, amendment 33 — the
    reversal of D1). Nothing in this system writes to GitHub, so assignment
    is a local annotation stored in ``.xo/workitems.json`` for every
    workitem, adopted or not, and it is served from the file for both kinds.
    What GitHub thinks is served separately as ``github_assignees``.

    **Two vocabularies for one distinction, on purpose.** ``source.kind`` is
    what is *on disk* — ``"local"`` | ``"github"`` — and every test, the
    schema and the store speak it. ``origin`` is the same distinction spelled
    for a reader of this API: ``"github"`` when the workitem came from a
    GitHub issue, ``"space"`` when it did not. ``origin: "space"``
    corresponds **exactly** to ``source.kind: "local"``; the stored value was
    not renamed because it is a synced on-disk format, and renaming it would
    have rewritten every document and every test to change a word.
    """

    id: str
    title: str
    body: Optional[str] = None
    labels: list[str] = []
    # ``Literal`` so the OpenAPI schema carries the enum — the same
    # reason ``Todo.status`` is one. The read path coerces rather than
    # trusts (``_coerce_workitem_choice``): a value the store would never
    # write can still arrive on disk from a restored snapshot, and one
    # such row must not take the whole list down with it.
    status: Optional[WorkitemStatus] = None
    state_reason: Optional[WorkitemStateReason] = None
    source: WorkitemSource
    # Where the workitem came from, in this API's own words. ``"github"``
    # iff ``source.kind == "github"``; ``"space"`` otherwise — i.e.
    # ``origin: "space"`` IS ``source.kind: "local"`` on disk. Two words for
    # one thing, and the class docstring says why the stored one did not
    # change.
    origin: Literal["github", "space"] = "space"
    # Who this Space says owes the work — from ``.xo/workitems.json``, for
    # both kinds. ``null`` when nobody does.
    assignee: Optional[str] = None
    # "Is this assigned to anyone or not", so a caller does not have to
    # null-check an identity to ask a yes/no question. It is exactly
    # ``assignee is not None`` and nothing else: it says nothing about
    # ``github_assignees``, because those are not an assignment this system
    # made.
    assigned: bool = False
    # ``assignee`` as a list, at most one long. Kept because it has always
    # been on the wire; it is the same fact, not a second one. It used to
    # carry GitHub's assignees for an adopted item — those moved to
    # ``github_assignees`` when assignment stopped being GitHub's (§13,
    # amendment 33).
    assignees: list[str] = []
    # Who **GitHub** has on the issue, flattened to logins. Information,
    # never assignment: nothing in this system writes it, the poller reads
    # it once a minute, and it is empty for a workitem that is not adopted
    # and for one whose issue the mirror cannot speak for. A peer assigning
    # themselves on GitHub shows up here and nowhere else.
    github_assignees: list[str] = []
    # "This item is adopted and its issue is not in the mirror" — deleted,
    # transferred, or the poller has never run (§5.3). The item still
    # renders, from the title and labels snapshotted at adoption, with
    # state and assignee ``null`` for *unknown*. It is never a 404: the
    # feature has to stay useful with GitHub switched off. It is not a
    # freshness measure — how old the mirror is belongs to the mirror as a
    # whole and is served by ``GET /github/issues``.
    stale: bool = False
    # Derived, never stored (§5.4, D7). ``status`` is GitHub's two
    # values; "an agent is working this right now" is a live claim whose
    # session is still present in the watcher's presence snapshot, and
    # it is computed on every read. It is *not* a third status value:
    # an item can be open and in progress, or open and not. Default
    # ``False`` so a caller that could not derive it (no runtime home,
    # no snapshot yet) reports the honest "nothing observed" rather
    # than omitting the key.
    in_progress: bool = False
    links: WorkitemLinks
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    # Soft delete, exactly as for todos: the tombstone is not a status
    # value. ``status`` records what we decided about the work;
    # ``deleted_at`` records that the workitem should not have existed.
    deleted_at: Optional[str] = None
    deleted_by: Optional[str] = None


class WorkitemsResponse(_ForbidExtra):
    """``GET /workitems`` — a **list**, never the raw map.

    §7.3's workspace rollup unions these across projects and a list has
    no union key, so the O-C collision class cannot occur there at all.
    Serving the project tier in the same shape means the rollup is a
    concatenation rather than a reshaping.
    """

    project_id: str
    workitems: list[Workitem]


class CreateWorkitemRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/workitems — create a **local** workitem.

    There is deliberately no ``source`` field. Adoption is an explicit
    human act with its own endpoint (§7.2 ``POST /github/issues/{n}/adopt``,
    task W7) and it needs the mirror to supply ``repo``/``number``/
    ``node_id``/``url``; letting this route fabricate an adoption record
    from caller-supplied strings would create a second, unverified path
    to the one state transition D2 says must be deliberate. W1–W3 are the
    local surface, with zero GitHub dependency, by design.

    ``runtime`` is required and shares the todos vocabulary: it is
    persisted as ``created_by`` into a synced document, so it is
    validated against the same charset rather than taken as free text.

    ``status`` is a plain ``str``, not :data:`WorkitemStatus`, for the
    same reason ``CreateTodoRequest.status`` is: the store validates it
    and the route maps that to the documented ``400 invalid_status``.
    Typing it as a ``Literal`` would turn the same request into a 422
    with a different body.
    """

    runtime: str
    title: str
    body: Optional[str] = None
    labels: Optional[list[str]] = None
    status: Optional[str] = None
    state_reason: Optional[str] = None
    assignee: Optional[str] = None
    todo_ids: Optional[list[str]] = None
    session_ids: Optional[list[str]] = None


#: The nullable trio — the fields for which "absent" and "null" are
#: different requests. Everything else on the PATCH body is two-state.
#: Module level rather than a class attribute so it is a plain constant
#: and not something Pydantic has to be told to ignore.
_WORKITEM_NULLABLE_FIELDS: tuple[str, ...] = ("body", "state_reason", "assignee")


class UpdateWorkitemRequest(_ForbidExtra):
    """PATCH /api/xo-projects/{id}/workitems/{workitem_id}.

    **Three-way, not two-way.** ``body``, ``state_reason`` and
    ``assignee`` are nullable, so this model has to express three
    distinct requests where a plain ``Optional`` field expresses two:

    ===========================  ==========================  ==============
    request                      meaning                     store kwarg
    ===========================  ==========================  ==============
    key absent                   leave it alone              *not passed*
    ``{"assignee": null}``       clear it                    ``None``
    ``{"assignee": "ada"}``      set it                      ``"ada"``
    ===========================  ==========================  ==============

    Collapsing the first two would make un-assigning a workitem
    impossible over HTTP — the store's ``UNSET`` sentinel exists for
    exactly this and flattening it here would waste it.
    :meth:`store_kwargs` is where the distinction is read, out of
    Pydantic's ``model_fields_set`` (the set of keys the *request*
    carried, which is the only place the information survives).

    ``title``, ``labels``, ``status``, ``todo_ids`` and ``session_ids``
    are not nullable, so for them ``null`` and absent mean the same
    thing — "not supplied" — which is also what the store reads them as.
    """

    title: Optional[str] = None
    labels: Optional[list[str]] = None
    status: Optional[str] = None
    todo_ids: Optional[list[str]] = None
    session_ids: Optional[list[str]] = None
    body: Optional[str] = None
    state_reason: Optional[str] = None
    assignee: Optional[str] = None

    def store_kwargs(self) -> dict:
        """The keyword arguments to hand ``workitems_store.update_workitem``.

        A nullable field the caller did not mention is **omitted**, not
        passed as ``None``: the store's own parameter default is its
        ``UNSET`` sentinel, so omission is how "not supplied" is spelled
        and the sentinel never has to cross this layer. A nullable field
        the caller did mention is passed exactly as sent, ``null``
        included.
        """
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
    """DELETE /api/xo-projects/{id}/workitems/{workitem_id} — idempotent
    (returns ``deleted: false`` when the workitem was already absent or
    already tombstoned)."""

    project_id: str
    workitem_id: str
    deleted: bool


# ── /workitems/{id}/claim ───────────────────────────────────────────────────
#
# The API for "an agent is working this" is a **claim, not a status
# write** (§5.4). The difference is the whole design: a status write
# leaves a flag that has to be un-written, and stays wrong if the
# process that wrote it dies; a claim is a fact about a session, and it
# stops meaning anything the moment that session stops being observed.
# So there is no ``in_progress`` on any request body here, and no way
# to set one.


class ClaimWorkitemRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/workitems/{workitem_id}/claim.

    ``session_id`` is the identity whose liveness the derived
    ``in_progress`` follows. Either handle the session index knows works
    — the runtime's native session id or the composite cowork key — and
    it is validated against the same charset as ``links.session_ids``
    rather than taken as free text.

    ``runtime`` is recorded alongside so a claim can say *what* is
    working the item, not merely that something is.
    """

    session_id: str
    runtime: str


class WorkitemClaim(_ForbidExtra):
    """The claim as recorded, plus what it currently means.

    ``in_progress`` is re-derived for the response rather than assumed
    ``true``: the claim is written, but whether it *reads* as in
    progress is a question about observed session presence, and a
    response that asserted otherwise would be the stored flag this
    design exists to avoid, one layer up.
    """

    project_id: str
    workitem_id: str
    session_id: str
    runtime: str
    started_at: str
    in_progress: bool


class ReleaseWorkitemClaimResponse(_ForbidExtra):
    """DELETE /api/xo-projects/{id}/workitems/{workitem_id}/claim —
    idempotent (``released: false`` when there was no claim to drop),
    matching the tombstoning DELETE above."""

    project_id: str
    workitem_id: str
    released: bool


# ── /github/issues, /adoption, /assignee ────────────────────────────────────
#
# The GitHub-facing half of the surface (workitems-plan §7.2, W7 and W8).
# Three shapes, and the split between them is the design:
#
# * the **mirror** is served as-is, with one derived field per row saying
#   whether a workitem already tracks it. It is the "what could I adopt"
#   view, and it is read-only — the poller is the mirror's single writer;
# * **adoption** answers with a ``Workitem``, because that is what it
#   produced; it does not invent a third representation of the same record;
# * **assignment** answers with its own shape rather than a ``Workitem``,
#   because the honest answer has a tense. Assignment for an adopted item is
#   written to GitHub and read back by the *next poll*, so a ``Workitem``
#   rendered now would show the old assignee and look like the write was
#   lost. ``pending`` says which of the two happened.


class GithubIssueAssignee(_ForbidExtra):
    """One assignee as the mirror holds them. The avatar is optional
    because GitHub does not always return one, and a missing picture must
    not cost the login it belongs to."""

    login: str
    avatar_url: Optional[str] = None


class GithubIssue(_ForbidExtra):
    """One row of the mirror, plus whether it is already tracked.

    ``labels`` is ``None``, never ``[]``, when the poller wrote the row:
    a nested ``labels`` connection was measured to double the poll's cost
    and halve the project ceiling (§6.2), so the poll does not fetch them
    and an empty array here would be the claim "this issue has no
    labels" — which the poll never establishes. Adoption fetches them
    once, separately.

    ``adopted`` / ``workitem_id`` are derived at read time by matching
    ``node_id`` against the project's workitems. ``node_id`` and not
    ``number``, because the node id survives a repository rename or
    transfer and the pair ``repo``/``number`` does not.
    """

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
    #: live claim on its workitem (never stored). ``False`` for an issue
    #: nobody has adopted, because an unadopted issue has no workitem to
    #: claim. Present here so a caller can render the browse list without
    #: joining it against ``GET /workitems`` on ``workitem_id``.
    in_progress: bool = False


class GithubMirrorError(_ForbidExtra):
    """The last poll failure, structured rather than a bare string.

    "Connect GitHub" and "the network is down" need different
    affordances, so ``kind`` is the closed vocabulary in
    ``connectors/github_issues.ERROR_KINDS`` and the UI branches on it
    instead of matching on prose.
    """

    kind: str
    message: str
    at: Optional[str] = None


class GithubIssuesResponse(_ForbidExtra):
    """``GET /github/issues`` — the mirror, and how much of it is untracked.

    ``fetched_at`` is null before the first successful poll, which is a
    real and common state: the poller only visits projects that are being
    looked at, hold adopted items, or have a live session (D9), and this
    endpoint is the signal that the first of those is now true.

    ``untracked`` counts the **open** rows no workitem tracks — the
    number a UI puts on a badge. Closed issues nobody adopted are not
    work anyone is being asked to notice, so counting them would make the
    badge grow forever on an old repository.
    """

    project_id: str
    repo: Optional[str] = None
    fetched_at: Optional[str] = None
    error: Optional[GithubMirrorError] = None
    issues: list[GithubIssue] = []
    untracked: int = 0
    tracked: int = 0


class AdoptIssueRequest(_ForbidExtra):
    """POST /api/xo-projects/{id}/github/issues/{number}/adopt.

    ``runtime`` shares the todos vocabulary and is persisted as
    ``created_by``, so it is validated against the same charset rather
    than taken as free text.

    ``workitem_id`` is optional and points adoption at an **existing
    local** workitem instead of minting a new one, so a note somebody has
    already been keeping — with its todo links and its history — becomes
    the record for the issue rather than being duplicated beside it. It
    is not D8's promotion: nothing is created on GitHub, and the issue
    being adopted was already public. The local ``status``,
    ``state_reason`` and ``body`` are dropped in the transition, because
    for an adopted item those three are GitHub's and a kept copy would
    assert a state nothing maintains. ``assignee`` is **kept**: it is a
    local annotation (§13, amendment 33), and adopting an issue does not
    change who this Space decided owes the work.
    """

    runtime: str
    workitem_id: Optional[str] = None


class AssignWorkitemRequest(_ForbidExtra):
    """PUT /api/xo-projects/{id}/workitems/{workitem_id}/assignee.

    A ``PUT`` because it *replaces*: ``{"assignee": null}`` un-assigns and
    ``{"assignee": "ada"}`` assigns, with no third reading. That is why
    the field is required-but-nullable rather than optional — an omitted
    key on a ``PUT`` of one value would have no meaning to give it.

    ``"me"`` resolves to this Space's own identity — ``resolve_user_id()``,
    the same name a local self-assignment has always carried. It needs no
    network and no credential: assignment is a local annotation now (§13,
    amendment 33), so there is nothing to route and nobody to ask.

    **Any identity is accepted, for any workitem.** The old
    ``local_assignee_only`` refusal existed because only GitHub could route
    work to a peer and a local item could never reach one; with no GitHub
    write left, that reason is gone. What is also gone is the guarantee it
    was standing in for: an assignment is stored in ``.xo/``, which is
    snapshot backup/restore rather than continuous merge, so assigning a
    peer records an intention **this Space can see** and does not deliver
    work to them.
    """

    assignee: Optional[str]


class WorkitemAssignment(_ForbidExtra):
    """The result of an assignment.

    ``kind`` is the workitem's ``source.kind`` as stored — ``"local"`` |
    ``"github"`` — not "where the write went", which is what it used to
    mean. Since amendment 33 the write always goes to the same place:
    ``.xo/workitems.json``. (The listing calls the same distinction
    ``origin``, with ``"space"`` where this says ``"local"``; see
    :class:`Workitem`.)

    ``pending`` is therefore **always false** and is kept only because it
    was on the wire. It used to mean "GitHub has it and this Space will
    read it back from the next poll", which was true when an adopted
    item's assignment was a ``PATCH`` against the issue. Nothing is
    outstanding now: the response is served from the record that was
    written.

    ``assignees`` is ``assignee`` as a list, at most one long — this
    Space's own assignment, never GitHub's. GitHub's assignees are on the
    workitem itself, as ``github_assignees``.
    """

    project_id: str
    workitem_id: str
    kind: Literal["local", "github"]
    assignee: Optional[str] = None
    assignees: list[str] = []
    pending: bool = False
