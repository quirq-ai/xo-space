"""One live coordinator per chat: find it, or start its first incarnation.

A coordinator is a session record with role ``workspace-coordinator`` (the
workspace chat) or ``project-coordinator`` (a project's chat). Its cwd
is the projects root or the project folder; it never writes there (its
tools are comms tools and read-only readers). Recycling an incarnation for a
fresh one with a handoff comes later (M4); for now the first live one is used.
"""

from __future__ import annotations

import threading
from typing import Optional

from services.cowork_agent import project_layout
from services.projects_chat import store

_lock = threading.Lock()


def role_for(project: Optional[str]) -> str:
    return "workspace-coordinator" if not project else "project-coordinator"


def cwd_for(project: Optional[str]) -> str:
    root = project_layout.xo_projects_root()
    return str(root if not project else project_layout.project_dir(project))


def live(project: Optional[str]) -> Optional[dict]:
    """The chat's live coordinator session, or ``None``."""
    sessions = [s for s in store.list_sessions(project, role=role_for(project)) if s.get("retired_at") is None]
    sessions.sort(key=lambda s: s.get("created_at") or "")
    return sessions[-1] if sessions else None


def ensure(project: Optional[str]) -> dict:
    """The chat's live coordinator, creating incarnation 1 if there is none."""
    with _lock:
        found = live(project)
        if found is not None:
            return found
        store.ensure_chat(project)
        previous = [s.get("incarnation") or 0 for s in store.list_sessions(project, role=role_for(project))]
        record = store.create_session(project, role=role_for(project), cwd=cwd_for(project),
                                      incarnation=max(previous, default=0) + 1)
        record.pop("token", None)
        return record
