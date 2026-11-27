"""When a job runs, as a calendar rule in a named time zone (standard library only).

A rule is a small JSON object, one of three shapes:

* ``{"days": ["mon", "wed"] | "weekdays", "time": "09:00", "tz": "Europe/London"}``;
* ``{"day_of_month": 1-31 | "last", "time": "18:00", "tz": "Asia/Kolkata"}``: a
  month without that day (the 31st in April) is skipped;
* ``{"every_seconds": 3600, "tz": "UTC"}``: a fixed interval, where ``tz`` only
  says how its times read.

``tz`` is an IANA zone name. A wall time keeps its wall time all year, so 09:00
stays 09:00 when the clocks change. A time that doesn't exist that day (in the
spring gap) runs at the next valid minute, and a time that happens twice (in
the autumn overlap) runs once, at its first occurrence.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = "weekdays"
LAST = "last"
#: How far ahead a rule is searched: a month-day rule finds its day within two months, a weekday within one week.
_HORIZON_DAYS = 400
#: How far past a nonexistent wall time the next valid minute is searched (no zone skips more than a day).
_GAP_MINUTES = 24 * 60
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_KEYS = {"days": {"days", "time", "tz"}, "day_of_month": {"day_of_month", "time", "tz"},
         "every_seconds": {"every_seconds", "tz"}}


def zone(name: Any) -> ZoneInfo | None:
    """The zone an IANA name names, or None."""
    if not isinstance(name, str) or not name or name.startswith(("/", ".")) or ".." in name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def validate(rule: Any) -> list[str]:
    """What is wrong with ``rule``, in plain words; empty when it is a rule."""
    if not isinstance(rule, Mapping):
        return ["A schedule is an object: {days, time, tz}, {day_of_month, time, tz} or {every_seconds, tz}."]
    shape = _shape(rule)
    if shape is None:
        return ["A schedule has exactly one of days, day_of_month or every_seconds."]
    problems = []
    if unknown := sorted(set(rule) - _KEYS[shape]):
        problems.append(f"A {shape} schedule takes only {', '.join(sorted(_KEYS[shape]))}; not {', '.join(unknown)}.")
    if shape == "every_seconds":
        seconds = rule["every_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            problems.append("every_seconds is a whole number of seconds, more than 0.")
        if "tz" in rule and zone(rule["tz"]) is None:
            problems.append(f"tz is an IANA time zone name, such as Europe/London; {rule['tz']!r} isn't.")
        return problems
    if shape == "days":
        days = rule["days"]
        if days != WEEKDAYS and (not isinstance(days, list) or not days or any(d not in DAYS for d in days)
                                 or len(set(days)) != len(days)):
            problems.append(f'days is "weekdays" or a list of different days among {", ".join(DAYS)}.')
    else:
        day = rule["day_of_month"]
        if day != LAST and (isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31):
            problems.append('day_of_month is a day from 1 to 31, or "last".')
    if not isinstance(rule.get("time"), str) or not _TIME.fullmatch(rule["time"]):
        problems.append("time is a 24-hour time, HH:MM, such as 09:00.")
    if zone(rule.get("tz")) is None:
        problems.append(f"tz is an IANA time zone name, such as Europe/London; {rule.get('tz')!r} isn't.")
    return problems


def next_after(rule: Mapping[str, Any], after: datetime) -> datetime | None:
    """The first time ``rule`` runs strictly after ``after`` (an aware time),
    in UTC; None if it never does."""
    after = after.astimezone(timezone.utc)
    if "every_seconds" in rule:
        return after + timedelta(seconds=rule["every_seconds"])
    tz = ZoneInfo(rule["tz"])
    wall = time.fromisoformat(rule["time"])
    start = after.astimezone(tz).date()
    for offset in range(_HORIZON_DAYS):
        day = start + timedelta(days=offset)
        if _matches(rule, day) and (at := _instant(day, wall, tz)) > after:
            return at
    return None


def upcoming(rule: Mapping[str, Any], after: datetime, n: int) -> list[datetime]:
    """The next ``n`` times ``rule`` runs after ``after``, in UTC."""
    times: list[datetime] = []
    while len(times) < n and (after := next_after(rule, after)) is not None:
        times.append(after)
    return times


def describe(rule: Mapping[str, Any]) -> str:
    """The rule in plain words: "Every weekday at 09:00 (Europe/London)"."""
    where = f" ({rule['tz']})" if rule.get("tz") else ""
    if "every_seconds" in rule:
        return f"Every {interval(rule['every_seconds'])}{where}"
    at = f"at {rule['time']}{where}"
    if "day_of_month" in rule:
        day = rule["day_of_month"]
        if day == LAST:
            return f"On the last day of every month {at}"
        skipped = f" (months without a {_ordinal(day)} are skipped)" if day > 28 else ""
        return f"On the {_ordinal(day)} of every month {at}{skipped}"
    days = rule["days"]
    if days == WEEKDAYS:
        return f"Every weekday {at}"
    if len(days) == len(DAYS):
        return f"Every day {at}"
    names = [calendar.day_abbr[DAYS.index(d)] for d in sorted(days, key=DAYS.index)]
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"Every {listed} {at}"


def interval(seconds: int) -> str:
    """"2 hours", "30 minutes", "day", "90 seconds": the largest unit that divides it."""
    for size, unit in ((604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            return _count(seconds // size, unit)
    return _count(seconds, "second")


def _count(n: int, unit: str) -> str:
    return unit if n == 1 else f"{n} {unit}s"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _shape(rule: Mapping[str, Any]) -> str | None:
    found = [key for key in _KEYS if key in rule]
    return found[0] if len(found) == 1 else None


def _matches(rule: Mapping[str, Any], day: date) -> bool:
    if "days" in rule:
        days = rule["days"]
        return day.weekday() < 5 if days == WEEKDAYS else DAYS[day.weekday()] in days
    wanted = rule["day_of_month"]
    if wanted == LAST:
        wanted = calendar.monthrange(day.year, day.month)[1]
    return day.day == wanted


def _instant(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """``wall`` on ``day`` in ``tz``, in UTC: its first occurrence when it
    happens twice, the next valid minute when it doesn't happen at all."""
    naive = datetime.combine(day, wall)
    for minute in range(_GAP_MINUTES):
        local = naive + timedelta(minutes=minute)
        at = local.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
        if at.astimezone(tz).replace(tzinfo=None) == local:
            return at
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)
