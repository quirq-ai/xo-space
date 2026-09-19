"""The project-scope usage routes (``/api/xo-projects/{id}/usage/*``) moved to
``modules/telemetry/routes.py``; the records half (todos, workitems, claims,
peers, the GitHub mirror, presence, the project timeline) lives in
``modules/projects/routes.py``.

This module stays importable for the mount list that still names it; its
router carries no routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
