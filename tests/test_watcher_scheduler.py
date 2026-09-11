"""The watcher as the scheduler's clock: one guarded call per tick.

Drives ``Watcher._scheduler_step`` (and, once, the whole ``Watcher.tick``)
against the real scheduler with a fake clock: ``scheduler.now_utc`` is
patched so a "tick at 10:01" is literally that, and jobs are
``sys.executable -c ...`` one-liners that finish in milliseconds or block on
a flag file the test creates to release them. No sleeps.

The watcher imports the visualizer package (``fcntl``), so this module
skips itself on Windows; run it on Linux/WSL with AGENT_NAME=claude_code.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils.commands import scheduler

try:
    from services.cowork_agent.visualizer import watcher as watcher_mod
    from services.cowork_agent.visualizer.state import watcher_heartbeat_path
except ImportError as exc:  # pragma: no cover - platform gate
    raise unittest.SkipTest(f"watcher needs POSIX: {exc}") from exc

PY = sys.executable
T0 = datetime(2026, 9, 11, 10, 0, 0, tzinfo=timezone.utc)
BLOCK = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n"


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _job(name: str, every: int, code: str = "pass", *extra: str, **fields) -> dict:
    return {"name": name, "command": {"argv": [PY, "-c", code, *extra], "timeout": 30},
            "every_seconds": every, **fields}


class WatcherSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir()
        (self.root / "projects").mkdir()
        self._env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.root / "state"),
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
            "QUIRQ_WATCHER_SOURCE_MODE": "active",
        }, clear=False)
        self._env.start()
        scheduler.reset_state()
        # A watcher with no telemetry sources: the active agent has no
        # visualizer_source capability, so __init__ registers nothing.
        with (
            patch.object(watcher_mod, "get_active_agent", return_value=SimpleNamespace(name="stub")),
            patch.object(watcher_mod, "try_load_capability", return_value=None),
        ):
            self.watcher = watcher_mod.Watcher()

    def tearDown(self) -> None:
        for run in list(scheduler._running.values()):
            (self.root / f"{run.job_id}.go").write_text("go")
            if run.thread is not None:
                run.thread.join(10)
        scheduler.reset_state()
        self._env.stop()
        self._tmp.cleanup()

    # ── helpers ──

    def _tick(self, at: datetime, *, full: bool = False) -> dict:
        """One watcher tick at a fixed wall-clock time. ``full`` runs the whole
        tick body (sources, sinks, workspace tier, heartbeat); otherwise just
        the scheduler step plus the heartbeat, which is what is under test."""
        with patch.object(scheduler, "now_utc", return_value=at):
            if full:
                self.watcher.tick()
            else:
                self.watcher._scheduler_step()
                self.watcher._write_heartbeat(tick_started=0.0)
        return json.loads(watcher_heartbeat_path().read_text(encoding="utf-8"))

    def _blocking_job(self, name: str, every: int) -> dict:
        job = scheduler.create_job(_job(name, every, BLOCK, "placeholder"), now=T0)
        flag = self.root / f"{job['id']}.go"
        return scheduler.update_job(job["id"], _job(name, every, BLOCK, str(flag)), now=T0)

    def _release(self, job_id: str) -> None:
        (self.root / f"{job_id}.go").write_text("go")
        scheduler._running[job_id].thread.join(10)

    def _state(self, job_id: str) -> dict:
        return json.loads(scheduler.state_file().read_text(encoding="utf-8"))["jobs"][job_id]

    # ── use cases ──

    def test_the_watcher_and_the_scheduler_share_one_tick_interval_reader(self) -> None:
        # Not two readers kept in sync by a test: the same function object.
        self.assertIs(watcher_mod._poll_interval_seconds, scheduler.tick_interval_seconds)

    def test_quiet_tick_before_the_slot_still_beats_and_reports(self) -> None:
        scheduler.create_job(_job("daily", 86400), now=T0)
        beat = self._tick(_at(1))
        self.assertEqual(beat["tick_count"], 1)
        self.assertEqual(beat["scheduler"], {
            "enabled": True, "started": [], "finished": [], "skipped": [],
            "deferred": [], "lost": [], "errors": [],
        })

    def test_long_job_spans_many_ticks_and_is_harvested_when_done(self) -> None:
        # The "10 minute job on a 1 second tick" case: one start, quiet ticks
        # while it runs, one finish.
        job = self._blocking_job("long", 3600)
        self.assertEqual(self._tick(_at(3600))["scheduler"]["started"], [job["id"]])

        for s in range(3601, 3611):  # ten more ticks while it is running
            beat = self._tick(_at(s))
            self.assertEqual(beat["scheduler"]["started"], [], f"tick at +{s}s re-launched the job")
            self.assertEqual(beat["scheduler"]["finished"], [])
        self.assertEqual(len(scheduler._running), 1)
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-11T12:00:00Z")

        self._release(job["id"])
        beat = self._tick(_at(3612))
        self.assertEqual(beat["scheduler"]["finished"], [job["id"]])
        result = self._state(job["id"])["last_result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["trigger"], "schedule")
        self.assertIsNone(self._state(job["id"])["running_since"])

    def test_overlap_at_the_next_slot_is_skipped_not_doubled(self) -> None:
        job = self._blocking_job("slow", 60)
        self._tick(_at(60))
        first_thread = scheduler._running[job["id"]].thread
        beat = self._tick(_at(120))
        self.assertEqual(beat["scheduler"]["skipped"], [job["id"]])
        self.assertEqual(beat["scheduler"]["started"], [])
        self.assertIs(scheduler._running[job["id"]].thread, first_thread)
        self._release(job["id"])
        self.assertEqual(self._tick(_at(121))["scheduler"]["finished"], [job["id"]])
        self.assertEqual([r["status"] for r in scheduler.list_runs(job["id"])], ["ok", "skipped"])

    def test_laptop_closed_for_days_runs_once_on_wake(self) -> None:
        job = scheduler.create_job(_job("daily", 86400), now=T0)
        self.assertTrue(self._tick(_at(60))["scheduler"]["started"] == [])
        wake = T0 + timedelta(days=4, hours=2)
        beat = self._tick(wake)
        self.assertEqual(beat["scheduler"]["started"], [job["id"]])
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-16T10:00:00Z")
        scheduler._running[job["id"]].thread.join(10)
        self.assertEqual(self._tick(wake + timedelta(seconds=1))["scheduler"]["finished"], [job["id"]])
        self.assertEqual(len(scheduler.list_runs(job["id"])), 1)

    def test_run_left_behind_by_a_previous_process_is_recorded_as_lost(self) -> None:
        job = scheduler.create_job(_job("j", 60), now=T0)
        state = json.loads(scheduler.state_file().read_text(encoding="utf-8"))
        state["jobs"][job["id"]]["running_since"] = "2026-09-11T09:59:00Z"
        scheduler._write_doc(scheduler.state_file(), state)
        beat = self._tick(_at(5))
        self.assertEqual(beat["scheduler"]["lost"], [job["id"]])
        self.assertEqual(self._state(job["id"])["last_result"]["status"], "lost")
        self.assertIsNone(self._state(job["id"])["running_since"])

    def test_manual_run_is_harvested_by_the_watcher(self) -> None:
        job = scheduler.create_job(_job("j", 86400, "print('manual ok')"), now=T0)
        with patch.object(scheduler, "now_utc", return_value=_at(5)):
            scheduler.run_now(job["id"])
        scheduler._running[job["id"]].thread.join(10)
        beat = self._tick(_at(6))
        self.assertEqual(beat["scheduler"]["finished"], [job["id"]])
        result = self._state(job["id"])["last_result"]
        self.assertEqual(result["trigger"], "manual")
        self.assertIn("manual ok", result["output_tail"])
        self.assertEqual(self._state(job["id"])["next_run"], "2026-09-12T10:00:00Z")

    def test_disabled_scheduler_is_reported_and_starts_nothing(self) -> None:
        scheduler.create_job(_job("j", 60), now=T0)
        with patch.dict(os.environ, {"XO_SCHEDULER_ENABLED": "false"}):
            beat = self._tick(_at(120))
        self.assertFalse(beat["scheduler"]["enabled"])
        self.assertEqual(beat["scheduler"]["started"], [])
        self.assertEqual(scheduler._running, {})

    def test_a_scheduler_crash_does_not_stop_the_watcher(self) -> None:
        with patch.object(scheduler, "tick", side_effect=RuntimeError("boom")):
            beat = self._tick(_at(1))
        self.assertEqual(beat["tick_count"], 1)
        self.assertEqual(beat["scheduler"], {"error": "scheduler tick raised; see log"})
        # And it recovers on the next tick.
        self.assertTrue(self._tick(_at(2))["scheduler"]["enabled"])

    def test_two_jobs_due_together_both_start_and_both_finish(self) -> None:
        a = scheduler.create_job(_job("a", 60, "print('A')"), now=T0)
        b = scheduler.create_job(_job("b", 60, "print('B')"), now=T0)
        beat = self._tick(_at(60))
        self.assertEqual(sorted(beat["scheduler"]["started"]), sorted([a["id"], b["id"]]))
        for j in (a, b):
            scheduler._running[j["id"]].thread.join(10)
        beat = self._tick(_at(61))
        self.assertEqual(sorted(beat["scheduler"]["finished"]), sorted([a["id"], b["id"]]))
        self.assertIn("A", self._state(a["id"])["last_result"]["output_tail"])
        self.assertIn("B", self._state(b["id"])["last_result"]["output_tail"])

    def test_full_watcher_tick_end_to_end(self) -> None:
        # The real tick body: sources (none), sinks, workspace tier over an
        # empty projects root, then the scheduler step, then the heartbeat.
        job = scheduler.create_job(_job("e2e", 60, "print('end to end')"), now=T0)
        beat = self._tick(_at(30), full=True)
        self.assertEqual(beat["scheduler"]["started"], [])
        beat = self._tick(_at(60), full=True)
        self.assertEqual(beat["scheduler"]["started"], [job["id"]])
        scheduler._running[job["id"]].thread.join(10)
        beat = self._tick(_at(61), full=True)
        self.assertEqual(beat["scheduler"]["finished"], [job["id"]])
        self.assertEqual(beat["tick_count"], 3)
        self.assertIn("duration_ms", beat)
        runs = scheduler.list_runs(job["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertIn("end to end", runs[0]["output_tail"])
        self.assertTrue(scheduler.log_file(job["id"]).exists())


if __name__ == "__main__":
    unittest.main()
