"""``python -m quirq projects <command>``

  list                        every project, newest first (the same as GET /api/xo-projects)
  todos <project_id>          the project's todos, by session, tombstones left out
  workitems <project_id>      the project's workitems, projected against its GitHub mirror
"""

from __future__ import annotations

from services.errors import ServiceError

from . import service


def list_(args: list[str]) -> dict:
    items = service.list_projects()
    return {"items": items, "total": len(items)}


def _project_id(args: list[str], command: str) -> str:
    if not args or not args[0].strip():
        raise ServiceError("missing_project_id", f"usage: quirq projects {command} <project_id>")
    return args[0].strip()


def todos(args: list[str]) -> dict:
    project_id = _project_id(args, "todos")
    raw = service.project_todos(project_id) or {}
    sessions: dict[str, dict] = {}
    for sid, entry in (raw.get("sessions") or {}).items():
        if not isinstance(entry, dict):
            continue
        rows = [t for t in entry.get("todos") or [] if isinstance(t, dict) and t.get("deleted_at") is None]
        sessions[str(sid)] = {"runtime": entry.get("runtime"), "todos": rows}
    return {"project_id": project_id, "updated_at": raw.get("updated_at"), "sessions": sessions}


def workitems(args: list[str]) -> dict:
    project_id = _project_id(args, "workitems")
    rows = service.project_workitems(project_id)
    return {"project_id": project_id, "workitems": rows, "count": len(rows)}


COMMANDS = {"list": list_, "todos": todos, "workitems": workitems}
