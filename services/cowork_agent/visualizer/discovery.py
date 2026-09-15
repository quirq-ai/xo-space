"""Shared session-row discovery for adapter visualizer sources."""

from __future__ import annotations

from typing import Iterator

from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def iter_sessionslist_rows(backend: str) -> Iterator[tuple[str, str, dict]]:
    """
    Yield ``(project_id, composite_key, row)`` for every adapter row in any
    project's session index whose ``backend`` field equals ``backend``.
    """
    for project_id in list_project_ids():
        for composite_key, row in session_index.read_session_index(project_id).items():
            if row.get("backend") != backend:
                continue
            yield project_id, composite_key, row
