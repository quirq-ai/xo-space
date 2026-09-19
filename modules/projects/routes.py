"""The projects module's routes: the project list and files, the records of
one project, the workspace rollup, the Space timeline and the graph views.

  GET    /api/xo-projects                                    every project, newest first
  POST   /api/xo-projects                                    clone a repository into the projects root
  GET    /api/xo-projects/{id}/removal                       what blocks removing a local project
  DELETE /api/xo-projects/{id}                               remove a local project (body confirms its id)
  GET    /api/xo-projects/{id}/tree?relative_path=           one level of the project's folder
  GET    /api/xo-projects/{id}/file?relative_path=&commit=   a text file, now or at a commit
  GET    /api/xo-projects/{id}/file-history?relative_path=   the commits that touched a file
  GET    /api/xo-projects/{id}/todos                          the per-session todo list; POST creates
  GET    PATCH DELETE /api/xo-projects/{id}/todos/{todo_id}
  GET    /api/xo-projects/{id}/workitems                      the durable work items; POST creates
  GET    PATCH DELETE /api/xo-projects/{id}/workitems/{wid}
  POST   DELETE /api/xo-projects/{id}/workitems/{wid}/claim  a session is (no longer) working it
  PUT    /api/xo-projects/{id}/workitems/{wid}/assignee      who owes it (always a local write)
  DELETE /api/xo-projects/{id}/workitems/{wid}/adoption      stop mirroring the issue, keep the item
  GET    /api/xo-projects/{id}/github/issues?refresh=         the GitHub issue mirror
  POST   /api/xo-projects/{id}/github/issues/{n}/adopt       track an issue as a workitem
  GET    /api/xo-projects/{id}/peers                          the collaborator roster; POST adds
  GET    PATCH DELETE /api/xo-projects/{id}/peers/{user_id}
  GET    /api/xo-projects/{id}/activity                       which sessions are open right now
  GET    /api/xo-projects/{id}/timeline                       the project's log, newest first
  GET    /api/xo-projects/timeline                            the merged Space view
  GET    /api/workspace/workitems?assignee=&status=&limit=    every workitem across every project
  GET    /xo/space.json  /xo/dashboard.json                   the workspace graph and its projection

Same paths and bodies as the BFF routers these came from
(``routers/cowork_agent/bff/{xo_projects,project_management,visualizer,
workspace_visualizer}.py`` and ``routers/xo_data.py``); the usage and
analytics routes stayed behind for the telemetry module. Thin over
``modules.projects.service``: the stores' and the facade's typed failures
carry their own status and reach the wire through the app's service error
handler (``routers/errors.py``). The timeline routes read through
``modules.timeline.service`` by way of the scope handles.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr, ValidationError

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AdoptIssueRequest,
    AssignWorkitemRequest,
    ClaimWorkitemRequest,
    CreatePeerRequest,
    CreateTodoRequest,
    CreateWorkitemRequest,
    DeletePeerResponse,
    DeleteTodoResponse,
    DeleteWorkitemResponse,
    GithubIssue,
    GithubIssueAssignee,
    GithubIssuesResponse,
    GithubMirrorError,
    OpenSession,
    Peer,
    PeersResponse,
    ReleaseWorkitemClaimResponse,
    SessionTodos,
    TimelineEvent,
    TimelineResponse,
    Todo,
    TodosResponse,
    UpdatePeerRequest,
    UpdateTodoRequest,
    UpdateWorkitemRequest,
    Workitem,
    WorkitemAssignment,
    WorkitemClaim,
    WorkitemLinks,
    WorkitemSource,
    WorkitemsResponse,
    _ForbidExtra,
)
from routers.cowork_agent.bff._visualizer_presenter import (
    bad_query as _bad_query,
    parse_types_param as _parse_types_param,
)
from routers.cowork_agent.bff.filters import is_hidden_name, is_root_only_hidden
from routers.errors import ForbidExtra
from services.cowork_agent import coder_identity, github_poller
from services.cowork_agent.connectors import github as github_connector
from services.cowork_agent.connectors.github import issue_actions as github_issue_actions
from services.errors import ServiceError
from services.storage.document import CORRUPT_DOCUMENT_MESSAGE, UNSUPPORTED_SCHEMA_MESSAGE

from . import service
from . import workitem_projection as _projection
from .peers_store import VALID_ROLES as _PEER_ROLES
from .todo_status import VALID_TODO_STATUSES
from .workitems_store import (
    VALID_STATE_REASONS as _WORKITEM_STATE_REASONS,
    VALID_STATUSES as _WORKITEM_STATUSES,
    is_adopted as _is_adopted,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


def _require_project(project_id: str) -> service.VisualizerScope:
    """Resolve a project-scope handle or 404."""
    return service.require_project(project_id)


# ── /api/xo-projects: the list ───────────────────────────────────────────────


class Project(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    unscaffolded: bool


class ListProjectsResponse(BaseModel):
    items: list[Project]
    total: int


@router.get("/api/xo-projects", response_model=ListProjectsResponse)
def list_xo_projects() -> ListProjectsResponse:
    """Every project under the projects root, newest first; never a path."""
    items = [Project(**row) for row in service.list_projects()]
    return ListProjectsResponse(items=items, total=len(items))


# ── Local project management: clone and remove ───────────────────────────────


def _require_mutation(request: Request) -> None:
    """Require JSON and the same origin for browsers, including remote Space.

    CLI clients do not send Origin. No loopback-peer requirement here, unlike
    the command routes: a Docker install reaches the server from its bridge
    address. See ``routers/browser_guard.py`` for the origin rule.
    """
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ServiceError("json_required", "Send an application/json request.", 415)
    if not origin_allowed(request):
        raise ServiceError("same_origin_required", "Manage projects from this Space's Setup page.", 403)


class CloneProject(ForbidExtra):
    project_id: StrictStr
    repository_url: StrictStr


class RemoveProject(ForbidExtra):
    confirm_project_id: StrictStr


@router.post("/api/xo-projects", status_code=201)
async def clone_project(body: CloneProject, request: Request) -> dict:
    """Clone a repository into the projects root as a new project."""
    _require_mutation(request)
    return await service.project_management.clone_project(body.project_id, body.repository_url)


@router.get("/api/xo-projects/{project_id}/removal")
async def removal_status(project_id: str) -> dict:
    """Whether the local project can be removed, and what blocks it."""
    return await service.project_management.removal_status(project_id)


@router.delete("/api/xo-projects/{project_id}")
async def remove_project(project_id: str, body: RemoveProject, request: Request) -> dict:
    """Remove a local project folder after fresh sharing checks."""
    _require_mutation(request)
    return await service.project_management.remove_project(project_id, body.confirm_project_id)


# ── /api/xo-projects/{id}/tree ───────────────────────────────────────────────


class TreeEntry(BaseModel):
    """One row of a project's file explorer.

    ``size_bytes`` is set for files, ``entries`` for directories, and both are
    optional: a broken symlink or a file deleted mid-listing still gets a row
    with a name, just without detail.
    """

    name: str
    relative_path: str
    is_dir: bool = False
    size_bytes: Optional[int] = None
    modified_at: Optional[str] = None
    entries: Optional[int] = None


class ProjectTreeResponse(BaseModel):
    project_id: str
    relative_path: str
    parent_relative_path: Optional[str] = None
    dirs: list[TreeEntry]
    files: list[TreeEntry]


def _filter_tree_entries(entries: list[dict], at_root: bool, *, is_dir: bool) -> list[TreeEntry]:
    out: list[TreeEntry] = []
    for e in entries:
        name = e.get("name") or ""
        if is_hidden_name(name):
            continue
        if at_root and is_root_only_hidden(name):
            continue
        out.append(
            TreeEntry(
                name=name,
                relative_path=e.get("relative_path") or "",
                is_dir=is_dir,
                size_bytes=e.get("size_bytes"),
                modified_at=service._to_iso_utc(e.get("modified_at")),
                entries=e.get("entries"),
            )
        )
    return out


@router.get("/api/xo-projects/{project_id}/tree", response_model=ProjectTreeResponse)
def project_tree(project_id: str, relative_path: str = "") -> ProjectTreeResponse:
    """One level of a project's folder, dotfiles and editor leftovers hidden."""
    raw = service.project_tree(project_id, relative_path)
    at_root = (raw["relative_path"] or "") == ""
    return ProjectTreeResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        parent_relative_path=raw["parent_relative_path"],
        dirs=_filter_tree_entries(raw["dirs"], at_root, is_dir=True),
        files=_filter_tree_entries(raw["files"], at_root, is_dir=False),
    )


# ── /api/xo-projects/{id}/file ───────────────────────────────────────────────
#
# Preview one text file from inside a project. Deliberately NOT the older
# POST /api/files/content: that takes an absolute host path, and the Space UI
# never sees absolute paths. Here the project id plus a project-relative path
# is the whole address space, validated by the same helper the tree uses.

PREVIEW_MAX_BYTES = service.PREVIEW_MAX_BYTES
PREVIEW_SUFFIXES = service.PREVIEW_SUFFIXES


class FilePreviewResponse(BaseModel):
    project_id: str
    relative_path: str
    name: str
    kind: str
    size_bytes: int
    modified_at: Optional[str] = None
    truncated: bool
    content: str


@router.get("/api/xo-projects/{project_id}/file", response_model=FilePreviewResponse)
def project_file(
    project_id: str,
    relative_path: str,
    commit: Optional[str] = None,
    commit_path: Optional[str] = None,
) -> FilePreviewResponse:
    """Return one previewable text file's content.

    With ``commit`` (and, across renames, ``commit_path``: the ``path``
    field of the matching /file-history item) the content comes from
    that commit instead of the working tree, so the previewer's version
    picker can show the document as it was.
    """
    return FilePreviewResponse(**service.project_file(
        project_id, relative_path, commit=commit, commit_path=commit_path,
    ))


# ── /api/xo-projects/{id}/file-history ───────────────────────────────────────

HISTORY_MAX_COMMITS = service.HISTORY_MAX_COMMITS
HISTORY_DEFAULT_COMMITS = service.HISTORY_DEFAULT_COMMITS


class FileHistoryCommit(BaseModel):
    """One commit touching the file.

    ``additions``/``deletions`` are ``None`` when git has no counts to
    give: a binary file, or a commit that only renamed it. ``path`` is
    the file's name at that commit; echo it back as ``commit_path`` when
    asking /file for that version, so renamed files resolve under the
    name the commit knew.
    """

    hash: str
    short_hash: str
    author: str
    date: Optional[str] = None
    subject: str
    additions: Optional[int] = None
    deletions: Optional[int] = None
    path: Optional[str] = None


class FileHistoryResponse(BaseModel):
    project_id: str
    relative_path: str
    is_repo: bool
    items: list[FileHistoryCommit]
    total: int


@router.get("/api/xo-projects/{project_id}/file-history", response_model=FileHistoryResponse)
def project_file_history(
    project_id: str, relative_path: str, limit: int = HISTORY_DEFAULT_COMMITS
) -> FileHistoryResponse:
    """Return the git log of one project file's edits."""
    raw = service.file_history(project_id, relative_path, limit=limit)
    return FileHistoryResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        is_repo=raw["is_repo"],
        items=[FileHistoryCommit(**item) for item in raw["items"]],
        total=len(raw["items"]),
    )


# ── /api/xo-projects/{id}/todos ──────────────────────────────────────────────
# EVERY agent writes todos through this API rather than touching .xo/todos.json
# directly, including runtimes with a native todo tool, whose tool calls no
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


def _shape_todos(project_id: str, raw: Optional[dict], *, include_deleted: bool = False) -> TodosResponse:
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


@router.get("/api/xo-projects/{project_id}/todos", response_model=TodosResponse)
def project_todos(
    project_id: str,
    include_deleted: bool = Query(False, description="Include soft-deleted todos (tombstones)."),
) -> TodosResponse:
    """Per-session task list for one project."""
    raw = service.project_todos(project_id)
    return _shape_todos(project_id, raw, include_deleted=include_deleted)


@router.post("/api/xo-projects/{project_id}/todos", response_model=Todo, status_code=201)
def project_todos_create(project_id: str, body: CreateTodoRequest) -> Todo:
    """Create a new todo under the project (any runtime can call).

    ``session_id`` defaults to the ``"_project"`` pseudo-session so
    callers without a session concept don't have to invent one.
    """
    scope = _require_project(project_id)
    new = scope.create_todo(
        runtime=body.runtime,
        content=body.content,
        description=body.description,
        active_form=body.active_form,
        session_id=body.session_id,
        status=body.status,
    )
    return _make_todo_model(new)


@router.get("/api/xo-projects/{project_id}/todos/{todo_id}", response_model=Todo)
def project_todos_get(project_id: str, todo_id: str) -> Todo:
    """Fetch one todo by id."""
    scope = _require_project(project_id)
    found = scope.get_todo(todo_id)
    if found is None:
        raise ServiceError("todo_not_found", "Todo not found.", 404)
    _, todo = found
    return _make_todo_model(todo)


@router.patch("/api/xo-projects/{project_id}/todos/{todo_id}", response_model=Todo)
def project_todos_update(project_id: str, todo_id: str, body: UpdateTodoRequest) -> Todo:
    """Update fields on an existing todo. Most common: status transition
    ``pending`` to ``in_progress`` to ``completed``.
    """
    scope = _require_project(project_id)
    updated = scope.update_todo(
        todo_id,
        status=body.status,
        content=body.content,
        description=body.description,
        active_form=body.active_form,
    )
    return _make_todo_model(updated)


@router.delete("/api/xo-projects/{project_id}/todos/{todo_id}", response_model=DeleteTodoResponse)
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
    Soft delete: the record is tombstoned (``deleted_at`` set), never removed,
    so it cannot come back and the history stays readable.
    """
    scope = _require_project(project_id)
    deleted = scope.delete_todo(todo_id, deleted_by=runtime)
    return DeleteTodoResponse(project_id=project_id, todo_id=todo_id, deleted=deleted)


# ── /api/xo-projects/{id}/workitems ──────────────────────────────────────────
# The project tier of workitems-plan §7.1, and deliberately the todos surface
# again: same ``runtime`` vocabulary, same tombstone semantics, same ``{"code":
# ..., "message": ...}`` 400 bodies, same optional ``?runtime=`` on DELETE.
# The stores' errors carry their own status (a caller error 400, a missing
# record 404, a refused document 409 with the path kept out of the wire) and
# reach the client through the app's service error handler; nothing is mapped
# here.


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


def _require_workitem(scope: service.VisualizerScope, workitem_id: str, *, include_deleted: bool = False) -> dict:
    """The stored record, or the 404/4xx the store's failure deserves."""
    found = scope.get_workitem(workitem_id, include_deleted=include_deleted)
    if found is None:
        raise ServiceError("workitem_not_found", "Workitem not found.", 404)
    return found


def _in_progress_ids(scope: service.VisualizerScope) -> frozenset[str]:
    """The derived set of workitems an agent is working right now (§5.4)."""
    return scope.in_progress_workitem_ids()


def _mirror_issues(scope: service.VisualizerScope) -> dict[str, dict]:
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


@router.get("/api/xo-projects/{project_id}/workitems", response_model=WorkitemsResponse)
def project_workitems_list(
    project_id: str,
    status: Optional[str] = Query(default=None, description="Filter to `open` or `closed`."),
    assignee: Optional[str] = Query(default=None, description="Filter to one assignee."),
    kind: Optional[str] = Query(default=None, description="Filter to `local` or `github` (adopted) items."),
    include_deleted: bool = Query(False, description="Include soft-deleted workitems (tombstones)."),
) -> WorkitemsResponse:
    """Every workitem in the project, oldest first."""
    scope = _require_project(project_id)
    rows = scope.list_workitems(
        status=status,
        kind=kind,
        assignee=assignee,
        include_deleted=include_deleted,
    )
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


@router.post("/api/xo-projects/{project_id}/workitems", response_model=Workitem, status_code=201)
def project_workitems_create(project_id: str, body: CreateWorkitemRequest) -> Workitem:
    """Create a workitem under the project (any runtime can call)."""
    scope = _require_project(project_id)
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
    # Projected even though a created item is always local and the mirror can
    # therefore say nothing about it: the projection is what fills
    # ``assignees``/``github_assignees`` beside the stored ``assignee``, and a
    # create that answered in a different shape from the following GET would be
    # a second wire contract for one record.
    return _make_workitem_model(_projection.project_workitem(new))


@router.get("/api/xo-projects/{project_id}/workitems/{workitem_id}", response_model=Workitem)
def project_workitems_get(project_id: str, workitem_id: str) -> Workitem:
    """Fetch one workitem by id."""
    scope = _require_project(project_id)
    found = _require_workitem(scope, workitem_id)
    return _make_workitem_model(
        _projection.project_workitem(found, issues=_mirror_issues(scope)),
        in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.patch("/api/xo-projects/{project_id}/workitems/{workitem_id}", response_model=Workitem)
def project_workitems_update(project_id: str, workitem_id: str, body: UpdateWorkitemRequest) -> Workitem:
    """Update fields on an existing workitem."""
    scope = _require_project(project_id)
    updated = scope.update_workitem(workitem_id, **body.store_kwargs())
    projected = _projection.project_workitem(updated, issues=_mirror_issues(scope))
    if updated.get("status") == "closed":
        scope.release_workitem_quiet(workitem_id)
        return _make_workitem_model(projected, in_progress=False)
    return _make_workitem_model(
        projected, in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.delete("/api/xo-projects/{project_id}/workitems/{workitem_id}", response_model=DeleteWorkitemResponse)
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
    Soft delete: the record is tombstoned (``deleted_at`` set), never removed,
    so it cannot come back and the history stays readable.
    """
    scope = _require_project(project_id)
    deleted = scope.delete_workitem(workitem_id, deleted_by=runtime)
    if deleted:
        # Same implicit release as closing (§5.4): a tombstoned workitem is not
        # work in progress.
        scope.release_workitem_quiet(workitem_id)
    return DeleteWorkitemResponse(project_id=project_id, workitem_id=workitem_id, deleted=deleted)


# ── /api/xo-projects/{id}/workitems/{id}/claim: derived in_progress ─────────
# §5.4, task W7b.


@router.post("/api/xo-projects/{project_id}/workitems/{workitem_id}/claim", response_model=WorkitemClaim)
def project_workitems_claim(project_id: str, workitem_id: str, body: ClaimWorkitemRequest) -> WorkitemClaim:
    """Record that a session is working this workitem."""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id)
    claim = scope.claim_workitem(workitem_id, session_id=body.session_id, runtime=body.runtime)
    return WorkitemClaim(
        project_id=project_id,
        workitem_id=workitem_id,
        session_id=claim["session_id"],
        runtime=claim["runtime"],
        started_at=claim["started_at"],
        in_progress=workitem_id in _in_progress_ids(scope),
    )


@router.delete("/api/xo-projects/{project_id}/workitems/{workitem_id}/claim", response_model=ReleaseWorkitemClaimResponse)
def project_workitems_release(project_id: str, workitem_id: str) -> ReleaseWorkitemClaimResponse:
    """Drop the claim: the explicit half of "stopped working on this"."""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id, include_deleted=True)
    released = scope.release_workitem(workitem_id)
    return ReleaseWorkitemClaimResponse(project_id=project_id, workitem_id=workitem_id, released=released)


# ── /api/xo-projects/{id}/github/*: the mirror, and adoption ─────────────────
# §7.2, tasks W7 and W8. Three facts shape everything below. (1) GitHub is
# read-only to this system: assignment writes a local annotation into
# ``.xo/workitems.json``, never an issue. (2) Adoption is explicit: the mirror
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


def _github_error(kind: Optional[str], message: Optional[str]) -> ServiceError:
    """One ``gh`` failure, as the answer it deserves."""
    status, code = _GITHUB_FAILURES.get(kind or "", (502, "github_unavailable"))
    return ServiceError(code, message or f"GitHub call failed ({kind}).", status)


def _require_github_repo(scope: service.VisualizerScope) -> str:
    """The project's ``owner/name``, or a 400 that says why there is none."""
    repo = scope.github_repo()
    if not repo:
        raise ServiceError(
            "not_a_github_project",
            "This project has no github.com remote in project.json, so there "
            "are no issues to adopt. Set a GitHub origin, or keep the work as "
            "local workitems.",
            400,
        )
    return repo


def _github_budget_gate() -> None:
    """Refuse an interactive GitHub call the poller has already stood down for."""
    snapshot = github_poller.budget_snapshot()
    if snapshot.get("paused"):
        raise ServiceError(
            "github_rate_limited",
            "GitHub calls are paused: "
            f"{snapshot.get('pause_reason') or 'the poller backed off'}. "
            "This affects the whole machine, not this project, and it "
            "clears on its own.",
            503,
        )
    remaining = snapshot.get("remaining")
    if isinstance(remaining, int) and remaining <= 0:
        raise ServiceError(
            "github_rate_limited",
            "The GraphQL budget is spent; it refills at "
            f"{snapshot.get('reset_at') or 'the next reset'}.",
            503,
        )


def _issues_state(*, repo: Optional[str], mirror: Optional[dict], error: Optional[GithubMirrorError], rows: int) -> str:
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


def _issue_model(row: dict, tracked: dict[str, str], live: frozenset[str] = frozenset()) -> GithubIssue:
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
        # labels, and an empty list would be the claim that the issue has
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


@router.get("/api/xo-projects/{project_id}/github/issues", response_model=GithubIssuesResponse)
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
    untracked = sum(1 for row in models if row.state == "open" and not row.adopted)

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
        state=_issues_state(repo=repo, mirror=mirror, error=error, rows=len(models)),
    )


def _mirror_row_for_number(scope: service.VisualizerScope, number: int) -> Optional[dict]:
    """The mirror's row for issue ``number``, or ``None``."""
    for row in _mirror_issues(scope).values():
        if row.get("number") == number:
            return dict(row)
    return None


@router.post("/api/xo-projects/{project_id}/github/issues/{issue_number}/adopt", response_model=Workitem, status_code=201)
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

    if not created:
        # Already tracked, or an existing workitem now mirrors the issue.
        # Nothing was minted, so 201 would be a lie a client may act on.
        response.status_code = 200
    return _make_workitem_model(
        _projection.project_workitem(record, issues=_mirror_issues(scope)),
        in_progress=record.get("id") in _in_progress_ids(scope),
    )


@router.delete("/api/xo-projects/{project_id}/workitems/{workitem_id}/adoption", response_model=Workitem)
def project_workitem_unadopt(project_id: str, workitem_id: str) -> Workitem:
    """Stop mirroring the issue. **Keep the workitem** (§7.2)."""
    scope = _require_project(project_id)
    found = _require_workitem(scope, workitem_id)

    projected = _projection.project_workitem(found, issues=_mirror_issues(scope))
    record = scope.unadopt_workitem(
        workitem_id,
        status=projected.get("status"),
        state_reason=projected.get("state_reason"),
    )
    return _make_workitem_model(
        _projection.project_workitem(record),
        in_progress=workitem_id in _in_progress_ids(scope),
    )


# ── /api/xo-projects/{id}/workitems/{id}/assignee: W8, amended ───────────────
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


@router.put("/api/xo-projects/{project_id}/workitems/{workitem_id}/assignee", response_model=WorkitemAssignment)
async def project_workitem_assign(project_id: str, workitem_id: str, body: AssignWorkitemRequest) -> WorkitemAssignment:
    """Set (or clear) who owes this workitem. **Always a local write.**"""
    scope = _require_project(project_id)
    _require_workitem(scope, workitem_id)

    wanted = (body.assignee or "").strip() or None
    if wanted is None:
        resolved: Optional[str] = None
    elif wanted.lower() in _SELF_ALIASES:
        # ``resolve_user_id`` is total (the authenticated user, the Coder
        # owner, or the literal ``"local"``), so this list is never empty and
        # there is no "me is nobody" case to answer.
        resolved = _self_identities()[0]
    else:
        # A leading ``@`` is how people write a login and is not part of it.
        resolved = wanted.lstrip("@").strip() or None

    # Off the event loop: it is a locked read-modify-write of a file.
    updated = await asyncio.to_thread(scope.update_workitem, workitem_id, assignee=resolved)

    stored = updated.get("assignee")
    stored = stored if isinstance(stored, str) and stored else None
    return WorkitemAssignment(
        project_id=project_id,
        workitem_id=workitem_id,
        # The workitem's own kind, as stored, not "where the write went",
        # which is now always the same place.
        kind="github" if _is_adopted(updated) else "local",
        assignee=stored,
        assignees=[stored] if stored else [],
        # Nothing is outstanding: the file is the record and it is written.
        pending=False,
    )


# ── /api/xo-projects/{id}/peers: the collaborator roster ─────────────────────
# ``<project>/.xo/peers.json`` shipped in the project template as a stub with a
# schema already written and **no writer anywhere in the tree**.


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


@router.get("/api/xo-projects/{project_id}/peers", response_model=PeersResponse)
def project_peers(
    project_id: str,
    role: Optional[str] = Query(default=None, description="Filter to `owner`, `collaborator` or `viewer`."),
) -> PeersResponse:
    """Everyone this project is shared with, oldest first."""
    scope = _require_project(project_id)
    updated_at, peers = scope.read_peer_roster(role=role)
    return PeersResponse(
        project_id=project_id,
        updated_at=updated_at,
        peers=[_make_peer_model(row) for row in peers],
    )


@router.post("/api/xo-projects/{project_id}/peers", response_model=Peer, status_code=201)
def project_peers_create(project_id: str, body: CreatePeerRequest) -> Peer:
    """Add a collaborator to the roster."""
    scope = _require_project(project_id)
    new = scope.create_peer(
        user_id=body.user_id,
        role=body.role,
        label=body.label,
        endpoint=body.endpoint,
    )
    return _make_peer_model(new)


@router.get("/api/xo-projects/{project_id}/peers/{user_id}", response_model=Peer)
def project_peers_get(project_id: str, user_id: str) -> Peer:
    """Fetch one peer by ``user_id``."""
    scope = _require_project(project_id)
    found = scope.get_peer(user_id)
    if found is None:
        raise ServiceError("peer_not_found", "Peer not found.", 404)
    return _make_peer_model(found)


@router.patch("/api/xo-projects/{project_id}/peers/{user_id}", response_model=Peer)
def project_peers_update(project_id: str, user_id: str, body: UpdatePeerRequest) -> Peer:
    """Change a peer's ``role``, ``label`` or ``endpoint``."""
    scope = _require_project(project_id)
    updated = scope.update_peer(user_id, **body.store_kwargs())
    return _make_peer_model(updated)


@router.delete("/api/xo-projects/{project_id}/peers/{user_id}", response_model=DeletePeerResponse)
def project_peers_delete(project_id: str, user_id: str) -> DeletePeerResponse:
    """Remove a collaborator. **A hard delete: there is no tombstone.**"""
    scope = _require_project(project_id)
    deleted = scope.delete_peer(user_id)
    return DeletePeerResponse(project_id=project_id, user_id=user_id, deleted=deleted)


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
        # boot empty file may omit). Missing required field: skip
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


@router.get("/api/xo-projects/{project_id}/activity", response_model=ActivityResponse)
def project_activity(project_id: str) -> ActivityResponse:
    """Live presence: which sessions are open in this project right now.

    Empty ``{open_sessions: []}`` when the watcher hasn't written the
    machine-local presence snapshot yet. AGENTS.md's boot ritual calls
    this endpoint to answer "is anyone else working here right now?".
    """
    return _shape_activity(project_id, service.project_activity(project_id))


# ── /api/xo-projects/{id}/timeline and /api/xo-projects/timeline ─────────────


def _check_before(before: Optional[str]) -> None:
    if before is not None:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _bad_query("before must be an ISO-8601 timestamp") from exc


def _timeline_response(project_id: Optional[str], events: list[dict], limit: int) -> TimelineResponse:
    out: list[TimelineEvent] = []
    for ev in events:
        try:
            out.append(TimelineEvent(**ev))
        except Exception:
            logger.warning("timeline: dropping an event that failed validation")
            continue
    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=project_id, events=out, next_cursor=next_cursor)


@router.get("/api/xo-projects/{project_id}/timeline", response_model=TimelineResponse, response_model_exclude_unset=True)
def project_timeline(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Newest-first event stream for one project (its log, through ``modules.timeline``)."""
    _check_before(before)
    type_set = _parse_types_param(types)
    scope = _require_project(project_id)
    events = scope.read_timeline(limit=limit, before=before, types=type_set)
    return _timeline_response(project_id, events, limit)


@router.get("/api/xo-projects/timeline", response_model=TimelineResponse, response_model_exclude_unset=True)
def workspace_timeline(
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Multiplexed workspace timeline: every project's log plus the Space log,
    each event tagged with ``project_id`` (the merged read of ``modules.timeline``)."""
    _check_before(before)
    type_set = _parse_types_param(types)
    events = service.workspace().read_timeline(limit=limit, before=before, types=type_set)
    return _timeline_response(None, events, limit)


# ── /api/workspace/workitems: the agent-pingable rollup ──────────────────────
# workitems-plan §7.3, task W9. The endpoint an agent polls to answer "what is
# assigned to me", across every project on this machine, in one call.


class WorkspaceWorkitem(Workitem):
    """One rollup row: a workitem plus where it lives."""

    #: The project's **directory name**: the id every other route, and every
    #: path helper, takes.
    project_id: str
    #: The project's durable identity from ``project.json``.
    pid: Optional[str] = None


class SkippedProject(_ForbidExtra):
    """A project the rollup could not read, and why."""

    project_id: str
    pid: Optional[str] = None
    #: The store's own code: ``corrupt_document``, ``unsupported_schema``, or
    #: ``unavailable`` for anything else (a directory that went away between
    #: the walk and the read, a permission change).
    code: str
    #: Path-free. The store's message names an absolute path, which is logged
    #: for the operator instead of being served to the caller.
    message: str


class WorkspaceWorkitemsResponse(_ForbidExtra):
    """``GET /api/workspace/workitems``: the cross-project answer."""

    workitems: list[WorkspaceWorkitem]
    #: Rows in ``workitems``, after ``?limit=``.
    count: int
    #: Rows that matched, before ``?limit=``. ``total > count`` is the only
    #: way a caller can tell it is seeing part of the answer.
    total: int
    truncated: bool
    #: The ``?assignee=`` value as it arrived, echoed verbatim so a caller can
    #: see what was interpreted. ``null`` when none was given.
    assignee: Optional[str] = None
    #: What that value resolved to, and what rows were actually matched against
    #: (case-insensitively).
    identities: list[str] = []
    #: Why ``me`` could not be resolved in full; currently only
    #: ``no_github_credential``. ``null`` when it resolved, and when no
    #: assignee filter was given.
    assignee_unresolved: Optional[str] = None
    #: How many projects were walked (read *and* skipped).
    projects: int
    skipped: list[SkippedProject] = []


#: What a caller may write for "me".
_ME = _SELF_ALIASES

#: The message served for a skipped project whose failure has no entry in the
#: document-error table: a directory that disappeared between the walk and the
#: read, a permission change, an unreadable pid.
_SKIPPED_FALLBACK = (
    "{name} could not be read for this project, so its workitems are "
    "missing from this answer. The rest of the workspace is unaffected."
)

#: A refused document is described in the words the store's own 409 uses
#: (``services/storage/document.py``): the document's name, never its path.
_DOCUMENT_ERRORS: dict[str, str] = {
    "corrupt_document": CORRUPT_DOCUMENT_MESSAGE,
    "unsupported_schema": UNSUPPORTED_SCHEMA_MESSAGE,
}


async def _resolve_assignee(raw: Optional[str]) -> tuple[Optional[list[str]], Optional[str]]:
    """``?assignee=`` to (identities to match, unresolved reason)."""
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
    One skipped project, with the store's path-naming text logged, not served:
    the same split the store's own 409 makes.
    """
    code = str(entry.get("code") or "unavailable")
    detail = entry.get("detail")
    project_id = str(entry.get("project_id") or "")
    if detail:
        logger.error("workspace rollup skipped project %s (%s): %s", project_id, code, detail)
    template = _DOCUMENT_ERRORS.get(code, _SKIPPED_FALLBACK)
    pid = entry.get("pid")
    return SkippedProject(
        project_id=project_id,
        pid=pid if isinstance(pid, str) and pid else None,
        code=code,
        message=template.format(name="workitems.json"),
    )


@router.get("/api/workspace/workitems", response_model=WorkspaceWorkitemsResponse)
async def workspace_workitems(
    assignee: Optional[str] = Query(default=None, description="`me`, `@login`, or a login. Matched case-insensitively."),
    status: Optional[str] = Query(default=None, description="Filter to `open` or `closed`."),
    limit: int = Query(100, ge=1, le=500),
) -> WorkspaceWorkitemsResponse:
    """Every workitem in the workspace, filtered on the **projected** view."""
    if status is not None and status not in _WORKITEM_STATUSES:
        raise _bad_query(f"status must be one of {sorted(_WORKITEM_STATUSES)}")

    identities, unresolved = await _resolve_assignee(assignee)

    workspace = service.workspace()
    # Off the event loop: the fan-out is one walk plus a handful of small
    # blocking reads per project, and this route is ``async`` only because
    # resolving ``me`` is.
    rollup = await asyncio.to_thread(workspace.rollup_workitems, assignees=identities, status=status)

    rows = sorted(rollup.rows, key=_row_sort_key, reverse=True)
    total = len(rows)
    shown = rows[:limit]
    return WorkspaceWorkitemsResponse(
        workitems=[
            WorkspaceWorkitem(
                **_make_workitem_model(row, in_progress=bool(row.get("_in_progress"))).model_dump(),
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


# ── /xo/space.json and /xo/dashboard.json: the graphs ────────────────────────


@router.get("/xo/space.json")
async def space_json() -> JSONResponse:
    """The workspace graph: projects, folders, files, ties, git history."""
    return JSONResponse(await service.view_payload("space"), headers=_NO_STORE)


@router.get("/xo/dashboard.json")
async def dashboard_json() -> JSONResponse:
    """The graph collapsed into five purpose environments. Same schema as
    space.json, so the browser reuses one renderer."""
    return JSONResponse(await service.view_payload("dashboard"), headers=_NO_STORE)
