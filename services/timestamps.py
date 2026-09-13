"""Timestamp helpers shared by the Space packages.

Producers disagree on how they write time (``Z``, ``+00:00``, naive), so
the inbox and the connections store never compare strings: every comparison
goes through :func:`parse_ts`, and every stamp this code writes comes from
:func:`now_iso` or :func:`iso` (``YYYY-MM-DDTHH:MM:SSZ``, :data:`TS_FORMAT`).
:data:`EPOCH` is the sort key for a value that does not parse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value) -> Optional[datetime]:
    """ISO-8601 string (``Z``, offset or naive, treated as UTC) to an aware
    UTC datetime; ``None`` on anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def aware(now: datetime) -> datetime:
    """``now`` as an aware UTC datetime (a naive value is taken as UTC)."""
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """``dt`` in :data:`TS_FORMAT`, converted to UTC first."""
    return aware(dt).strftime(TS_FORMAT)
