"""Read-only, bounded Space operations shared by MCP and the command-line API."""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Annotated, Literal

import anyio
from pydantic import Field

from services.errors import ServiceError
from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import todos_store
from services.inbox import store as inbox_store
from services.storage.reader import read_json

DOCUMENT_BYTES = 64 * 1024
_JSON_BYTES = 8 * 1024 * 1024
ProjectDocument = Literal[
    "README.md", "PROJECT.md", "OBJECTIVES.md", "PLAN.md", "PROGRESS.md", "AGENTS.md"
]
PageLimit = Annotated[int, Field(ge=1, le=100, strict=True)]
Offset = Annotated[int, Field(ge=0, strict=True)]
InboxStatus = Literal["open", "done", "all"]

TOOL_DESCRIPTIONS = [
    {"name": "space_list_projects", "description": "List Space projects with names and descriptions."},
    {"name": "space_read_project_document", "description": "Read a project's planning document (up to 64 KiB)."},
    {"name": "space_list_todos", "description": "List a project's todos, excluding deleted items."},
    {"name": "space_list_inbox", "description": "Read saved Space inbox items without refreshing connections."},
]


def _safe_project_id(value: str) -> None:
    if (
        not value or len(value) > 200 or value.startswith(".")
        or any(char in value for char in ("/", "\\"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Use a project_id returned by space_list_projects.")


def _safe_path(root: Path, *parts: str) -> Path:
    """Reject links even within a project: an allowed document cannot alias secrets."""
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symbolic links are not available through Space tools.")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("The requested file is outside its Space data directory.")
    return path


def _project_path(project_id: str) -> Path:
    _safe_project_id(project_id)
    root = project_layout.xo_projects_root(create=False)
    path = _safe_path(root, project_id)
    metadata = _safe_path(path, ".xo", "project.json")
    if not path.is_dir() or not metadata.is_file():
        raise ValueError("Project not found. Use space_list_projects to find available projects.")
    return path


def _text(value: object, limit: int = 4000) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _check_json_size(path: Path) -> None:
    if path.is_file() and path.stat().st_size > _JSON_BYTES:
        raise ValueError("This Space data file is too large to read through Space tools.")


def list_projects(limit: int, offset: int) -> dict:
    root = project_layout.xo_projects_root(create=False)
    projects = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            project = _project_path(entry.name)
            metadata_path = _safe_path(project, ".xo", "project.json")
            _check_json_size(metadata_path)
        except ValueError:
            continue
        metadata = read_json(metadata_path)
        metadata = metadata if isinstance(metadata, dict) else {}
        display_name = _text(metadata.get("display_name"), 300)
        if not display_name or (
            metadata.get("name") != entry.name and display_name == metadata.get("name")
        ):
            display_name = entry.name
        projects.append({
            "project_id": entry.name,
            "display_name": display_name,
            "description": _text(metadata.get("description")),
        })
    return {
        "projects": projects[offset:offset + limit], "total": len(projects),
        "offset": offset, "limit": limit, "has_more": offset + limit < len(projects),
    }


def read_project_document(project_id: str, document: ProjectDocument) -> dict:
    project = _project_path(project_id)
    path = _safe_path(project, document)
    if not path.is_file():
        raise ValueError("That document is not available in this project.")
    with path.open("rb") as stream:
        raw = stream.read(DOCUMENT_BYTES + 1)
    truncated = len(raw) > DOCUMENT_BYTES
    # A byte limit may end halfway through a UTF-8 character; omit that fragment.
    content = codecs.getincrementaldecoder("utf-8")().decode(
        raw[:DOCUMENT_BYTES], final=not truncated,
    )
    return {
        "project_id": project_id, "document": document,
        "content": content, "truncated": truncated,
    }


def list_todos(project_id: str, limit: int) -> dict:
    project = _project_path(project_id)
    path = _safe_path(project, ".xo", "todos.json")
    _check_json_size(path)
    raw = read_json(path)
    if path.is_file() and not isinstance(raw, dict):
        raise ValueError("The project's todos are not readable. Repair todos.json in Space.")
    raw = raw if isinstance(raw, dict) else {}
    sessions = raw.get("sessions")
    todos = []
    for session_id, session in (sessions.items() if isinstance(sessions, dict) else []):
        if not isinstance(session, dict) or not isinstance(session.get("todos"), list):
            continue
        for todo in session["todos"]:
            if not isinstance(todo, dict) or todos_store.is_deleted(todo):
                continue
            todos.append({
                "id": _text(todo.get("id"), 200),
                "session_id": _text(session_id, 200),
                "content": _text(todo.get("content"), 1000),
                "description": _text(todo.get("description")),
                "status": _text(todo.get("status"), 40),
                "created_at": _text(todo.get("created_at"), 100),
                "updated_at": _text(todo.get("updated_at"), 100),
            })
    return {
        "project_id": project_id, "todos": todos[:limit], "total": len(todos),
        "has_more": len(todos) > limit, "updated_at": _text(raw.get("updated_at"), 100),
    }


def list_inbox(status: InboxStatus, limit: int) -> dict:
    path = inbox_store.inbox_path()
    path = _safe_path(path.parent.parent, path.parent.name, path.name)
    _check_json_size(path)
    # Supplying the path bypasses the store's legacy migration; reading Space
    # tools must not move files, ingest feeds, or create lock directories.
    document, ok = inbox_store.load_document(path=path)
    if not ok:
        raise ValueError("The saved inbox is not readable. Repair inbox.json in Space.")
    wanted = {"open": ("new", "seen"), "done": ("done",), "all": inbox_store.STATUSES}[status]
    counts = {value: 0 for value in inbox_store.STATUSES}
    items = []
    for item in document["items"]:
        counts[item["status"]] += 1
        if item["status"] in wanted:
            # Store records preserve unknown keys; expose only the public data.
            items.append({key: item.get(key) for key in (
                "id", "title", "body", "status", "source", "kind", "ts", "project_id", "url",
            )})
    return {
        "items": items[:limit], "counts": counts, "total": len(items),
        "has_more": len(items) > limit, "updated_at": _text(document.get("updated_at"), 100),
    }


async def read(operation, *args) -> dict:
    """Run bounded file reads off the event loop and surface safe service errors."""
    try:
        return await anyio.to_thread.run_sync(operation, *args)
    except (OSError, UnicodeError):
        raise ServiceError("space_data_unavailable", "Space could not read that data. Check its files and permissions locally.", 503) from None
    except ValueError as exc:
        raise ServiceError("invalid_space_request", str(exc), 400) from None
