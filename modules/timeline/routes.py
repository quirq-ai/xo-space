"""``GET /api/timeline``: what happened, newest first.

  GET /api/timeline?limit=200&before=&types=&project_id=
      {"events": [...], "count": n}

``limit`` is clamped to 1..500; ``before`` is an ISO-8601 cursor (the
``ts`` of the last event served); ``types`` is a comma-separated allowlist
(an undeclared type is 400 ``invalid_value``, as ``?types=`` always was);
``project_id`` narrows the read to one project's log (unknown: 404
``project_not_found``) and without it the read is the merged Space view.
Thin over ``modules.timeline.service``; every typed failure reaches the
wire through the app's service error handler.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from . import service

router = APIRouter()


@router.get("/api/timeline")
def read_timeline(limit: int = Query(service.LIMIT_DEFAULT),
                  before: Optional[str] = Query(None),
                  types: Optional[str] = Query(None),
                  project_id: Optional[str] = Query(None)) -> dict:
    events = service.read(limit=service.clamp_limit(limit), before=before or None,
                          types=service.check_types(types), project_id=project_id or None)
    return {"events": events, "count": len(events)}
