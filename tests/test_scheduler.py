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


if __name__ == "__main__":
    unittest.main()
