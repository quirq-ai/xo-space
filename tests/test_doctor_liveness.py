"""Liveness: the task record and what each component leaves on disk."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from services.timestamps import iso
from tests.doctor_sandbox import DoctorSandbox


class LivenessSandbox(DoctorSandbox):
    def record(self, name: str, **fields) -> dict:
        base = {"name": name, "started_at": self.now - 3600, "finishes_by_design": False, "state": "running",
                "ended_at": None, "error": None, "ticks": 100, "last_tick_started_at": self.now - 1,
                "last_tick_ok_at": self.now - 1, "consecutive_failures": 0, "last_failure": None,
                "last_failure_at": None}
        base.update(fields)
        return base

    @contextmanager
    def tasks(self, *records: dict):
        with patch("services.doctor.context._components_snapshot",
                   return_value={r["name"]: r for r in records}):
            yield

    def beat(self, age: float) -> None:
        stamp = iso(datetime.fromtimestamp(self.now - age, timezone.utc))
        (self.state / "cache" / "heartbeat.json").write_text(
            json.dumps({"schema": 1, "last_tick_at": stamp}), encoding="utf-8")

    def of(self, prefix: str) -> list[dict]:
        return [f for f in self.problems() if f["id"].startswith(prefix)]


class WatcherTests(LivenessSandbox):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def test_no_task_record_means_no_component_findings(self) -> None:
        self.beat(1)
        self.assertEqual(self.of("watcher.") + self.of("component."), [])

    def test_a_crashed_watcher_says_when_and_with_what(self) -> None:
        # #188 issue 3: it used to say only "last ticked 3 minutes ago", as a WARN.
        self.beat(180)
        with self.tasks(self.record("watcher", state="crashed", ended_at=self.now - 180,
                                    error="RuntimeError: watcher crashed")):
            [finding] = self.of("watcher.")
        self.assertEqual((finding["id"], finding["level"]), ("watcher.stopped", "FAIL"))
        self.assertIn("RuntimeError: watcher crashed", finding["observed"])
        self.assertIn("3 minutes ago", finding["observed"])
        self.assertIn("Restart the server", finding["next_step"])
        self.assertIn("scheduled commands", finding["consequence"])

    def test_a_crashed_watcher_with_a_fresh_heartbeat_names_another_server(self) -> None:
        self.beat(1)
        with self.tasks(self.record("watcher", state="crashed", ended_at=self.now - 60, error="RuntimeError: x")):
            [finding] = self.of("watcher.")
        self.assertIn("another xo-space server", " ".join(e["value"] for e in finding["evidence"]))

    def test_a_stale_heartbeat_escalates_after_five_minutes(self) -> None:
        for age, level in ((12, "WARN"), (400, "FAIL")):
            with self.subTest(age=age):
                self.beat(age)
                [finding] = self.of("watcher.heartbeat")
                self.assertEqual(finding["level"], level)
                self.assertIn("last ticked", finding["observed"])

    def test_a_running_task_with_a_stale_heartbeat_is_stuck(self) -> None:
        self.beat(60)
        with self.tasks(self.record("watcher", last_failure="ValueError: bad stats")):
            [finding] = self.of("watcher.heartbeat")
        self.assertEqual(finding["title"], "The watcher is stuck")
        self.assertIn("ValueError: bad stats", " ".join(e["value"] for e in finding["evidence"]))

    def test_failing_work_under_a_fresh_heartbeat_is_reported(self) -> None:
        self.beat(1)
        for failures, level in ((4, None), (5, "WARN"), (60, "FAIL")):
            with self.subTest(failures=failures), self.tasks(self.record(
                    "watcher", consecutive_failures=failures, last_failure="1 step(s) failed; first: x")):
                found = self.of("watcher.failing")
                self.assertEqual([f["level"] for f in found], [] if level is None else [level])


class ComponentTests(LivenessSandbox):
    def test_a_crashed_poller_fails(self) -> None:
        with self.tasks(self.record("github poller", state="crashed", ended_at=self.now - 30,
                                    error="RuntimeError: github poller crashed")):
            [finding] = self.of("component.")
        self.assertEqual((finding["id"], finding["level"], finding["subject"]),
                         ("component.crashed", "FAIL", "github poller"))
        self.assertIn("GitHub issue copies", finding["consequence"])

    def test_a_task_that_finishes_by_design_is_fine(self) -> None:
        with self.tasks(self.record("gateway reconcile", state="returned", ended_at=self.now - 5,
                                    finishes_by_design=True)):
            self.assertEqual(self.of("component."), [])

    def test_a_task_that_returns_unexpectedly_warns(self) -> None:
        with self.tasks(self.record("relay poller", state="returned", ended_at=self.now - 5)):
            [finding] = self.of("component.")
        self.assertEqual((finding["id"], finding["level"]), ("component.exited", "WARN"))

    def test_a_poller_whose_passes_keep_failing_warns(self) -> None:
        with self.tasks(self.record("connections poller", consecutive_failures=3,
                                    last_failure="TimeoutError")):
            [finding] = self.of("component.failing")
        self.assertIn("TimeoutError", " ".join(e["value"] for e in finding["evidence"]))

    def test_a_cancelled_task_is_shutdown_not_a_problem(self) -> None:
        with self.tasks(self.record("usage sync", state="cancelled", ended_at=self.now - 5)):
            self.assertEqual(self.of("component."), [])
