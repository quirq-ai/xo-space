"""Project sharing on the watcher's command scheduler: status on disk, the
built-in "sharing tick" job, nudges, and the tick as its own process
(services/cowork_agent/project_sharing/{status,job,tick}.py)."""
from __future__ import annotations

import fcntl
import io
import json
import os
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import job, poller, service, status, tick
from utils.commands import scheduler


class _StateRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        status.reset()
        status.read_from_file(False)
        job._job_id, job._watcher_off, job._last_signature, job._last_scan = None, False, None, 0.0
        scheduler.reset_state()
        self.addCleanup(status.reset)
        self.addCleanup(status.read_from_file, False)


class StatusOnDiskTests(_StateRoot):
    def test_save_then_load_keeps_repos_and_recent(self):
        status.record_poll(ok=True, membership={"github.com/a/app"}, local={"github.com/a/app": "app"})
        status.record_available("github.com/a/other")
        status.save()
        status.reset()
        status.load()
        snap = status.snapshot()
        self.assertTrue(snap["repos"]["github.com/a/app"]["shared"])
        self.assertEqual([e["kind"] for e in snap["recent"]], ["shared_with_you"])
        self.assertIsInstance(status._state["recent"], deque)

    def test_a_revoke_is_seen_across_processes(self):
        # "revoked" means: shared last tick, not shared now. Each tick is a new
        # process, so this only works when the status survives between them.
        status.record_poll(ok=True, membership={"github.com/a/app"}, local={})
        status.save()
        status.reset()          # the next process starts empty...
        status.load()           # ...and loads what the last tick saw
        status.record_poll(ok=True, membership=set(), local={})
        self.assertIn("revoked", [e["kind"] for e in status.snapshot()["recent"]])

    def test_the_server_reads_the_file_once_switched_on(self):
        status.record_poll(ok=True, membership={"github.com/a/app"}, local={})
        status.save()
        status.reset()
        self.assertEqual(status.snapshot()["repos"], {})
        status.read_from_file()
        self.assertIn("github.com/a/app", status.snapshot()["repos"])

    def test_no_file_yet_falls_back_to_memory(self):
        status.read_from_file()
        self.assertEqual(status.snapshot()["cadence"], "parked")


class JobTests(_StateRoot):
    def test_ensure_job_creates_once_and_resets_an_edited_job(self):
        with patch("shutil.which", return_value="/usr/bin/qq"):
            first = job.ensure_job()
            jobs = scheduler.list_jobs()
            scheduler.update_job(first, {**{k: jobs[0][k] for k in ("name", "command", "every_seconds")},
                                         "description": "edited", "enabled": False})
            second = job.ensure_job()
        jobs = scheduler.list_jobs()
        self.assertEqual(first, second)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["description"], job.DESCRIPTION)
        self.assertTrue(jobs[0]["enabled"])
        self.assertEqual(jobs[0]["command"]["argv"], ["/usr/bin/qq", "sharing", "tick", "--json"])

    def test_without_qq_the_job_runs_the_tick_module(self):
        with patch("shutil.which", return_value=None):
            job.ensure_job()
        argv = scheduler.list_jobs()[0]["command"]["argv"]
        self.assertEqual(argv[1:], ["-m", "services.cowork_agent.project_sharing.tick", "--json"])

    def test_nudge_starts_the_job_now(self):
        job._job_id = "sharing-tick-abc"
        with patch.object(scheduler, "run_now") as run_now:
            job.nudge()
        run_now.assert_called_once_with("sharing-tick-abc")

    def test_a_nudge_during_a_run_gets_one_more_round(self):
        job._job_id = "sharing-tick-abc"
        with patch.object(scheduler, "run_now", side_effect=scheduler.JobRunningError("busy")):
            job.nudge()
        self.assertTrue(job.take_pending_nudge())
        self.assertFalse(job.take_pending_nudge())

    def test_without_the_job_the_old_nudge_is_used(self):
        with patch.object(poller, "nudge") as old:
            job.nudge()
        old.assert_called_once_with()

    def test_watcher_off_is_the_reason_shown(self):
        job.mark_watcher_off()
        snap = service.status_snapshot()
        self.assertEqual((snap["cadence"], snap["reason"]), ("parked", "watcher_off"))

    def test_local_change_starts_the_job(self):
        job._job_id = "sharing-tick-abc"
        signatures = iter([("a",), ("a",), ("a", "b")])
        with patch.object(poller, "_local_signature", side_effect=lambda: next(signatures)), \
                patch.object(job, "nudge") as nudge, patch.object(job.time, "monotonic", side_effect=[10, 20, 30]):
            job.local_change_check()   # baseline
            job.local_change_check()   # unchanged
            nudge.assert_not_called()
            job.local_change_check()   # a new clone appeared
        nudge.assert_called_once_with()


class TickTests(_StateRoot):
    def run_tick(self, *args):
        out = io.StringIO()
        with patch.object(tick, "_load_settings"), redirect_stdout(out):
            code = tick.main(["--json", *args])
        return code, json.loads(out.getvalue())

    def test_parked_tick_saves_the_reason_and_exits_0(self):
        with patch.object(poller.config, "parked_reason", return_value="no_workspace_id"):
            code, result = self.run_tick()
        self.assertEqual((code, result["result"], result["reason"]), (0, "parked", "no_workspace_id"))
        status.read_from_file()
        self.assertEqual(status.snapshot()["reason"], "no_workspace_id")

    def test_a_failed_poll_exits_1(self):
        with patch.object(poller.config, "parked_reason", return_value=None), \
                patch.object(poller.config, "workspace_id", return_value="ws"), \
                patch.object(poller, "local_repo_map", new=AsyncMock(return_value={})), \
                patch.object(poller.swarm_client, "poll_detailed", new=AsyncMock(return_value=(None, 503, False))):
            code, result = self.run_tick()
        self.assertEqual((code, result["result"]), (1, "failed"))

    def test_a_running_tick_holds_the_lock(self):
        from services.storage.layout import sharing_dir
        sharing_dir().mkdir(parents=True, exist_ok=True)
        with open(sharing_dir() / ".tick.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, result = self.run_tick()
        self.assertEqual((code, result["result"]), (3, "busy"))


if __name__ == "__main__":
    unittest.main()
