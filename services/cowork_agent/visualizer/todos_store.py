"""CRUD over ``<project>/.xo/todos.json`` — and the todo event source."""

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

#: On-disk revision of ``todos.json`` (syncplan §5.5).
TODOS_SCHEMA = 2

#: Value written into the document's ``$schema`` key. It is the schema's own
#: ``$id`` (visualizer/schema/todos.schema.json), NOT a filesystem path.
_SCHEMA_REF = "xo/todos.schema.json"

# The status vocabulary lives in one place — todo_status.py.
VALID_STATUSES = VALID_TODO_STATUSES

_DEFAULT_STATUS = "pending"

# Sanitisation regex for runtime / session_id.
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
    """Whether a todo record carries a tombstone."""
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
# The API is the event source (syncplan T7).


def _derived_dir(todos_path: Path) -> Optional[Path]:
    """Runtime directory holding the watcher's derived views for this project."""
    return project_layout.runtime_dir_for_project(todos_path.parent.parent.name)


def _emit(todos_path: Path, events: Iterable[Event]) -> None:
    """Fan todo lifecycle events to the sinks that render them."""
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
    """Return ``(document, deep-copied sessions map)``."""
    current = read_json(todos_path)
    if not isinstance(current, dict):
        return None, {}
    raw = current.get("sessions")
    sessions = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    return current, sessions


def _find(sessions: dict, todo_id: str) -> Optional[tuple[str, dict, dict]]:
    """First ``(session_id, session_entry, todo)`` matching ``todo_id``."""
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
    """Append a new todo. Returns the created todo dict."""
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

        # Ids are looked up across every session, so uniqueness has to hold
        # across every session too — including against tombstones, which must
        # never be shadowed by a live todo of the same id.
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
    """Return ``(session_id, todo_dict)`` or ``None`` if no match."""
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
    """Update fields on an existing todo. Returns the updated dict."""
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
    """Soft-delete a todo."""
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

    # The counters describe todos that exist; a tombstone doesn't.
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
