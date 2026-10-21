"""A job's schedule in plain words (design ch 12 §5.8: the ``create_job``
item shows "the exact schedule … in plain words"), and the line a run's
thread is rooted on ("Morning triage · 09:00", ch 11 §7.1).

Times read in the UTC offset the job's ``first_run_at`` was given in (the
scheduler keeps it as ``utc_offset``), so they read as the owner wrote them.
The scheduler's model is a fixed interval from an anchor; nothing here is a
calendar rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.timestamps import parse_ts

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
UTC = "+00:00"


def _zone(offset: str) -> timezone:
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def local(stamp: str, offset: str) -> datetime:
    """A scheduler stamp (UTC) in the job's own offset."""
    return parse_ts(stamp).astimezone(_zone(offset))


def zone_name(offset: str) -> str:
    return "UTC" if offset == UTC else f"UTC{offset}"


def _count(n: int, unit: str) -> str:
    return unit if n == 1 else f"{n} {unit}s"


def interval(seconds: int) -> str:
    """"2 hours", "30 minutes", "day", "90 seconds": the largest unit that divides it."""
    for size, unit in ((WEEK, "week"), (DAY, "day"), (HOUR, "hour"), (MINUTE, "minute")):
        if seconds % size == 0:
            return _count(seconds // size, unit)
    return _count(seconds, "second")


def describe(every_seconds: int | None, first_run_at: str | None, offset: str = UTC) -> str:
    """"Every day at 09:00 UTC+05:30, from Thu 22 Oct 2026"; "Once, on Thu 22
    Oct 2026 at 09:00 UTC"."""
    at = local(first_run_at, offset) if first_run_at else None
    if every_seconds is None:
        if at is None:
            return "Only when run by hand"
        return f"Once, on {at:%a %d %b %Y} at {at:%H:%M} {zone_name(offset)}"
    if at is None:
        return f"Every {interval(every_seconds)}, from one interval after it was saved"
    when = f"at {at:%H:%M} {zone_name(offset)}, from {at:%a %d %b %Y}"
    if every_seconds % WEEK == 0:
        return f"Every {_count(every_seconds // WEEK, 'week')} on {at:%A} {when}"
    if every_seconds % DAY == 0:
        return f"Every {_count(every_seconds // DAY, 'day')} {when}"
    return f"Every {interval(every_seconds)}, from {at:%a %d %b %Y} at {at:%H:%M} {zone_name(offset)}"


def fire_label(title: str, fire_at: str, offset: str = UTC) -> str:
    """The text of a run's root message: the job's title and the fire's time."""
    return f"{title} · {local(fire_at, offset):%H:%M}"
