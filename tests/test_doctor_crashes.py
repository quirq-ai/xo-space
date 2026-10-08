"""The crashes check reads back what services/health recorded.

Records are made with the real recorder (at a controlled time), so the
doctor is tested against exactly what the server writes.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from services.health import recorder
from tests.doctor_sandbox import DoctorSandbox

HOUR, DAY = 3600, 86400


def _boom(message: str = "poller blew up") -> BaseException:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


class CrashSandbox(DoctorSandbox):
    def setUp(self) -> None:
        super().setUp()
        recorder._reset_for_tests(enable_recording=True)
        self.addCleanup(recorder._reset_for_tests)

    def at(self, ago: float, *args, **kwargs) -> None:
        """Record one failure as if it happened ``ago`` seconds before the report."""
        recorder._last_write.clear()  # each call reaches the disk at once
        with patch.object(recorder.time, "time", return_value=self.now - ago):
            recorder.record(*args, **kwargs)
            recorder.flush()

    def crashes(self, report=None) -> list[dict]:
        report = report or self.report()
        return [f for c in report["checks"] if c["id"] == "crashes" for f in c["findings"]]

    def evidence(self, finding: dict) -> dict[str, list[str]]:
        rows: dict[str, list[str]] = {}
        for row in finding["evidence"]:
            rows.setdefault(row["label"], []).append(row["value"])
        return rows


class CrashTests(CrashSandbox):
    def test_the_samples_old_record_is_not_reported(self) -> None:
        self.assertEqual(self.crashes(), [])

    def test_a_crash_today_is_a_fail_with_its_evidence(self) -> None:
        recorder.set_boot_id("bootabc123")
        boots = self.state / "setup" / "health" / "boots"
        boots.mkdir(parents=True, exist_ok=True)
        (boots / "bootabc123.json").write_text(json.dumps({"schema": 1, "boot_id": "bootabc123", "version": "v9.9.9"}))
        self.at(2 * HOUR, "connections poller", recorder.CRASH, exc=_boom())
        [finding] = self.crashes()
        self.assertEqual((finding["id"], finding["level"], finding["subject"]),
                         ("crash.recent", "FAIL", "connections poller"))
        self.assertEqual(finding["title"], "The connections poller crashed")
        rows = self.evidence(finding)
        self.assertEqual(rows["Error"], ["RuntimeError: poller blew up"])
        self.assertEqual(rows["Where"][0], f"tests/test_doctor_crashes.py:{_boom.__code__.co_firstlineno + 2} in _boom")
        self.assertEqual(rows["Version"], ["v9.9.9"])
        self.assertTrue(finding["problem_key"].startswith("event:"))

    def test_three_crashes_in_a_day_keep_crashing(self) -> None:
        for ago in (5 * HOUR, 3 * HOUR, 1 * HOUR):
            self.at(ago, "github poller", recorder.CRASH, exc=_boom())
        [finding] = self.crashes()
        self.assertEqual((finding["id"], finding["level"], finding["title"]),
                         ("crash.repeating", "FAIL", "The GitHub poller keeps crashing"))
        self.assertEqual(self.evidence(finding)["Happened"], ["3 time(s)"])

    def test_an_older_crash_is_a_warning_and_a_week_old_one_is_gone(self) -> None:
        self.at(3 * DAY, "usage sync", recorder.CRASH, exc=_boom())
        [finding] = self.crashes()
        self.assertEqual(finding["level"], "WARN")
        later = self.report(now=self.now + 5 * DAY)
        self.assertNotIn("usage sync", [f["subject"] for f in self.crashes(later)])

    def test_an_unclean_exit_and_a_repeated_one(self) -> None:
        self.at(2 * DAY, "server", recorder.UNCLEAN_EXIT, error_type="UncleanExit",
                message="The previous run ended without shutting down.",
                details={"ended_around": "2026-01-01T09:29:59Z"})
        [finding] = self.crashes()
        self.assertEqual((finding["id"], finding["level"], finding["title"]),
                         ("crash.unclean_exit", "WARN", "The server stopped without shutting down"))
        self.assertIn("Ended around", self.evidence(finding))
        self.at(1 * DAY, "server", recorder.UNCLEAN_EXIT, error_type="UncleanExit", message="again")
        [finding] = self.crashes()
        self.assertEqual(finding["level"], "FAIL")

    def test_a_request_that_failed_names_its_route(self) -> None:
        self.at(HOUR, "http", recorder.HTTP_500, exc=_boom("x"), subject="GET /api/xo-projects/{project_id}/todos")
        [finding] = self.crashes()
        self.assertEqual(finding["id"], "crash.http_500")
        self.assertIn("/api/xo-projects/{project_id}/todos", finding["title"])

    def test_newest_first(self) -> None:
        self.at(3 * DAY, "usage sync", recorder.CRASH, exc=_boom())
        self.at(HOUR, "http", recorder.HTTP_500, exc=_boom("x"), subject="GET /x")
        self.assertEqual([f["id"] for f in self.crashes()], ["crash.http_500", "crash.recent"])

    def test_a_damaged_record_is_the_read_checks_and_not_this_ones(self) -> None:
        self.at(HOUR, "usage sync", recorder.CRASH, exc=_boom())
        [path] = [p for p in (self.state / "setup" / "health" / "events").glob("*.json")
                  if json.loads(p.read_text())["component"] == "usage sync"]
        path.write_text("{")
        os.utime(path, (self.now - DAY, self.now - DAY))
        report = self.report()
        self.assertEqual(self.crashes(report), [])
        [damaged] = [f for c in report["checks"] for f in c["findings"] if f["subject"] == f"setup/health/events/{path.name}"]
        self.assertEqual((damaged["id"], damaged["level"]), ("read.invalid_json", "WARN"))


class HistoryFoldingTests(CrashSandbox):
    """One problem reads as one entry: a record goes under today's finding."""

    def test_a_store_refusal_goes_under_the_damaged_file(self) -> None:
        todos = self.projects / "sample-project" / ".xo" / "todos.json"
        document = json.loads(todos.read_text())
        document["sessions"] = []
        todos.write_text(json.dumps(document))
        self.at(HOUR, "todos", recorder.REFUSAL, error_type="corrupt_document",
                message="sessions is a list, expected object", subject=str(todos))
        report = self.report()
        self.assertEqual(self.crashes(report), [])
        [content] = [f for c in report["checks"] for f in c["findings"] if f["id"] == "content.wrong_shape"]
        self.assertEqual([r["id"] for r in content["related"]], ["crash.refusal"])
        self.assertIn("History", [e["label"] for e in content["evidence"]])

    def test_once_the_file_is_repaired_the_refusal_stands_alone(self) -> None:
        todos = self.projects / "sample-project" / ".xo" / "todos.json"
        self.at(HOUR, "todos", recorder.REFUSAL, error_type="corrupt_document", message="m", subject=str(todos))
        [finding] = self.crashes()
        self.assertEqual((finding["id"], finding["level"], finding["title"]),
                         ("crash.refusal", "WARN", "The todo list refused a damaged file"))

    def test_a_crash_goes_under_the_component_that_is_down_now(self) -> None:
        self.at(HOUR, "connections poller", recorder.CRASH, exc=_boom())
        record = {"name": "connections poller", "state": "crashed", "started_at": self.now - 2 * HOUR,
                  "ended_at": self.now - HOUR, "error": "RuntimeError: poller blew up", "consecutive_failures": 0}
        with patch("services.doctor.context._components_snapshot", return_value={"connections poller": record}):
            report = self.report()
        self.assertEqual(self.crashes(report), [])
        [down] = [f for c in report["checks"] for f in c["findings"] if f["id"] == "component.crashed"]
        self.assertEqual([r["id"] for r in down["related"]], ["crash.recent"])


if __name__ == "__main__":
    import unittest
    unittest.main()
