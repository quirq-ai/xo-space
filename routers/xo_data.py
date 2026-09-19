"""``/xo/sessions.json`` moved to ``modules/telemetry/routes.py`` (the
telemetry module serves the session telemetry payload; ``/xo/space.json``
and ``/xo/dashboard.json`` are the projects module's).

This module stays importable for the mount lists that still name it; its
router carries no routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/xo", tags=["xo-data"])
