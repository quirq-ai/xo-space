"""CRUD over ``<project>/.xo/todos.json`` — and the todo event source.

Two things live here, and the second one is the point.

**1. The only writer of ``todos.json``.** The watcher's todo sink is
gone (syncplan §7, T8): it ingested task events that exactly one
runtime emits, so todos worked on one backend out of five and the file
had two writers with two incompatible id spaces. Every todo now enters
through the agent-facing ``POST/PATCH/DELETE /todos`` endpoints, which
means the document is byte-identical under every ``AGENT_NAME`` for the
same sequence of calls. :func:`flock.locked` still guards the
read-modify-write because concurrent *requests* (FastAPI's thread pool,
or a second uvicorn worker) are still two writers.

**2. The only producer of todo lifecycle events** (syncplan §7, T7).
Three consumers used to be fed from one runtime's transcript: this
file, the timeline sink and the per-session ``taskCount`` counters.
Removing only the first would have left the other two emitting
``todo.*`` lines and counting tasks that no longer exist anywhere on
disk. So the write path emits the same
:class:`~services.cowork_agent.visualizer.ingest.events.TaskCreated` /
:class:`~...events.TaskStatusChanged` events the sinks already know how
to render, and the watcher no longer feeds them from ingestion. One
source, every backend, and ``todos.json``, ``timeline.jsonl`` and
``sessions-augment.json`` cannot drift apart.

**Soft delete** (syncplan §5.5, T9). ``status`` is lifecycle only —
``cancelled`` is a real outcome and stays visible. Deletion is a
separate ``deleted_at`` / ``deleted_by`` tombstone: the record is never
removed, so "everything ever closed" stays answerable and a deleted
todo cannot come back. Reads hide tombstones unless asked.

The BFF route layer never imports this module directly — it goes
through ``services.cowork_agent.scopes.VisualizerScope`` (P3).
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.atomic_write import (
    write_json_atomic_if_changed,
)
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    TaskCreated,
    TaskStatusChanged,
)
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.sinks import sessions_augment, timeline
from services.cowork_agent.visualizer.todo_status import VALID_TODO_STATUSES


logger = logging.getLogger(__name__)

PROJECT_SESSION = "_project"          # default session_id when caller doesn't provide one

#: On-disk revision of ``todos.json`` (syncplan §5.5). Schema 2 declares
#: the per-todo ``created_at`` / ``updated_at`` timestamps and the
#: ``deleted_at`` / ``deleted_by`` tombstone.
TODOS_SCHEMA = 2

#: Value written into the document's ``$schema`` key. It is the schema's own
#: ``$id`` (visualizer/schema/todos.schema.json), NOT a filesystem path. The
#: previous value was a project-relative path into a ``.xo/schema/``
#: directory that nothing has ever created, so every todos.json on disk
#: carried a dangling pointer (syncplan T16). ``xo/<name>.schema.json`` is
#: the convention the other records already use — workspace/projects_json.py,
#: workspace/space_json.py, adapters/*/agents.py.
_SCHEMA_REF = "xo/todos.schema.json"

# The status vocabulary lives in one place — todo_status.py. Kept as a
# module name here because :func:`_validate_status` below is the single
# enforcement point in the whole system and reads better unqualified.
VALID_STATUSES = VALID_TODO_STATUSES

_DEFAULT_STATUS = "pending"

# Sanitisation regex for runtime / session_id. Permissive enough for
# realistic adapter keys (composite ``<agent>:<project>:<surface>:<id>``
# forms, dashes, dots, underscores) but rejects path traversal.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_session_entry(runtime: str) -> dict:
    return {
        "runtime": runtime,
        "source_file": None,
        "session_started_at": None,
        "todos": [],
    }


def _document(sessions: dict) -> dict:
    return {
        "$schema": _SCHEMA_REF,
        "schema": TODOS_SCHEMA,
        "updated_at": _now_iso(),
        "sessions": sessions,
    }


def is_deleted(todo: object) -> bool:
    """Whether a todo record carries a tombstone.

    One predicate so "deleted" means the same thing to the store, the
    routes and the tests. Absent / ``null`` ``deleted_at`` is alive —
    which is also what every pre-schema-2 record on disk says.
    """
    return isinstance(todo, dict) and todo.get("deleted_at") is not None


class TodosStoreError(Exception):
    """Base for all store failures. ``code`` is the BFF error code
    the route maps to ``detail.code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_safe_key(value: str, kind: str) -> None:
    if not isinstance(value, str) or not _SAFE_KEY_RE.match(value):
        raise TodosStoreError(
            "invalid_runtime" if kind == "runtime" else "invalid_session_id",
            f"{kind} must match [A-Za-z0-9_:\\-\\.] (1..200 chars).",
        )


def _validate_status(value: str) -> None:
    if value not in VALID_STATUSES:
        raise TodosStoreError(
            "invalid_status",
            f"status must be one of {sorted(VALID_STATUSES)}.",
        )


def _validate_content_length(value: str, *, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TodosStoreError("invalid_value", f"{field} is required and must be non-empty.")
    if len(value) > limit:
        raise TodosStoreError("invalid_value", f"{field} exceeds {limit} chars.")


# ── Lifecycle events ───────────────────────────────────────────────────────
#
# The API is the event source (syncplan T7). We hand the sinks the same
# event objects the watcher used to hand them, so the rendering — the
# timeline vocabulary, the per-status counters — lives in exactly one
# place and every backend produces identical output.


def _derived_dir(todos_path: Path) -> Optional[Path]:
    """Runtime directory holding the watcher's derived views for this project.

    ``timeline.jsonl`` and ``sessions/sessions-augment.json`` used to sit
    beside ``todos.json`` in ``.xo/``. T19 moved them to
    ``~/.quirq/projects/<key>/`` while ``todos.json`` — part of the synced
    contract — stayed put, so the two are no longer the same directory and
    this function is what keeps the todos API writing to the tier the
    watcher reads.

    ``todos_path`` is ``<project>/.xo/todos.json``, so the project folder is
    its grandparent. Returns ``None`` when that project has no runtime home
    (it was deleted, or its identity was never minted) — the caller then
    skips the derived write, exactly as the watcher does.
    """
    return project_layout.runtime_dir_for_project(todos_path.parent.parent.name)


def _emit(todos_path: Path, events: Iterable[Event]) -> None:
    """Fan todo lifecycle events to the sinks that render them.

    Never raises: a todo that is safely on disk must not turn into a 500
    because a derived view could not be updated. Both sinks are
    self-healing (the timeline is append-only, the counters are rebuilt
    from their own persisted state), so a logged failure costs one
    timeline line, not correctness of ``todos.json``.

    Called *after* the ``todos.json`` lock is released — the two sinks
    take their own locks, and nesting them under this one would be the
    only place in the system where two locks are held at once.
    """
    events = [ev for ev in events]
    if not events:
        return
    runtime_dir = _derived_dir(todos_path)
    if runtime_dir is None:
        return
    try:
        sessions_augment.apply(runtime_dir, events, legacy_root=todos_path.parent)
    except Exception:  # noqa: BLE001 - derived view, never fails the write
        logger.warning("todo events: augment counters failed for %s", runtime_dir, exc_info=True)
    try:
        timeline.apply(runtime_dir, events)
    except Exception:  # noqa: BLE001
        logger.warning("todo events: timeline append failed for %s", runtime_dir, exc_info=True)


def _read_sessions(todos_path: Path) -> tuple[Optional[dict], dict]:
    """Return ``(document, deep-copied sessions map)``.

    The copy matters: the document is passed back to
    :func:`write_json_atomic_if_changed` as the comparison baseline, so
    it must not alias the map we are about to mutate — an aliased
    baseline compares equal to itself and would silently skip the write.

    A file that is missing, unparseable, or not a JSON object comes back
    as ``(None, {})``: the baseline says "there was nothing", so the next
    write always happens and repairs the file. That is the declared
    behaviour for a document with a single owner (syncplan §3) — there
    is no foreign key to preserve, so refusing to write would only leave
    the corruption in place.
    """
    current = read_json(todos_path)
    if not isinstance(current, dict):
        return None, {}
    raw = current.get("sessions")
    sessions = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    return current, sessions


def _find(sessions: dict, todo_id: str) -> Optional[tuple[str, dict, dict]]:
    """First ``(session_id, session_entry, todo)`` matching ``todo_id``.

    Ids are server-minted and globally unique across sessions (they were
    per-session decimals only while the removed sink co-wrote this file),
    so "first match" is now "the match".
    """
    for sid, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        for todo in entry.get("todos") or []:
            if isinstance(todo, dict) and todo.get("id") == todo_id:
                return str(sid), entry, todo
    return None


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_todo(
    todos_path: Path,
    *,
    runtime: str,
    content: str,
    description: Optional[str] = None,
    active_form: Optional[str] = None,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Append a new todo. Returns the created todo dict.

    ``todo_id`` is server-generated (UUID v4 hex prefix, 8 chars) so
    callers don't have to coordinate. Collisions are extremely
    unlikely; on the off chance, we'd 500 (caller retries).

    Emits ``TaskCreated`` (plus a ``TaskStatusChanged`` when the caller
    asked for a non-default initial status), so the timeline and the
    per-session counters see the todo whatever backend is active.
    """
    _validate_safe_key(runtime, "runtime")
    sid = session_id or PROJECT_SESSION
    _validate_safe_key(sid, "session_id")
    _validate_content_length(content, field="content", limit=1000)
    if description is not None:
        _validate_content_length(description, field="description", limit=4000)
    if active_form is not None:
        _validate_content_length(active_form, field="active_form", limit=1000)
    initial_status = status or _DEFAULT_STATUS
    _validate_status(initial_status)

    todo_id = uuid.uuid4().hex[:8]
    stamp = _now_iso()

    with locked(todos_path):
        current, sessions = _read_sessions(todos_path)
        entry = sessions.get(sid)
        if entry is None or not isinstance(entry, dict):
            entry = _empty_session_entry(runtime)
            sessions[sid] = entry
        # Keep runtime in sync — useful when a session_id is later
        # reused by a different runtime, the latest wins.
        entry["runtime"] = runtime
        todos = entry.setdefault("todos", [])
        if not isinstance(todos, list):
            todos = []
            entry["todos"] = todos

        # Ids are looked up across every session, so uniqueness has to
        # hold across every session too — including against tombstones,
        # which must never be shadowed by a live todo of the same id.
        if _find(sessions, todo_id) is not None:
            raise TodosStoreError(
                "scope_unavailable",
                f"todo id collision ({todo_id}); retry the call.",
            )

        new_todo = {
            "id": todo_id,
            "content": content,
            "status": initial_status,
            "description": description,
            "active_form": active_form,
            "created_at": stamp,
            "updated_at": stamp,
            "deleted_at": None,
            "deleted_by": None,
        }
        todos.append(new_todo)

        write_json_atomic_if_changed(
            todos_path, _document(sessions), previous=current,
        )

    events: list[Event] = [
        TaskCreated(
            ts=stamp,
            native_session_id=sid,
            runtime=runtime,
            task_id=todo_id,
            content=content,
            description=description,
            active_form=active_form,
        )
    ]
    if initial_status != _DEFAULT_STATUS:
        events.append(
            TaskStatusChanged(
                ts=stamp,
                native_session_id=sid,
                runtime=runtime,
                task_id=todo_id,
                status=initial_status,
            )
        )
    _emit(todos_path, events)
    return new_todo


def get_todo(
    todos_path: Path, todo_id: str, *, include_deleted: bool = False,
) -> Optional[tuple[str, dict]]:
    """Return ``(session_id, todo_dict)`` or ``None`` if no match.

    A tombstoned todo is *not* a match unless ``include_deleted`` — the
    caller asked for a todo, and a deleted one is history, not a todo.
    """
    _current, sessions = _read_sessions(todos_path)
    found = _find(sessions, todo_id)
    if found is None:
        return None
    sid, _entry, todo = found
    if is_deleted(todo) and not include_deleted:
        return None
    return sid, todo


def update_todo(
    todos_path: Path,
    todo_id: str,
    *,
    status: Optional[str] = None,
    content: Optional[str] = None,
    description: Optional[str] = None,
    active_form: Optional[str] = None,
) -> dict:
    """Update fields on an existing todo. Returns the updated dict.

    Raises ``TodosStoreError("todo_not_found", ...)`` if the id isn't
    present in any session, or if it names a tombstone — a deleted todo
    is not editable, which is what stops it coming back to life.

    A call that changes nothing writes nothing, stamps nothing and emits
    nothing: re-sending the status a todo already has is a no-op, not a
    fresh timeline line.
    """
    if status is not None:
        _validate_status(status)
    if content is not None:
        _validate_content_length(content, field="content", limit=1000)
    if description is not None:
        _validate_content_length(description, field="description", limit=4000)
    if active_form is not None:
        _validate_content_length(active_form, field="active_form", limit=1000)

    with locked(todos_path):
        current, sessions = _read_sessions(todos_path)
        found = _find(sessions, todo_id)
        if found is None or is_deleted(found[2]):
            raise TodosStoreError("todo_not_found", "Todo not found.")
        sid, entry, todo = found

        previous_status = todo.get("status")
        changes = {
            "status": status,
            "content": content,
            "description": description,
            "active_form": active_form,
        }
        changed = False
        for field, value in changes.items():
            if value is not None and todo.get(field) != value:
                todo[field] = value
                changed = True

        if not changed:
            return todo

        stamp = _now_iso()
        todo["updated_at"] = stamp
        write_json_atomic_if_changed(
            todos_path, _document(sessions), previous=current,
        )
        runtime = str(entry.get("runtime") or "")

    if status is not None and status != previous_status:
        _emit(todos_path, [
            TaskStatusChanged(
                ts=stamp,
                native_session_id=sid,
                runtime=runtime,
                task_id=todo_id,
                status=status,
            )
        ])
    return todo


def delete_todo(
    todos_path: Path, todo_id: str, *, deleted_by: Optional[str] = None,
) -> bool:
    """Soft-delete a todo. Returns ``True`` if this call tombstoned it,
    ``False`` if there was nothing to delete (idempotent — the route
    reports ``deleted: false`` rather than 404, and a second DELETE of
    the same id is a no-op, exactly as before).

    The record itself is never removed (syncplan §5.5): ``deleted_at``
    and ``deleted_by`` are set alongside the unchanged ``status``, so
    "we decided not to do this" (``cancelled``) and "this should not
    have existed" stay distinguishable forever.

    ``deleted_by`` carries the calling **runtime**, the same vocabulary
    ``create_todo`` already requires, and is validated against the same
    charset: it is persisted into ``todos.json``, which is a synced
    document, so it may not become a channel for arbitrary caller text.
    ``None`` when the caller did not say — attribution is optional, and
    an unattributed tombstone is still a tombstone.
    """
    if deleted_by is not None:
        _validate_safe_key(deleted_by, "runtime")
    with locked(todos_path):
        current, sessions = _read_sessions(todos_path)
        found = _find(sessions, todo_id)
        if found is None or is_deleted(found[2]):
            return False
        sid, _entry, todo = found

        stamp = _now_iso()
        todo["deleted_at"] = stamp
        todo["deleted_by"] = deleted_by
        todo["updated_at"] = stamp
        write_json_atomic_if_changed(
            todos_path, _document(sessions), previous=current,
        )

    # The counters describe todos that exist; a tombstone doesn't. The
    # timeline is append-only history and keeps the lines it already
    # wrote — the todo *was* added, and that stays true.
    runtime_dir = _derived_dir(todos_path)
    try:
        if runtime_dir is not None:
            sessions_augment.forget_task(
                runtime_dir,
                native_session_id=sid,
                task_id=todo_id,
                ts=stamp,
                legacy_root=todos_path.parent,
            )
    except Exception:  # noqa: BLE001 - derived view, never fails the write
        logger.warning("todo events: augment counters failed for %s", todos_path, exc_info=True)
    return True
