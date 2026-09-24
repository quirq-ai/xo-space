"""Liveness: the task record and what each component leaves on disk."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from services.timestamps import iso
from tests.doctor_sandbox import PID, DoctorSandbox


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


def _stamp(ts: float) -> str:
    return iso(datetime.fromtimestamp(ts, timezone.utc))


class ConnectionsTests(LivenessSandbox):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": "true", "XO_CONNECTIONS_POLL_TICK_S": "30"})
        env.start()
        self.addCleanup(env.stop)
        # The live-gate filter needs Composio settings; the tests pin it to "error still live".
        live = patch("services.doctor.liveness._live_error", side_effect=lambda toolkit, stored: stored)
        live.start()
        self.addCleanup(live.stop)

    def write_state(self, **fields) -> None:
        path = self.state / "connections" / "gmail" / "state.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update(fields)
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_a_poller_that_stopped_is_reported_once_overdue(self) -> None:
        # #188 issue 1. Gmail polls every 900 s; tick 30 s; grace 300 s.
        self.write_state(last_poll_at=_stamp(self.now - 900), last_ok_at=_stamp(self.now - 900))
        self.assertEqual(self.of("connections."), [])
        self.write_state(last_poll_at=_stamp(self.now - 1300), last_ok_at=_stamp(self.now - 1300))
        [finding] = self.of("connections.overdue")
        self.assertEqual((finding["subject"], finding["level"]), ("gmail", "WARN"))
        self.assertEqual(finding["title"], "Gmail is no longer being checked")
        self.assertIn("Inbox", finding["consequence"])

    def test_a_future_last_poll_is_not_overdue(self) -> None:
        self.write_state(last_poll_at=_stamp(self.now + 3600), last_ok_at=_stamp(self.now + 3600))
        self.assertEqual(self.of("connections."), [])

    def test_a_connection_failing_for_hours_is_reported_with_its_error(self) -> None:
        # #188 issue 2, the real Gmail case.
        self.write_state(last_poll_at=_stamp(self.now - 60), last_ok_at=_stamp(self.now - 5 * 3600),
                         last_error="gmail is not turned on in this workspace")
        [finding] = self.of("connections.failing")
        self.assertIn("5 hours", finding["observed"])
        self.assertIn("gmail is not turned on in this workspace", finding["observed"])

    def test_the_error_shown_is_redacted(self) -> None:
        self.write_state(last_poll_at=_stamp(self.now - 60), last_ok_at=None,
                         last_error="session unavailable: https://mcp.example.com/u/abc?api_key=0123456789abcdefghijklmn")
        [finding] = self.of("connections.failing")
        text = json.dumps(finding)
        self.assertNotIn("mcp.example.com", text)
        self.assertNotIn("0123456789abcdefghijklmn", text)

    def test_a_stale_gate_error_is_not_reported(self) -> None:
        self.write_state(last_poll_at=_stamp(self.now - 60), last_ok_at=None, last_error="gmail is not turned on")
        with patch("services.doctor.liveness._live_error", return_value=None):
            self.assertEqual(self.of("connections.failing"), [])

    def test_disabled_connections_and_a_disabled_poller_are_silent(self) -> None:
        self.write_state(last_poll_at=_stamp(self.now - 99999))
        config = self.state / "connections" / "gmail" / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text(encoding="utf-8")), "enabled": False}),
                          encoding="utf-8")
        self.assertEqual(self.of("connections."), [])
        with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": "false"}):
            self.assertEqual(self.of("connections."), [])


class GitHubTests(LivenessSandbox):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"XO_GITHUB_POLL_ENABLED": "true", "XO_GITHUB_POLL_INTERVAL_S": "60"})
        env.start()
        self.addCleanup(env.stop)
        project = self.projects / "sample-project" / ".xo" / "project.json"
        document = json.loads(project.read_text(encoding="utf-8"))
        document["git"] = {"remote_url": "https://github.com/acme/sample-project"}
        project.write_text(json.dumps(document), encoding="utf-8")
        self.mirror = self.state / "projects" / PID / "github" / "issues.json"

    def write_mirror(self, **fields) -> None:
        document = json.loads(self.mirror.read_text(encoding="utf-8"))
        document.update(fields)
        self.mirror.write_text(json.dumps(document), encoding="utf-8")

    def test_a_mirror_that_stopped_refreshing_is_reported(self) -> None:
        # #188 issue 4: 60 s interval → stale after max(180 s, 300 s).
        self.write_mirror(fetched_at=_stamp(self.now - 60), error=None)
        self.assertEqual(self.of("github."), [])
        self.write_mirror(fetched_at=_stamp(self.now - 600), error=None)
        [finding] = self.of("github.stale")
        self.assertEqual(finding["subject"], "sample-project")
        self.assertIn("acme/sample-project", " ".join(e["value"] for e in finding["evidence"]))

    def test_a_failing_repository_names_its_error(self) -> None:
        self.write_mirror(fetched_at=_stamp(self.now - 7200),
                          error={"kind": "not_found", "message": "repository not found", "at": _stamp(self.now - 3600)})
        [finding] = self.of("github.")
        self.assertEqual(finding["id"], "github.failing")
        self.assertIn("not_found", finding["observed"])
        self.assertIn("git remote", finding["next_step"])

    def test_a_paused_poller_is_one_finding_not_a_page_of_stale_mirrors(self) -> None:
        self.write_mirror(fetched_at=_stamp(self.now - 7200), error=None)
        snapshot = {"paused": True, "pause_reason": "rate_limited: hourly budget", "spent_last_hour": 0,
                    "remaining": 0, "limit": 5000, "reset_at": None}
        with self.tasks(self.record("github poller")), \
             patch("services.cowork_agent.github_poller.budget_snapshot", return_value=snapshot):
            found = self.of("github.")
        self.assertEqual([f["id"] for f in found], ["github.paused"])
        self.assertIn("rate_limited", found[0]["observed"])

    def test_a_pause_reason_is_redacted(self) -> None:
        self.write_mirror(fetched_at=_stamp(self.now - 7200), error=None)
        snapshot = {"paused": True,
                    "pause_reason": "no_cli: gh isn't installed; see https://cli.github.com/ token "
                                     "sk_" "live_0123456789abcdefghij0123456789ab",
                    "spent_last_hour": 0, "remaining": 0, "limit": 5000, "reset_at": None}
        with self.tasks(self.record("github poller")), \
             patch("services.cowork_agent.github_poller.budget_snapshot", return_value=snapshot):
            [finding] = self.of("github.paused")
        text = json.dumps(finding)
        self.assertNotIn("cli.github.com", text)
        self.assertNotIn("0123456789abcdefghij", text)
        self.assertIn("Install the GitHub CLI", finding["next_step"])

    def test_a_project_without_a_github_remote_is_not_checked(self) -> None:
        project = self.projects / "sample-project" / ".xo" / "project.json"
        document = json.loads(project.read_text(encoding="utf-8"))
        document["git"] = {"remote_url": "https://gitlab.com/acme/sample-project"}
        project.write_text(json.dumps(document), encoding="utf-8")
        self.write_mirror(fetched_at=_stamp(self.now - 7200), error=None)
        self.assertEqual(self.of("github."), [])
