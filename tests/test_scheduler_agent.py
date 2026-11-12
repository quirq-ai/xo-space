"""The scheduler's ``agent`` kind (storm design ch 10 §4.5, ch 12 §9, ch 17
§2.8), with a runner the test controls: what storm registers at its boot is
tested in ``tests/test_storm_jobs.py``.

Hermetic like ``tests/test_scheduler.py``: a temp state root and an injected
``now``. A fire's outcome is a ``concurrent.futures.Future`` the test settles.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from utils.commands import scheduler

T0 = datetime(2026, 10, 21, 3, 30, 0, tzinfo=timezone.utc)
DAY = 86400


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _agent(name: str = "Morning triage", every: int | None = DAY, **fields) -> dict:
    return {"kind": "agent", "name": name, "chat": "chat_0123456789abcdef", "prompt": "Triage new issues.",
            "every_seconds": every, **fields}


class Runner:
    """Records each fire handed over; the test settles its future."""

    def __init__(self, state_file: Path) -> None:
        self.fires: list[tuple[str, str, str]] = []
        self.futures: list[Future] = []
        self.state_seen: list[dict] = []
        self._state_file = state_file

    def __call__(self, job, fire_at: str, trigger: str) -> Future:
        # What state.json said when the fire was handed over (it must already hold it).
        self.state_seen.append(json.loads(self._state_file.read_text())["jobs"][job["id"]])
        self.fires.append((job["id"], fire_at, trigger))
        future: Future = Future()
        self.futures.append(future)
        return future


class AgentKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self._tmp.name}, clear=False)
        self._env.start()
        scheduler.reset_state()
        self.runner = Runner(scheduler.state_file())
        scheduler.set_agent_runner(self.runner)

    def tearDown(self) -> None:
        scheduler.reset_state()
        self._env.stop()
        self._tmp.cleanup()

    def entry(self, job_id: str) -> dict:
        return json.loads(scheduler.state_file().read_text())["jobs"][job_id]

    # ── definitions ──────────────────────────────────────────────────────

    def test_a_command_job_is_unchanged_and_kind_command_is_accepted(self) -> None:
        command = {"name": "c", "command": {"argv": ["true"], "timeout": 5}, "every_seconds": 60}
        plain = scheduler.create_job(command, now=T0)
        explicit = scheduler.create_job({**command, "kind": "command"}, now=T0)
        for job in (plain, explicit):
            self.assertNotIn("kind", job)
            self.assertEqual(set(job) - {"id"}, {"name", "description", "project_id", "command", "every_seconds",
                                                 "first_run_at", "enabled", "created_at", "updated_at", "next_run",
                                                 "last_run", "running_since", "last_result", "running"})
        with self.assertRaisesRegex(ValueError, 'kind must be "command" or "agent"'):
            scheduler.create_job({**command, "kind": "shell"})
        with self.assertRaisesRegex(ValueError, "belong to agent jobs"):
            scheduler.create_job(command, approved_by={"review_id": "rv_1"})

    def test_an_agent_job_holds_its_chat_prompt_and_choices_and_keeps_the_owners_offset(self) -> None:
        job = scheduler.create_job(_agent(first_run_at="2026-10-22T09:00:00+05:30", model="m", effort="high"),
                                   now=T0)
        self.assertEqual((job["kind"], job["chat"], job["prompt"], job["model"], job["effort"], job["agent"]),
                         ("agent", "chat_0123456789abcdef", "Triage new issues.", "m", "high", None))
        self.assertEqual((job["first_run_at"], job["utc_offset"]), ("2026-10-22T03:30:00Z", "+05:30"))
        self.assertEqual((job["created_by"], job["approved_by"]), ({"kind": "person"}, None))
        for bad, why in (({"chat": ""}, "chat is required"), ({"prompt": " "}, "prompt is required"),
                         ({"prompt": "x" * (scheduler.PROMPT_MAX + 1)}, "at most"),
                         ({"command": {"argv": ["true"], "timeout": 5}}, "unknown job keys"),
                         ({"approved_by": {"review_id": "rv_x"}}, "unknown job keys"),
                         ({"effort": 3}, "effort must be"), ({"every_seconds": 0}, "positive integer")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, why):
                scheduler.validate_definition({**_agent(), **bad})

    def test_an_interval_below_the_tick_is_refused(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "30"}):
            self.assertEqual(scheduler.min_every_seconds(), 30)
            with self.assertRaisesRegex(ValueError, "shorter than the watcher tick"):
                scheduler.validate_definition(_agent(every=29))
            scheduler.validate_definition(_agent(every=30))

    def test_registering_an_allowed_item_twice_makes_one_job(self) -> None:
        by = {"kind": "agent", "session_id": "cse_1", "role": "thread"}
        first = scheduler.create_job(_agent(), now=T0, created_by=by, approved_by={"review_id": "rv_1"})
        again = scheduler.create_job(_agent(), now=T0, created_by=by, approved_by={"review_id": "rv_1"})
        other = scheduler.create_job(_agent(), now=T0, created_by=by, approved_by={"review_id": "rv_2"})
        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(len(scheduler.list_jobs()), 2)
        self.assertEqual((first["created_by"], first["approved_by"]), (by, {"review_id": "rv_1"}))
        with self.assertRaisesRegex(ValueError, "approved_by is"):
            scheduler.create_job(_agent(), approved_by={"review_id": ""})

    def test_disabling_records_who_and_reenabling_drops_it(self) -> None:
        job = scheduler.create_job(_agent(first_run_at="2026-10-22T09:00:00+05:30"), now=T0)
        by = {"kind": "agent", "session_id": "cse_1", "role": "thread", "reason": "no longer needed"}
        off = scheduler.disable_job(job["id"], by=by, now=_at(10))
        self.assertFalse(off["enabled"])
        self.assertEqual(off["disabled_by"], {**by, "at": scheduler.stamp(_at(10))})
        again = scheduler.disable_job(job["id"], by={**by, "reason": "later"}, now=_at(20))
        self.assertEqual(again["disabled_by"]["reason"], "no longer needed")        # the first record stays
        self.assertEqual(scheduler.tick(_at(DAY * 3)).started, [])                  # disabled: never fires
        on = scheduler.update_job(job["id"], _agent(first_run_at="2026-10-22T09:00:00+05:30"), now=_at(30))
        self.assertTrue(on["enabled"])
        self.assertNotIn("disabled_by", on)
        self.assertEqual(on["created_by"], {"kind": "person"})

    def test_a_change_of_kind_keeps_nothing_of_the_old_kind(self) -> None:
        job = scheduler.create_job(_agent(), now=T0, created_by={"kind": "agent"}, approved_by={"review_id": "rv_1"})
        command = {"name": "c", "command": {"argv": ["true"], "timeout": 5}, "every_seconds": 60}
        changed = scheduler.update_job(job["id"], command, now=T0)
        self.assertFalse({"kind", "chat", "prompt", "created_by", "approved_by", "utc_offset"} & set(changed))
        back = scheduler.update_job(job["id"], _agent(), now=T0)
        self.assertFalse({"command", "project_id"} & set(back))
        self.assertEqual((back["created_by"], back["approved_by"]), ({"kind": "person"}, None))

    # ── fires ────────────────────────────────────────────────────────────

    def test_a_due_fire_is_on_disk_before_it_is_handed_over_and_a_duplicate_tick_hands_nothing(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        report = scheduler.tick(_at(60))
        self.assertEqual(report.started, [job["id"]])
        fire = scheduler.stamp(_at(60))
        self.assertEqual(self.runner.fires, [(job["id"], fire, "schedule")])
        self.assertEqual(self.runner.state_seen[0]["fire"], {"at": fire, "trigger": "schedule"})
        self.assertEqual(scheduler.tick(_at(60)).started, [])
        self.assertEqual(len(self.runner.fires), 1)

    def test_missed_slots_collapse_into_the_latest_one(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        scheduler.tick(_at(60 + 3 * DAY + 5))
        self.assertEqual(self.runner.fires, [(job["id"], scheduler.stamp(_at(60 + 3 * DAY)), "schedule")])
        self.assertEqual(self.entry(job["id"])["next_run"], scheduler.stamp(_at(60 + 4 * DAY)))

    def test_the_outcome_is_the_run_record_with_its_thread(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        scheduler.tick(_at(60))
        self.assertTrue(scheduler.get_job(job["id"])["running"])
        self.runner.futures[0].set_result({"status": "ok", "chat_id": "chat_0123456789abcdef",
                                           "thread_ts": "cmsg_1", "ignored": "x"})
        self.assertEqual(scheduler.tick(_at(61)).finished, [job["id"]])
        [record] = scheduler.list_runs(job["id"])
        self.assertEqual({k: record[k] for k in ("trigger", "fire_at", "status", "chat_id", "thread_ts")},
                         {"trigger": "schedule", "fire_at": scheduler.stamp(_at(60)), "status": "ok",
                          "chat_id": "chat_0123456789abcdef", "thread_ts": "cmsg_1"})
        self.assertNotIn("ignored", record)
        entry = self.entry(job["id"])
        self.assertEqual((entry["running_since"], entry.get("fire")), (None, None))

    def test_a_run_that_could_not_open_says_why(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        scheduler.tick(_at(60))
        self.runner.futures[0].set_result({"status": "failed", "code": "chat_retired", "reason": "removed"})
        scheduler.tick(_at(61))
        [record] = scheduler.list_runs(job["id"])
        self.assertEqual((record["status"], record["code"], record["reason"]), ("failed", "chat_retired", "removed"))

    def test_until_storm_registers_its_runner_a_due_job_waits(self) -> None:
        scheduler.reset_state()                      # no runner: storm boots after the watcher's first tick
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        once = scheduler.create_job(_agent("Once", every=None, first_run_at=scheduler.stamp(_at(60))), now=T0)
        report = scheduler.tick(_at(60))
        self.assertEqual(sorted(report.deferred), sorted([job["id"], once["id"]]))
        self.assertEqual(self.entry(job["id"])["next_run"], scheduler.stamp(_at(60)))
        with self.assertRaises(scheduler.AgentJobsUnavailable):
            scheduler.run_now(job["id"], now=_at(60))
        scheduler.set_agent_runner(self.runner)
        self.assertEqual(sorted(scheduler.tick(_at(61)).started), sorted([job["id"], once["id"]]))

    def test_a_fire_cut_short_by_a_restart_is_handed_over_again_never_lost(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        scheduler.tick(_at(60))
        scheduler.reset_state()                      # the server stopped before the run was harvested
        report = scheduler.tick(_at(65))             # storm not up yet: the fire waits, it isn't "lost"
        self.assertEqual((report.lost, report.deferred), ([], [job["id"]]))
        self.assertEqual(scheduler.list_runs(job["id"]), [])
        scheduler.set_agent_runner(self.runner)
        self.assertEqual(scheduler.tick(_at(66)).started, [job["id"]])
        self.assertEqual(self.runner.fires, [(job["id"], scheduler.stamp(_at(60)), "schedule")] * 2)

    def test_a_fire_cancelled_at_shutdown_goes_again_with_the_same_time(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)
        scheduler.tick(_at(60))
        self.runner.futures[0].cancel()
        scheduler.tick(_at(61))
        self.assertEqual(scheduler.list_runs(job["id"]), [])
        self.assertEqual(self.runner.fires[-1], (job["id"], scheduler.stamp(_at(60)), "schedule"))
        self.assertEqual(len(self.runner.fires), 2)

    def test_run_now_is_a_manual_fire_single_flight_and_not_held_by_the_command_cap(self) -> None:
        job = scheduler.create_job(_agent(), now=T0)
        blocker = Path(self._tmp.name) / "go"
        code = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n"
        command = scheduler.create_job({"name": "busy", "command": {"argv": [sys.executable, "-c", code,
                                                                             str(blocker)], "timeout": 30}}, now=T0)
        with patch.dict(os.environ, {"XO_SCHEDULER_MAX_CONCURRENT": "1"}):
            try:
                scheduler.run_now(command["id"], now=_at(1))
                view = scheduler.run_now(job["id"], now=_at(5))     # the one command slot is taken
                self.assertTrue(view["running"])
                self.assertEqual(self.runner.fires, [(job["id"], scheduler.stamp(_at(5)), "manual")])
                with self.assertRaises(scheduler.JobRunningError):
                    scheduler.run_now(job["id"], now=_at(6))
            finally:
                blocker.write_text("go")
                scheduler._running[command["id"]].thread.join(10)

    def test_a_pending_fire_after_a_restart_blocks_a_manual_run(self) -> None:
        job = scheduler.create_job(_agent(), now=T0)
        scheduler.run_now(job["id"], now=_at(5))
        runner = self.runner
        scheduler.reset_state()
        with self.assertRaises(scheduler.AgentJobsUnavailable):         # storm isn't up: that is the answer
            scheduler.run_now(job["id"], now=_at(6))
        scheduler.set_agent_runner(runner)
        with self.assertRaises(scheduler.JobRunningError):
            scheduler.run_now(job["id"], now=_at(6))

    def test_a_fire_storms_loop_refuses_stays_and_goes_again(self) -> None:
        job = scheduler.create_job(_agent(first_run_at=scheduler.stamp(_at(60))), now=T0)

        def stopping(job, fire_at, trigger):
            raise RuntimeError("Event loop is closed")
        scheduler.set_agent_runner(stopping)
        report = scheduler.tick(_at(60))
        self.assertTrue(report.errors and "could not hand its run to storm" in report.errors[0])
        self.assertEqual(self.entry(job["id"])["fire"], {"at": scheduler.stamp(_at(60)), "trigger": "schedule"})
        scheduler.set_agent_runner(self.runner)
        self.assertEqual(scheduler.tick(_at(61)).started, [job["id"]])
        self.assertEqual(self.runner.fires, [(job["id"], scheduler.stamp(_at(60)), "schedule")])

    def test_a_persons_new_prompt_is_no_longer_the_allowed_one(self) -> None:
        job = scheduler.create_job(_agent(), now=T0, created_by={"kind": "agent"}, approved_by={"review_id": "rv_1"})
        same = scheduler.update_job(job["id"], _agent(every=2 * DAY), now=T0)
        self.assertEqual(same["approved_by"], {"review_id": "rv_1"})
        changed = scheduler.update_job(job["id"], _agent(prompt="Triage and label."), now=T0)
        self.assertEqual((changed["approved_by"], changed["created_by"]), (None, {"kind": "agent"}))


class AgentRouteTests(unittest.TestCase):
    """``POST /api/schedules/{id}/run`` on an agent job needs storm."""

    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from routers.schedules import router
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self._tmp.name}, clear=False)
        self._env.start()
        scheduler.reset_state()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, client=("127.0.0.1", 12345))

    def tearDown(self) -> None:
        scheduler.reset_state()
        self._env.stop()
        self._tmp.cleanup()

    def test_running_an_agent_job_without_storm_is_503_and_saving_one_needs_storm(self) -> None:
        job = scheduler.create_job(_agent(), now=T0)
        response = self.client.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(response.status_code, 503)
        self.assertIn("storm", response.json()["detail"])
        saved = self.client.post("/api/schedules", json=_agent())
        self.assertEqual(saved.status_code, 503)
        self.assertEqual(saved.json()["detail"]["code"], "engine_not_running")


if __name__ == "__main__":
    unittest.main()
