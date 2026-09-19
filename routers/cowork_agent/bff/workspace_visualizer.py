"""Workspace-scope presence: ``GET /api/xo-projects/activity``.

The workspace usage routes (``/api/xo-projects/usage/*``) moved to
``modules/telemetry/routes.py``; the workitem rollup
(``/api/workspace/workitems``) and the Space timeline
(``/api/xo-projects/timeline``) live in ``modules/projects/routes.py``.
What stays here is the union of every project's open sessions, read from
the watcher's ``~/.quirq/cache/activity/workspace.json``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    OpenSession,
)
from services.cowork_agent import scopes

logger = logging.getLogger(__name__)

router = APIRouter()


# ── /api/xo-projects/activity ────────────────────────────────────────────────


@router.get(
    "/api/xo-projects/activity",
    response_model=ActivityResponse,
)
def workspace_activity() -> ActivityResponse:
    """Workspace-wide live presence: the union of every project's open sessions.

    Empty when no project has live presence yet. Each open-session
    row carries ``project_id`` so the UI can group by project.
    """
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    raw = workspace.read_activity() or {}
    open_sessions: list[OpenSession] = []
    for s in raw.get("open_sessions") or []:
        if not isinstance(s, dict):
            continue
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
                    project_id=s.get("project_id"),
                )
            )
        except (KeyError, ValueError):
            continue
    return ActivityResponse(
        project_id=None,
        updated_at=raw.get("updated_at"),
        open_sessions=open_sessions,
    )
