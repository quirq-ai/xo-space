"""The query helpers the project routers share, and the old home of the
usage transforms.

The pure ``stats.json`` to wire-shape functions moved to
``modules/telemetry/presenter.py`` with the usage routes; they are
re-exported here under their old names for one release. What is defined
here is what ``modules/projects/routes.py`` still reads: the 400 for a
malformed query and the timeline ``?types=`` filter.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from modules.telemetry.presenter import (  # noqa: F401  (re-exported for one release)
    avg_latency_ms_from_by_day,
    by_day_from_stats,
    cost_and_tokens_for_dates,
    date_from_ms,
    messages_for_dates,
    model_call_counts_from_by_day,
    model_usage_entries,
    model_usage_with_totals,
    performance_entry_for_day,
    performance_for_dates,
    provider_for_model,
    rolling_key_for,
    row_total_tokens,
    tokens_from_stats,
    tool_usage_from_stats,
    zero_filled_dates,
)
from modules.timeline import service as _timeline_service


def bad_query(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "invalid_query", "message": message},
    )


# ── timeline event-type filter ─────────────────────────────────────────────────


def _declared_timeline_types() -> frozenset[str]:
    """Every event type the timeline module declares: the schema's ``type``
    enum (the legacy types) plus every module's ``events.TYPES``."""
    return frozenset(_timeline_service.declared_types())


TIMELINE_TYPES = _declared_timeline_types()


def parse_types_param(types: Optional[str]) -> Optional[frozenset]:
    if types is None:
        return None
    requested = {t.strip() for t in types.split(",") if t.strip()}
    unknown = requested - TIMELINE_TYPES
    if unknown:
        raise bad_query(f"unknown timeline type(s): {sorted(unknown)}")
    return frozenset(requested)
