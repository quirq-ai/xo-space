"""``/api/telemetry/sources`` moved to ``modules/telemetry/routes.py``, which
the registry mounts behind the telemetry module's api switch.

This module stays importable for the mount lists that still name it; its
router carries no routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])
