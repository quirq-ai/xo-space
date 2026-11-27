"""A job's schedule in plain words (design ch 12 §5.8: the ``create_job``
item shows "the exact schedule … in plain words"), and the line a run's
thread is rooted on ("Morning triage · 09:00", ch 11 §7.1).

Times read in the UTC offset the job's ``first_run_at`` was given in (the
scheduler keeps it as ``utc_offset``), so they read as the owner wrote them.
The scheduler's model is a fixed interval from an anchor. An agent job may
instead carry a calendar rule in a named zone (``utils/schedule_rule``) as
its definition's ``schedule``: ``split`` takes it apart from what the
scheduler checks, and ``first_due`` says when it starts.

The owner's default zone, for a rule that names none, is a Space setting
(``settings/time.json``): the app saves the browser's zone there, since the
server runs on UTC and can't tell.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import settings_dir
from services.timestamps import parse_ts
from utils import schedule_rule
from utils.commands import scheduler
from utils.safe_read import read_text_guarded

log = logging.getLogger(__name__)

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
UTC = "+00:00"


#: The zone a rule takes when it names none and the owner saved none.
DEFAULT_TZ = "UTC"
_WRITE_LOCK = threading.Lock()


def _zone(offset: str) -> timezone | schedule_rule.ZoneInfo:
    """A fixed offset ("+05:30"), or a named zone ("Europe/London")."""
    if not offset.startswith(("+", "-")):
        return schedule_rule.zone(offset) or timezone.utc
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def local(stamp: str, offset: str) -> datetime:
    """A scheduler stamp (UTC) in the job's own offset or zone."""
    return parse_ts(stamp).astimezone(_zone(offset))


def zone_name(offset: str) -> str:
    if not offset.startswith(("+", "-")):
        return offset
    return "UTC" if offset == UTC else f"UTC{offset}"


def _count(n: int, unit: str) -> str:
    return unit if n == 1 else f"{n} {unit}s"


interval = schedule_rule.interval


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


def words(definition: Mapping[str, Any], every_seconds: int | None) -> str:
    """A job's schedule in plain words: its calendar rule's, else its interval's."""
    rule = definition.get("schedule")
    if rule and "every_seconds" not in rule:
        return schedule_rule.describe(rule)
    return describe(every_seconds, definition.get("first_run_at"), definition.get("utc_offset") or UTC)


def split(payload: Any) -> tuple[Any, dict[str, Any] | None]:
    """A job's definition without its ``schedule``, for the scheduler's
    check, and the rule, checked: a rule naming no ``tz`` takes the owner's
    default, and an interval rule is the definition's ``every_seconds``.
    Raises ValueError saying what is wrong."""
    if not isinstance(payload, Mapping) or "schedule" not in payload:
        return payload, None
    rest = {key: value for key, value in payload.items() if key != "schedule"}
    rule = payload["schedule"]
    if rule is None:
        return rest, None
    if isinstance(rule, Mapping) and "tz" not in rule:
        rule = {**rule, "tz": default_tz()}
    if problems := schedule_rule.validate(rule):
        raise ValueError(" ".join(problems).rstrip("."))
    if "every_seconds" in rule:
        if rest.get("every_seconds") not in (None, rule["every_seconds"]):
            raise ValueError("Give the interval once, in schedule or in every_seconds")
        rest["every_seconds"] = rule["every_seconds"]
    elif rest.get("every_seconds") is not None or rest.get("first_run_at"):
        raise ValueError("A calendar schedule says when it runs by itself: leave every_seconds and first_run_at out")
    return rest, dict(rule)


def definition(payload: Any) -> dict[str, Any]:
    """An agent job's checked definition (``scheduler.validate_definition``),
    with its checked calendar rule as ``schedule`` when it has one. Raises
    ValueError saying what is wrong."""
    rest, rule = split(payload)
    checked = scheduler.validate_definition(rest)
    return {**checked, "schedule": rule} if rule is not None else checked


def first_due(rule: Mapping[str, Any], now: datetime) -> datetime | None:
    """A calendar rule's first time at or after ``now``."""
    return schedule_rule.next_after(rule, now - timedelta(seconds=1))


def _setting() -> Path:
    return settings_dir() / "time.json"


def default_tz() -> str:
    """The owner's time zone: the one the app saved, else UTC. A file that
    can't be read gives UTC (the doctor names it), so jobs still schedule."""
    try:
        document = json.loads(read_text_guarded(_setting()))
    except FileNotFoundError:
        return DEFAULT_TZ
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("schedules: the time-zone setting can't be read (%s); using %s", type(exc).__name__, DEFAULT_TZ)
        return DEFAULT_TZ
    tz = document.get("tz") if isinstance(document, dict) and document.get("schema") == 1 else None
    if schedule_rule.zone(tz) is None:
        log.warning("schedules: the time-zone setting holds no zone this machine resolves; using %s", DEFAULT_TZ)
        return DEFAULT_TZ
    return tz


def set_default_tz(tz: Any) -> str:
    """Save the owner's time zone, an IANA name; jobs already made keep their own.
    A well-formed name this machine can't resolve is saved as given and reads
    as UTC until it can (``default_tz``)."""
    if not schedule_rule.well_formed(tz):
        raise ServiceError("invalid_time_zone", f"A time zone is an IANA name, such as Europe/London; {tz!r} isn't.",
                           422)
    try:
        path = _setting()
        with _WRITE_LOCK, locked(path):
            write_json_atomic(path, {"schema": 1, "tz": tz})
    except OSError as exc:
        raise ServiceError("time_zone_save_failed", "The time zone could not be saved. Check the Space's storage "
                                                    "and try again.", 503) from exc
    return tz
