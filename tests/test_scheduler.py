"""Tests for utils/commands/scheduler.py.

Hermetic: QUIRQ_STATE_ROOT points at a temp dir, `now` is injected, jobs are
`sys.executable -c ...` one-liners so they are real subprocesses that finish
in milliseconds. No sleeps; where a job must be running we block it on a flag
file and release it from the test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.local_state import quirq_state_dir
from utils.commands import scheduler

PY = sys.executable
T0 = datetime(2026, 9, 11, 10, 0, 0, tzinfo=timezone.utc)
# Waits until the file named by argv[1] exists, then exits 0.
BLOCK = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n"


def _cmd(code: str = "pass", *extra: str, timeout: float = 30) -> dict:
    return {"argv": [PY, "-c", code, *extra], "timeout": timeout}


def _job(name: str = "job", every: int = 60, code: str = "pass", *extra: str, **fields) -> dict:
    return {"name": name, "command": _cmd(code, *extra), "every_seconds": every, **fields}


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)}, clear=False)
        self._env.start()
        scheduler.reset_state()

    def tearDown(self) -> None:
        # Release any job still blocked on a flag file, then forget it.
        for run in list(scheduler._running.values()):
            flag = self.root / f"{run.job_id}.go"
            flag.write_text("go")
            if run.thread is not None:
                run.thread.join(10)
        scheduler.reset_state()
        self._env.stop()
        self._tmp.cleanup()

    # ── helpers ──

    def _flag(self, job_id: str) -> Path:
        return self.root / f"{job_id}.go"

    def _blocking_job(self, name: str, every: int = 60) -> dict:
        """Register a job that blocks until its flag file exists. The flag path
        must contain the id, which we only know after creation, so register
        with a placeholder and then rewrite the definition."""
        job = scheduler.create_job(_job(name, every, BLOCK, "placeholder"), now=T0)
        return scheduler.update_job(
            job["id"], _job(name, every, BLOCK, str(self._flag(job["id"]))), now=T0
        )

    def _release(self, job_id: str) -> None:
        self._flag(job_id).write_text("go")
        scheduler._running[job_id].thread.join(10)

    def _state(self, job_id: str) -> dict:
        return json.loads(scheduler.state_file().read_text(encoding="utf-8"))["jobs"][job_id]

    # ── Task 1: validation and the store ──

    def test_state_root_is_the_one_local_state_exposes(self) -> None:
        # One definition (utils/runtime_env.py), re-exported by local_state.
        self.assertIs(scheduler.quirq_state_dir, quirq_state_dir)
        self.assertEqual(scheduler.scheduler_dir(), quirq_state_dir() / "scheduler")

    def test_create_validates_writes_both_files_and_schedules_one_interval_out(self) -> None:
        job = scheduler.create_job(_job("Pull issues", 3600, project_id="blackhole"), now=T0)
        self.assertTrue(job["id"].startswith("pull-issues-"))
        self.assertEqual(job["every_seconds"], 3600)
        self.assertEqual(job["project_id"], "blackhole")
        self.assertTrue(job["enabled"])
        self.assertEqual(job["command"]["argv"][:2], [PY, "-c"])
        self.assertEqual(job["command"]["timeout"], 30.0)
        self.assertEqual(job["created_at"], "2026-09-11T10:00:00Z")
        self.assertEqual(job["next_run"], "2026-09-11T11:00:00Z")
        self.assertIsNone(job["last_run"])
        self.assertFalse(job["running"])

        jobs = json.loads(scheduler.jobs_file().read_text(encoding="utf-8"))
        state = json.loads(scheduler.state_file().read_text(encoding="utf-8"))
        self.assertEqual(jobs["schema"], 1)
        self.assertIn(job["id"], jobs["jobs"])
        self.assertNotIn("next_run", jobs["jobs"][job["id"]])   # state never leaks into definitions
        self.assertEqual(state["jobs"][job["id"]]["next_run"], "2026-09-11T11:00:00Z")

    def test_rejects_bad_definitions_and_writes_nothing(self) -> None:
        bad = [
            ({**_job(), "name": ""}, "name"),
            ({**_job(), "name": "x" * 65}, "name"),
            ({**_job(), "every_seconds": 0}, "every_seconds"),
            ({**_job(), "every_seconds": -5}, "every_seconds"),
            ({**_job(), "every_seconds": "60"}, "every_seconds"),
            ({**_job(), "every_seconds": True}, "every_seconds"),
            ({**_job(), "command": {"argv": [PY, "-c", "pass"]}}, "timeout"),
            ({**_job(), "command": {"command": "ls && rm -rf /", "timeout": 5}}, "shell"),
            ({**_job(), "command": {"argv": [], "timeout": 5}}, "argv"),
            ({**_job(), "command": "ls"}, "object"),
            ({**_job(), "project_id": ""}, "project_id"),
            ({**_job(), "enabled": "yes"}, "enabled"),
            ({**_job(), "surprise": 1}, "unknown"),
            ("not an object", "object"),
        ]
        for payload, needle in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as ctx:
                    scheduler.create_job(payload, now=T0)
                self.assertIn(needle, str(ctx.exception).lower())
        self.assertFalse(scheduler.jobs_file().exists())
        self.assertFalse(scheduler.state_file().exists())

    def test_intervals_shorter_than_the_watcher_tick_are_polling_not_scheduling(self) -> None:
        # The only lower bound on every_seconds is the watcher's tick period:
        # the scheduler looks once per tick, so anything shorter could not be
        # honoured. There is no separate policy floor.
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "30"}):
            self.assertEqual(scheduler.tick_interval_seconds(), 30.0)
            with self.assertRaises(ValueError) as ctx:
                scheduler.create_job(_job("fast", 10), now=T0)
            self.assertIn("watcher tick", str(ctx.exception))
            self.assertIn("polling", str(ctx.exception))
            scheduler.create_job(_job("on-the-tick", 30), now=T0)   # equal is fine
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "0.25"}):
            scheduler.create_job(_job("one-second", 1), now=T0)     # 1 s on a 0.25 s tick
        # The one shared reader (utils/runtime_env.py) clamps and defaults.
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "junk"}):
            self.assertEqual(scheduler.tick_interval_seconds(), 1.0)
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "999"}):
            self.assertEqual(scheduler.tick_interval_seconds(), 60.0)
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "0.01"}):
            self.assertEqual(scheduler.tick_interval_seconds(), 0.25)

    def test_a_job_faster_than_a_slowed_watcher_runs_once_per_tick_and_says_so(self) -> None:
        job = scheduler.create_job(_job("fast", 10), now=T0)   # registered on a 1 s tick
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "60"}):
            report = scheduler.tick(now=_at(60))
        self.assertEqual(report.started, [job["id"]])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("once per tick", report.errors[0])
        scheduler._running[job["id"]].thread.join(10)
        # The six missed 10 s slots collapsed onto the next future one.
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-11T10:01:10Z")

    def test_command_string_form_is_stored_as_argv(self) -> None:
        job = scheduler.create_job(
            {"name": "echo", "command": {"command": f'"{PY}" -c pass', "timeout": 5}, "every_seconds": 60},
            now=T0,
        )
        self.assertEqual(job["command"]["argv"], [PY, "-c", "pass"])
        self.assertNotIn("command", job["command"])

    def test_get_list_update_delete(self) -> None:
        a = scheduler.create_job(_job("a", 60), now=T0)
        b = scheduler.create_job(_job("b", 120), now=T0)
        self.assertEqual([j["id"] for j in scheduler.list_jobs()], sorted([a["id"], b["id"]]))
        self.assertEqual(scheduler.get_job(a["id"])["every_seconds"], 60)

        # Same interval: next_run untouched. New interval: recomputed from now.
        same = scheduler.update_job(a["id"], _job("a renamed", 60), now=_at(30))
        self.assertEqual(same["name"], "a renamed")
        self.assertEqual(same["next_run"], "2026-09-11T10:01:00Z")
        self.assertEqual(same["updated_at"], "2026-09-11T10:00:30Z")
        changed = scheduler.update_job(a["id"], _job("a", 600), now=_at(30))
        self.assertEqual(changed["next_run"], "2026-09-11T10:10:30Z")

        scheduler.delete_job(b["id"])
        self.assertEqual([j["id"] for j in scheduler.list_jobs()], [a["id"]])
        with self.assertRaises(scheduler.UnknownJobError):
            scheduler.get_job(b["id"])
        with self.assertRaises(scheduler.UnknownJobError):
            scheduler.update_job(b["id"], _job(), now=T0)
        with self.assertRaises(scheduler.UnknownJobError):
            scheduler.delete_job(b["id"])
        with self.assertRaises(scheduler.UnknownJobError):
            scheduler.list_runs(b["id"])

    def test_corrupt_jobs_file_is_refused_not_rewritten(self) -> None:
        scheduler.jobs_file().parent.mkdir(parents=True)
        scheduler.jobs_file().write_text("{not json", encoding="utf-8")
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.list_jobs()
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.create_job(_job(), now=T0)
        self.assertEqual(scheduler.jobs_file().read_text(encoding="utf-8"), "{not json")

    def test_advance_collapses_missed_slots_onto_the_jobs_grid(self) -> None:
        nr = _at(60)
        self.assertEqual(scheduler.advance(nr, 60, _at(59)), _at(60))    # not due: unchanged
        self.assertEqual(scheduler.advance(nr, 60, _at(60)), _at(120))   # due exactly on the slot
        self.assertEqual(scheduler.advance(nr, 60, _at(61)), _at(120))   # a late tick
        self.assertEqual(scheduler.advance(nr, 60, _at(210)), _at(240))  # three slots missed → next future slot
        self.assertEqual(scheduler.advance(nr, 60, _at(240)), _at(300))  # landing on a slot counts as due

    # ── Task 2: the tick ──

    def test_tick_is_quiet_before_the_slot_and_starts_the_job_on_it(self) -> None:
        job = scheduler.create_job(_job("j", 60), now=T0)
        early = scheduler.tick(now=_at(59))
        self.assertTrue(early.quiet)
        self.assertEqual(early.as_dict()["started"], [])

        due = scheduler.tick(now=_at(61))
        self.assertEqual(due.started, [job["id"]])
        self.assertTrue(due.enabled)
        state = self._state(job["id"])
        self.assertEqual(state["running_since"], "2026-09-11T10:01:01Z")
        self.assertEqual(state["next_run"], "2026-09-11T10:02:00Z")   # grid, not now + 60
        scheduler._running[job["id"]].thread.join(10)

    def test_tick_is_idempotent_for_the_same_now(self) -> None:
        # A blocking job, so the run is provably still in progress on the
        # second call: idempotency must hold while the process exists.
        job = self._blocking_job("j", 60)
        first = scheduler.tick(now=_at(60))
        second = scheduler.tick(now=_at(60))
        self.assertEqual(first.started, [job["id"]])
        self.assertTrue(second.quiet)
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-11T10:02:00Z")
        self.assertEqual(self._state(job["id"])["running_since"], "2026-09-11T10:01:00Z")
        self._release(job["id"])

    def test_state_is_on_disk_before_the_process_is_launched(self) -> None:
        job = scheduler.create_job(_job("j", 60), now=T0)
        seen: dict = {}

        def fake_launch(job_def, trigger, now):
            seen["running_since"] = self._state(job_def["id"])["running_since"]
            seen["next_run"] = self._state(job_def["id"])["next_run"]
            seen["trigger"] = trigger

        with patch.object(scheduler, "_launch", fake_launch):
            report = scheduler.tick(now=_at(60))
        self.assertEqual(report.started, [job["id"]])
        self.assertEqual(seen, {"running_since": "2026-09-11T10:01:00Z",
                                "next_run": "2026-09-11T10:02:00Z", "trigger": "schedule"})

    def test_catch_up_runs_once_and_resumes_from_the_next_future_slot(self) -> None:
        job = scheduler.create_job(_job("daily", 86400), now=T0)
        three_days_late = T0 + timedelta(days=4, hours=2)
        report = scheduler.tick(now=three_days_late)
        self.assertEqual(report.started, [job["id"]])
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-16T10:00:00Z")
        scheduler._running[job["id"]].thread.join(10)
        self.assertTrue(scheduler.tick(now=three_days_late + timedelta(seconds=1)).finished)

    def test_disabled_job_and_disabled_scheduler_start_nothing(self) -> None:
        off = scheduler.create_job(_job("off", 60, enabled=False), now=T0)
        on = scheduler.create_job(_job("on", 60), now=T0)
        with patch.dict(os.environ, {"XO_SCHEDULER_ENABLED": "false"}):
            report = scheduler.tick(now=_at(120))
        self.assertFalse(report.enabled)
        self.assertEqual(report.started, [])
        self.assertIsNone(self._state(on["id"])["running_since"])

        report = scheduler.tick(now=_at(120))
        self.assertEqual(report.started, [on["id"]])
        self.assertIsNone(self._state(off["id"])["running_since"])
        scheduler._running[on["id"]].thread.join(10)

    def test_corrupt_jobs_file_is_reported_and_nothing_runs(self) -> None:
        scheduler.jobs_file().parent.mkdir(parents=True)
        scheduler.jobs_file().write_text("[]", encoding="utf-8")
        report = scheduler.tick(now=T0)
        self.assertEqual(report.started, [])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("jobs.json", report.errors[0])
        self.assertEqual(scheduler.jobs_file().read_text(encoding="utf-8"), "[]")

    def test_hand_added_job_is_adopted_one_interval_out(self) -> None:
        # jobs.json edited directly: no state entry yet. The tick seeds one and
        # does not run the job on the spot.
        scheduler._write_doc(scheduler.jobs_file(), {"schema": 1, "jobs": {
            "manual-000000": {"id": "manual-000000", "name": "manual", "project_id": None,
                              "command": _cmd(), "every_seconds": 60, "enabled": True,
                              "created_at": "2026-09-11T10:00:00Z", "updated_at": "2026-09-11T10:00:00Z"}}})
        report = scheduler.tick(now=_at(500))
        self.assertEqual(report.started, [])
        self.assertEqual(self._state("manual-000000")["next_run"], "2026-09-11T10:09:20Z")
        self.assertEqual(scheduler.tick(now=_at(560)).started, ["manual-000000"])
        scheduler._running["manual-000000"].thread.join(10)

    def test_a_definition_the_executor_refuses_is_an_error_not_a_crash(self) -> None:
        scheduler._write_doc(scheduler.jobs_file(), {"schema": 1, "jobs": {
            "bad-000000": {"id": "bad-000000", "name": "bad", "project_id": None,
                           "command": {"argv": [], "timeout": 5}, "every_seconds": 60, "enabled": True,
                           "created_at": "2026-09-11T10:00:00Z", "updated_at": "2026-09-11T10:00:00Z"}}})
        scheduler._write_doc(scheduler.state_file(), {"schema": 1, "jobs": {
            "bad-000000": {"next_run": "2026-09-11T10:01:00Z", "last_run": None,
                           "running_since": None, "last_result": None}}})
        report = scheduler.tick(now=_at(60))
        self.assertEqual(report.started, [])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("bad-000000", report.errors[0])
        state = self._state("bad-000000")
        self.assertIsNone(state["running_since"])
        self.assertEqual(state["next_run"], "2026-09-11T10:02:00Z")  # advanced: one error per slot, not per tick

    # ── Task 3: harvest, overlap, cap, lost, run-now ──

    def test_finished_run_is_harvested_into_state_and_history(self) -> None:
        job = scheduler.create_job(_job("j", 60, "import sys; print('hello'); sys.exit(3)"), now=T0)
        scheduler.tick(now=_at(60))
        scheduler._running[job["id"]].thread.join(10)
        report = scheduler.tick(now=_at(61))
        self.assertEqual(report.finished, [job["id"]])
        self.assertNotIn(job["id"], scheduler._running)

        state = self._state(job["id"])
        self.assertIsNone(state["running_since"])
        self.assertEqual(state["last_run"], "2026-09-11T10:01:00Z")
        self.assertEqual(state["last_result"]["status"], "failed")
        self.assertEqual(state["last_result"]["returncode"], 3)
        self.assertEqual(state["last_result"]["trigger"], "schedule")
        self.assertIn("hello", state["last_result"]["output_tail"])

        runs = scheduler.list_runs(job["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertTrue(scheduler.log_file(job["id"]).exists())   # the executor's full log
        self.assertFalse(scheduler.get_job(job["id"])["running"])

    def test_status_mapping_ok_timeout_missing_binary(self) -> None:
        ok = scheduler.create_job(_job("ok", 60, "pass"), now=T0)
        slow = scheduler.create_job(
            {"name": "slow", "command": {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 0.5},
             "every_seconds": 60}, now=T0)
        missing = scheduler.create_job(
            {"name": "missing", "command": {"argv": ["definitely-not-a-binary-xyz"], "timeout": 5},
             "every_seconds": 60}, now=T0)
        scheduler.tick(now=_at(60))
        for j in (ok, slow, missing):
            scheduler._running[j["id"]].thread.join(15)
        scheduler.tick(now=_at(61))
        self.assertEqual(self._state(ok["id"])["last_result"]["status"], "ok")
        self.assertEqual(self._state(slow["id"])["last_result"]["status"], "timed_out")
        self.assertEqual(self._state(missing["id"])["last_result"]["status"], "missing_binary")

    def test_overlap_is_skipped_not_queued_and_no_second_process_starts(self) -> None:
        job = self._blocking_job("block", 60)
        self.assertEqual(scheduler.tick(now=_at(60)).started, [job["id"]])
        first_thread = scheduler._running[job["id"]].thread

        report = scheduler.tick(now=_at(120))
        self.assertEqual(report.skipped, [job["id"]])
        self.assertEqual(report.started, [])
        self.assertIs(scheduler._running[job["id"]].thread, first_thread)
        state = self._state(job["id"])
        self.assertEqual(state["next_run"], "2026-09-11T10:03:00Z")
        self.assertEqual(state["last_result"]["status"], "skipped")
        self.assertEqual(state["running_since"], "2026-09-11T10:01:00Z")

        self._release(job["id"])
        after = scheduler.tick(now=_at(121))
        self.assertEqual(after.finished, [job["id"]])
        runs = scheduler.list_runs(job["id"])
        self.assertEqual([r["status"] for r in runs], ["ok", "skipped"])   # newest first

    def test_concurrency_cap_defers_without_advancing_next_run(self) -> None:
        a = self._blocking_job("a", 60)
        b = self._blocking_job("b", 60)
        with patch.dict(os.environ, {"XO_SCHEDULER_MAX_CONCURRENT": "1"}):
            report = scheduler.tick(now=_at(60))
            self.assertEqual(report.started, [a["id"]])
            self.assertEqual(report.deferred, [b["id"]])
            self.assertEqual(self._state(b["id"])["next_run"], "2026-09-11T10:01:00Z")   # still due
            self.assertIsNone(self._state(b["id"])["running_since"])

            self._release(a["id"])
            report = scheduler.tick(now=_at(61))
            self.assertEqual(report.finished, [a["id"]])
            self.assertEqual(report.started, [b["id"]])
        self._release(b["id"])

    def test_lost_run_is_recorded_and_cleared(self) -> None:
        job = scheduler.create_job(_job("j", 60), now=T0)
        state = json.loads(scheduler.state_file().read_text(encoding="utf-8"))
        state["jobs"][job["id"]]["running_since"] = "2026-09-11T09:59:00Z"   # a previous process
        scheduler._write_doc(scheduler.state_file(), state)

        report = scheduler.tick(now=_at(10))
        self.assertEqual(report.lost, [job["id"]])
        entry = self._state(job["id"])
        self.assertIsNone(entry["running_since"])
        self.assertEqual(entry["last_result"]["status"], "lost")
        self.assertEqual(entry["last_result"]["started_at"], "2026-09-11T09:59:00Z")
        self.assertEqual(scheduler.list_runs(job["id"])[0]["status"], "lost")

    def test_run_now_is_manual_single_flight_and_leaves_next_run_alone(self) -> None:
        job = self._blocking_job("block", 3600)
        view = scheduler.run_now(job["id"], now=_at(5))
        self.assertTrue(view["running"])
        self.assertEqual(view["running_since"], "2026-09-11T10:00:05Z")
        self.assertEqual(view["next_run"], "2026-09-11T11:00:00Z")
        with self.assertRaises(scheduler.JobRunningError):
            scheduler.run_now(job["id"], now=_at(6))
        with self.assertRaises(scheduler.UnknownJobError):
            scheduler.run_now("nope-000000", now=_at(6))

        self._release(job["id"])
        report = scheduler.tick(now=_at(7))
        self.assertEqual(report.finished, [job["id"]])
        self.assertEqual(self._state(job["id"])["last_result"]["trigger"], "manual")
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-11T11:00:00Z")

    def test_run_now_works_for_a_disabled_job(self) -> None:
        job = scheduler.create_job(_job("off", 60, enabled=False), now=T0)
        scheduler.run_now(job["id"], now=T0)
        scheduler._running[job["id"]].thread.join(10)
        self.assertEqual(scheduler.tick(now=_at(1)).finished, [job["id"]])

    def test_delete_while_running_keeps_the_history(self) -> None:
        job = self._blocking_job("block", 60)
        scheduler.tick(now=_at(60))
        scheduler.delete_job(job["id"])
        self._release(job["id"])
        report = scheduler.tick(now=_at(61))
        self.assertEqual(report.finished, [job["id"]])
        self.assertTrue(scheduler.runs_file(job["id"]).exists())
        self.assertNotIn(job["id"], json.loads(scheduler.state_file().read_text(encoding="utf-8"))["jobs"])

    def test_list_runs_limit_and_order(self) -> None:
        job = scheduler.create_job(_job("j", 60), now=T0)
        for i in range(5):
            scheduler._append_run(job["id"], {"status": "ok", "started_at": f"2026-09-11T10:0{i}:00Z"})
        runs = scheduler.list_runs(job["id"], limit=3)
        self.assertEqual([r["started_at"][-6:-1] for r in runs], ["04:00", "03:00", "02:00"])


if __name__ == "__main__":
    unittest.main()
