"""Service tests for services/cowork_agent/scheduler.py.

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

from services.cowork_agent import scheduler

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
            ({**_job(), "every_seconds": 59}, "every_seconds"),
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


if __name__ == "__main__":
    unittest.main()
