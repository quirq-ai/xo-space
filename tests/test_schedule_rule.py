"""Calendar rules in a named time zone (``utils/schedule_rule``): when they
run, across the clock changes, and how they read."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from utils import schedule_rule as r

LONDON_WEEKDAYS = {"days": "weekdays", "time": "09:00", "tz": "Europe/London"}


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class NextTests(unittest.TestCase):
    def test_a_weekday_rule_keeps_its_wall_time_when_the_clocks_change(self) -> None:
        # The clocks went back on Sun 25 Oct 2026: 09:00 London is 08:00Z before, 09:00Z after.
        self.assertEqual(r.upcoming(LONDON_WEEKDAYS, utc(2026, 10, 22), 4),
                         [utc(2026, 10, 22, 8), utc(2026, 10, 23, 8), utc(2026, 10, 26, 9), utc(2026, 10, 27, 9)])

    def test_next_after_is_strictly_after(self) -> None:
        self.assertEqual(r.next_after(LONDON_WEEKDAYS, utc(2026, 10, 22, 8)), utc(2026, 10, 23, 8))
        self.assertEqual(r.next_after(LONDON_WEEKDAYS, utc(2026, 10, 22, 7, 59, 59)), utc(2026, 10, 22, 8))

    def test_an_aware_time_in_another_zone_is_read_as_its_instant(self) -> None:
        from zoneinfo import ZoneInfo
        after = datetime(2026, 10, 22, 13, 29, tzinfo=ZoneInfo("Asia/Kolkata"))      # 07:59Z
        self.assertEqual(r.next_after(LONDON_WEEKDAYS, after), utc(2026, 10, 22, 8))

    def test_chosen_days(self) -> None:
        rule = {"days": ["fri", "mon"], "time": "19:00", "tz": "UTC"}
        self.assertEqual(r.upcoming(rule, utc(2026, 11, 27, 19), 3),       # a Friday, 19:00 itself excluded
                         [utc(2026, 11, 30, 19), utc(2026, 12, 4, 19), utc(2026, 12, 7, 19)])

    def test_a_time_in_the_spring_gap_runs_at_the_next_valid_minute(self) -> None:
        # 01:30 doesn't exist in London on Sun 28 Mar 2027: it runs at 02:00 BST (01:00Z).
        rule = {"days": ["sun"], "time": "01:30", "tz": "Europe/London"}
        self.assertEqual(r.upcoming(rule, utc(2027, 3, 25), 2), [utc(2027, 3, 28, 1), utc(2027, 4, 4, 0, 30)])

    def test_a_time_in_the_autumn_overlap_runs_once(self) -> None:
        # 01:30 happens twice in London on Sun 25 Oct 2026 (00:30Z and 01:30Z): it runs at the first.
        rule = {"days": ["sun"], "time": "01:30", "tz": "Europe/London"}
        self.assertEqual(r.upcoming(rule, utc(2026, 10, 24), 2), [utc(2026, 10, 25, 0, 30), utc(2026, 11, 1, 1, 30)])

    def test_a_day_of_the_month_skips_months_without_it(self) -> None:
        rule = {"day_of_month": 31, "time": "18:00", "tz": "Asia/Kolkata"}
        self.assertEqual(r.upcoming(rule, utc(2026, 3, 1), 3),
                         [utc(2026, 3, 31, 12, 30), utc(2026, 5, 31, 12, 30), utc(2026, 7, 31, 12, 30)])

    def test_the_last_day_of_the_month(self) -> None:
        rule = {"day_of_month": "last", "time": "23:30", "tz": "America/New_York"}
        self.assertEqual(r.upcoming(rule, utc(2028, 1, 15), 3),          # 2028 is a leap year
                         [utc(2028, 2, 1, 4, 30), utc(2028, 3, 1, 4, 30), utc(2028, 4, 1, 3, 30)])

    def test_an_interval_counts_from_the_time_given(self) -> None:
        self.assertEqual(r.upcoming({"every_seconds": 3600}, utc(2026, 1, 1), 2),
                         [utc(2026, 1, 1, 1), utc(2026, 1, 1, 2)])


class DescribeTests(unittest.TestCase):
    def test_plain_words(self) -> None:
        for rule, words in (
                (LONDON_WEEKDAYS, "Every weekday at 09:00 (Europe/London)"),
                ({"days": list(r.DAYS), "time": "07:15", "tz": "UTC"}, "Every day at 07:15 (UTC)"),
                ({"days": ["fri", "mon", "wed"], "time": "19:00", "tz": "UTC"},
                 "Every Mon, Wed and Fri at 19:00 (UTC)"),
                ({"days": ["sun"], "time": "10:00", "tz": "UTC"}, "Every Sun at 10:00 (UTC)"),
                ({"day_of_month": 1, "time": "09:00", "tz": "UTC"}, "On the 1st of every month at 09:00 (UTC)"),
                ({"day_of_month": 22, "time": "09:00", "tz": "UTC"}, "On the 22nd of every month at 09:00 (UTC)"),
                ({"day_of_month": 31, "time": "09:00", "tz": "UTC"},
                 "On the 31st of every month at 09:00 (UTC) (months without a 31st are skipped)"),
                ({"day_of_month": "last", "time": "18:00", "tz": "Asia/Kolkata"},
                 "On the last day of every month at 18:00 (Asia/Kolkata)"),
                ({"every_seconds": 7200, "tz": "UTC"}, "Every 2 hours (UTC)"),
                ({"every_seconds": 86400}, "Every day")):
            with self.subTest(words=words):
                self.assertEqual(r.describe(rule), words)


class ValidateTests(unittest.TestCase):
    def test_rules_that_hold(self) -> None:
        for rule in (LONDON_WEEKDAYS, {"days": ["mon"], "time": "00:00", "tz": "UTC"},
                     {"day_of_month": "last", "time": "23:59", "tz": "Asia/Kolkata"},
                     {"every_seconds": 60}, {"every_seconds": 60, "tz": "Europe/Paris"}):
            with self.subTest(rule=rule):
                self.assertEqual(r.validate(rule), [])

    def test_each_problem_is_named(self) -> None:
        for rule, words in (
                ("weekdays", "A schedule is an object"),
                ({}, "exactly one of days, day_of_month or every_seconds"),
                ({"days": "weekdays", "every_seconds": 60}, "exactly one of"),
                ({**LONDON_WEEKDAYS, "at": "x"}, "not at"),
                ({**LONDON_WEEKDAYS, "days": ["mon", "mon"]}, 'days is "weekdays" or a list'),
                ({**LONDON_WEEKDAYS, "days": []}, 'days is "weekdays" or a list'),
                ({**LONDON_WEEKDAYS, "days": ["monday"]}, 'days is "weekdays" or a list'),
                ({"day_of_month": 0, "time": "09:00", "tz": "UTC"}, "day_of_month is a day from 1 to 31"),
                ({"day_of_month": True, "time": "09:00", "tz": "UTC"}, "day_of_month is a day from 1 to 31"),
                ({**LONDON_WEEKDAYS, "time": "9:00"}, "time is a 24-hour time"),
                ({**LONDON_WEEKDAYS, "time": "24:00"}, "time is a 24-hour time"),
                ({**LONDON_WEEKDAYS, "tz": "Mars/Olympus"}, "tz is an IANA time zone name"),
                ({**LONDON_WEEKDAYS, "tz": "../../etc/passwd"}, "tz is an IANA time zone name"),
                ({"days": "weekdays", "time": "09:00"}, "tz is an IANA time zone name"),
                ({"every_seconds": 0}, "every_seconds is a whole number"),
                ({"every_seconds": 60, "tz": "nope"}, "tz is an IANA time zone name")):
            with self.subTest(rule=rule):
                problems = r.validate(rule)
                self.assertTrue(any(words in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
