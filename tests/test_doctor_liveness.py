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

    def test_a_heartbeat_older_than_the_crash_is_not_mistaken_for_another_server(self) -> None:
        # The heartbeat (now-2) predates the crash (now-1): it's this watcher's
        # own last beat before it died, not evidence of a second server.
        self.beat(2)
        with self.tasks(self.record("watcher", state="crashed", ended_at=self.now - 1, error="RuntimeError: x")):
            [finding] = self.of("watcher.")
        self.assertNotIn("another xo-space server", " ".join(e["value"] for e in finding["evidence"]))

    def test_a_heartbeat_written_after_the_crash_names_another_server(self) -> None:
        self.beat(1)
        with self.tasks(self.record("watcher", state="crashed", ended_at=self.now - 2, error="RuntimeError: x")):
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

    def test_a_heartbeat_older_than_the_process_is_not_stuck_right_after_a_restart(self) -> None:
        # The heartbeat file predates this server's start: it's a leftover
        # from before the restart, not evidence the watcher is stuck.
        self.beat(900)
        with self.tasks(self.record("watcher", started_at=self.now - 2)):
            self.assertEqual(self.of("watcher.heartbeat"), [])

    def test_a_slow_first_tick_still_fails_once_the_process_has_run_long_enough(self) -> None:
        self.beat(900)
        with self.tasks(self.record("watcher", started_at=self.now - 400)):
            [finding] = self.of("watcher.heartbeat")
        self.assertEqual(finding["level"], "FAIL")

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

    def test_a_freshly_restarted_poller_is_not_yet_overdue(self) -> None:
        # Gmail polls every 900 s; tick 30 s; grace 300 s → threshold 1260 s.
        # The poller task started 10 s ago: it hasn't had its chance yet.
        self.write_state(last_poll_at=_stamp(self.now - 1300), last_ok_at=_stamp(self.now - 1300))
        with self.tasks(self.record("connections poller", started_at=self.now - 10)):
            self.assertEqual(self.of("connections.overdue"), [])

    def test_a_poller_running_past_its_own_grace_is_still_overdue(self) -> None:
        self.write_state(last_poll_at=_stamp(self.now - 1300), last_ok_at=_stamp(self.now - 1300))
        with self.tasks(self.record("connections poller", started_at=self.now - 2000)):
            [finding] = self.of("connections.overdue")
        self.assertEqual(finding["subject"], "gmail")

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

    def test_a_freshly_restarted_poller_is_not_yet_stale(self) -> None:
        # 60 s interval → stale after max(180 s, 300 s) = 300 s. The poller
        # task started 10 s ago: it hasn't had its chance yet.
        self.write_mirror(fetched_at=_stamp(self.now - 600), error=None)
        with self.tasks(self.record("github poller", started_at=self.now - 10)):
            self.assertEqual(self.of("github.stale"), [])

    def test_a_poller_running_past_its_own_grace_is_still_stale(self) -> None:
        self.write_mirror(fetched_at=_stamp(self.now - 600), error=None)
        with self.tasks(self.record("github poller", started_at=self.now - 2000)):
            [finding] = self.of("github.stale")
        self.assertEqual(finding["subject"], "sample-project")

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


class SchedulerTests(LivenessSandbox):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"XO_SCHEDULER_ENABLED": "true", "QUIRQ_WATCHER_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        self.beat(1)

    def schedule(self, *, enabled=True, **entry) -> None:
        job = {"id": "nightly-1", "name": "nightly tests", "enabled": enabled, "every_seconds": 86400,
               "command": {"argv": ["true"], "timeout": 60}}
        state = {"next_run": None, "last_run": None, "running_since": None, "last_result": None, **entry}
        (self.state / "scheduler" / "jobs.json").write_text(
            json.dumps({"schema": 1, "jobs": {"nightly-1": job}}), encoding="utf-8")
        (self.state / "scheduler" / "state.json").write_text(
            json.dumps({"schema": 1, "jobs": {"nightly-1": state}}), encoding="utf-8")

    def test_a_job_that_did_not_run_on_time(self) -> None:
        self.schedule(next_run=_stamp(self.now - 60))
        self.assertEqual(self.of("scheduler."), [])
        self.schedule(next_run=_stamp(self.now - 600))
        [finding] = self.of("scheduler.overdue")
        self.assertEqual(finding["title"], "Scheduled command 'nightly tests' didn't run on time")

    def test_a_run_that_outlives_its_timeout_is_stuck(self) -> None:
        self.schedule(running_since=_stamp(self.now - 1000), next_run=_stamp(self.now - 1000))
        self.assertEqual([f["id"] for f in self.of("scheduler.")], ["scheduler.stuck"])

    def test_a_failed_last_run_is_reported(self) -> None:
        self.schedule(next_run=_stamp(self.now + 3600),
                      last_result={"status": "timed_out", "returncode": None, "finished_at": _stamp(self.now - 60)})
        [finding] = self.of("scheduler.last_failed")
        self.assertIn("timed_out", finding["observed"])

    def test_disabled_jobs_and_a_disabled_scheduler_are_silent(self) -> None:
        self.schedule(enabled=False, next_run=_stamp(self.now - 99999))
        self.assertEqual(self.of("scheduler."), [])
        self.schedule(next_run=_stamp(self.now - 99999))
        with patch.dict(os.environ, {"XO_SCHEDULER_ENABLED": "false"}):
            self.assertEqual(self.of("scheduler."), [])

    def two_jobs(self, *, running_since) -> None:
        jobs = {
            "running-job": {"id": "running-job", "name": "running job", "enabled": True, "every_seconds": 86400,
                            "command": {"argv": ["true"], "timeout": 3600}},
            "due-job": {"id": "due-job", "name": "due job", "enabled": True, "every_seconds": 86400,
                        "command": {"argv": ["true"], "timeout": 3600}},
        }
        state = {
            "running-job": {"next_run": None, "last_run": None, "running_since": running_since,
                            "last_result": None},
            "due-job": {"next_run": _stamp(self.now - 600), "last_run": None, "running_since": None,
                        "last_result": None},
        }
        (self.state / "scheduler" / "jobs.json").write_text(
            json.dumps({"schema": 1, "jobs": jobs}), encoding="utf-8")
        (self.state / "scheduler" / "state.json").write_text(
            json.dumps({"schema": 1, "jobs": state}), encoding="utf-8")

    def test_a_due_job_waits_while_the_scheduler_is_at_capacity(self) -> None:
        with patch.dict(os.environ, {"XO_SCHEDULER_MAX_CONCURRENT": "1"}):
            self.two_jobs(running_since=_stamp(self.now - 10))
            self.assertEqual(self.of("scheduler.overdue"), [])
            self.two_jobs(running_since=None)
            [finding] = self.of("scheduler.overdue")
            self.assertEqual(finding["subject"], "due-job")


class UsageTests(LivenessSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bookmark = self.state / "usage" / "active.json"
        env = patch.dict(os.environ, {"USAGE_SYNC_STATE_FILE": str(self.bookmark)})
        env.start()
        self.addCleanup(env.stop)

    def probe(self, outcome: str, age: float) -> None:
        self.bookmark.write_text(json.dumps({"schema": 1, "key_probe": {
            "outcome": outcome, "status": 200 if outcome == "accepted" else 401,
            "at": _stamp(self.now - age)}}), encoding="utf-8")

    def test_usage_that_stopped_being_reported(self) -> None:
        with patch("services.doctor.liveness._usage_token_present", return_value=True):
            self.probe("accepted", 3600)
            self.assertEqual(self.of("usage."), [])
            self.probe("accepted", 27 * 3600)
            [finding] = self.of("usage.stale")
        self.assertIn("Restart the server", finding["next_step"])

    def test_a_rejected_key(self) -> None:
        with patch("services.doctor.liveness._usage_token_present", return_value=True):
            self.probe("rejected", 3600)
            self.assertEqual([f["id"] for f in self.of("usage.")], ["usage.rejected"])

    def test_no_key_means_nothing_is_expected(self) -> None:
        with patch("services.doctor.liveness._usage_token_present", return_value=False):
            self.probe("accepted", 99 * 3600)
            self.assertEqual(self.of("usage."), [])


class RelayTests(LivenessSandbox):
    def relay(self, **snapshot):
        base = {"cadence": "running", "last_poll_at": _stamp(self.now - 30), "last_poll_ok": True}
        base.update(snapshot)
        return patch("services.cowork_agent.project_sharing.status.snapshot", return_value=base)

    def test_a_relay_that_stopped_polling(self) -> None:
        with self.tasks(self.record("relay poller")), self.relay(last_poll_at=_stamp(self.now - 400)), \
             patch("services.cowork_agent.project_sharing.config.poll_interval", return_value=60.0):
            self.assertEqual([f["id"] for f in self.of("relay.")], ["relay.overdue"])

    def test_an_unreachable_xo_and_a_parked_relay(self) -> None:
        with self.tasks(self.record("relay poller")), self.relay(last_poll_ok=False), \
             patch("services.cowork_agent.project_sharing.config.poll_interval", return_value=60.0):
            self.assertEqual([f["id"] for f in self.of("relay.")], ["relay.unreachable"])
        with self.tasks(self.record("relay poller")), self.relay(cadence="parked", last_poll_at=None):
            self.assertEqual(self.of("relay."), [])

    def test_outside_the_server_the_relay_is_not_judged(self) -> None:
        with self.relay(last_poll_at=_stamp(self.now - 99999)):
            self.assertEqual(self.of("relay."), [])
