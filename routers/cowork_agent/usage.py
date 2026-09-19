"""``/api/usage/*`` moved to ``modules/telemetry/routes.py``, which the
registry mounts behind the telemetry module's api switch.

This module stays importable for the mount list that still names it (its
router carries no routes) and keeps exporting the handler functions, so
``routers/cowork_agent/legacy/openclaw_usage.py`` can bind the legacy
``/openclaw/usage/*`` paths to the same functions as before.
"""

from __future__ import annotations

from fastapi import APIRouter

from modules.telemetry.routes import (  # noqa: F401  (re-exported for the legacy alias router)
    usage_analytics,
    usage_dashboard,
    usage_session,
    usage_sessions,
    usage_summary,
    usage_summary_card,
)

router = APIRouter(tags=["usage"])
