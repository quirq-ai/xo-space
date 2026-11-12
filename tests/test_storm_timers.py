"""W6a: one timer system, and leases (record design v2 §4, §5 rules 5, 6, 7
and 9, §6 invariants 4, 6, 7 and 9; w5-w7-design W6).

The timers: an ``at`` timer goes off once and its wake reaches the live
session; one into a scope the founder stopped is refused, as any wake is;
an ``every`` keeps its grid however late it fires; after a restart a missed
timer fires once, never once per missed slot. Event timers go off on a
lease freed, a process exited, a pool reopened and a task done. Leases: a
second holder is refused with who and since when; a lease ends when its
holder's process exits. No orphans: when a task or a goal ends, its timers
expire, its leases are released and its timers' queued wakes are dropped,
in that same transaction. The engine runs on the fake claude CLI with the
memory guard holding every process back, so wakes stay queued and are read
from the store; one flow test lets a coordinator's process take its timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import sqlite3
import uuid

from services.cowork_agent.storm import chats as storm_chats
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.chats import live_coordinator
from services.cowork_agent.storm.chats.conversation import Conversation
from services.cowork_agent.storm.control import founder
from services.cowork_agent.storm.engine import admission
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import ledger, timers
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.timers import TimerLoop
from services.timestamps import parse_ts
from tests.fakes import by_prompt, claude, install
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT
from tests.test_storm_chats_flows import PC, done, next_input, start
from tests.test_storm_paid import report

MINUTE = 60.0
HOUR = 3600.0


class _Timers(CommsTestCase):
    """A project with its coordinator and a task's thread; no session's
    process runs (the test calls the tools itself)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        await self.started()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.root = await self.owner_message(self.chat, "Fix the login page")
        made = await self.call(self.pc_token, "create_task", message_id=self.root["id"], title="Fix login",
                               instructions="The form posts to the wrong URL.", **CHOICE)
        self.task, self.th = made["task_id"], made["session_id"]
        self.th_token = await self.token(self.th)

    async def started(self) -> None:
        """The jobs' start ran and the timer loop runs; its passes are counted."""
        await storm_jobs.current().running()
        self.loop: TimerLoop = storm_jobs.current().timers
        self.passes = 0
        run = self.loop._pass

        async def counted() -> None:
            await run()
            self.passes += 1
        self.loop._pass = counted

    async def passed(self, seconds: float = 0.0) -> None:
        """Let ``seconds`` go by on the engine's clock, then wait for a whole
        pass of the loop, and for what it set going (an event's fires, a
        job's run), to be done."""
        before = self.passes
        self.kit.clock.advance(seconds)
        self.loop.kick()
        await self.kit.until(lambda: self.passes > before)
        await self.kit.until(lambda: not self.loop._tasks and not self.loop._opening)

    def at(self, seconds: float) -> str:
        return iso(math.floor(self.kit.clock.time()) + seconds)

    def timer(self, timer_id: str) -> timers.Timer:
        conn = sqlite3.connect(self.kit.state / "engine" / "xo.db")
        try:
            return timers.get(conn, timer_id)
        finally:
            conn.close()

    def timer_wakes(self, session_id: str | None = None) -> list[tuple[str, str, str]]:
        return [(r[0], r[1], r[2]) for r in self.rows(
            "SELECT id, session_id, state FROM wakes WHERE id LIKE 'timer:%' AND (? IS NULL OR session_id = ?) "
            "ORDER BY rowid", (session_id, session_id))]

    async def findings(self) -> list:
        return await self.kit.engine.store.read(ledger.verify)


class AtTests(_Timers):
    async def test_an_at_timer_fires_once_and_its_wake_reaches_the_threads_session(self) -> None:
        due = self.at(MINUTE)
        armed = await self.call(self.th_token, "wake_me", at=due, note="Check whether the CI build passed.")
        self.assertEqual((armed["kind"], armed["at"], armed["state"], armed["task_id"], armed["set_by"]),
                         ("at", due, "armed", self.task, self.th))
        await self.passed(MINUTE - 5)
        self.assertEqual(self.timer_wakes(), [])
        await self.passed(10)
        wake_id = f"timer:{armed['timer_id']}:{due}"
        self.assertEqual(self.timer_wakes(), [(wake_id, self.th, "queued")])
        [(text, reason)] = self.rows("SELECT envelope, reason FROM wakes WHERE id = ?", (wake_id,))
        self.assertEqual(reason, "timer")
        self.assertIn('<wake reason="timer"', text)
        self.assertIn(f'<thread ts="{self.root["id"]}">', text)
        self.assertIn(f'cause="its time came ({due})"', text)
        self.assertIn("<note>Check whether the CI build passed.</note>", text)
        fired = self.timer(armed["timer_id"])
        self.assertEqual((fired.state, fired.last), ("fired", {"at": due, "status": "woke"}))
        await self.passed(HOUR)
        self.assertEqual(len(self.timer_wakes()), 1)                     # once
        self.assertEqual(await self.findings(), [])

    async def test_a_coordinators_timer_reaches_the_chats_live_coordinator(self) -> None:
        """Every wake goes to a role in a chat (rule 4): the coordinator that
        set it was retired meanwhile, and the chat's live one is woken."""
        armed = await self.call(self.pc_token, "wake_me", at=self.at(MINUTE), note="Ask the thread how it goes.")
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, self.pc)))
        await self.passed(MINUTE + 1)
        live = await self.kit.engine.store.read(lambda conn: live_coordinator(conn, self.chat["id"]))
        self.assertNotIn(live, (None, self.pc))
        self.assertEqual([(s, state) for _w, s, state in self.timer_wakes()], [(live, "queued")])
        self.assertEqual(self.timer(armed["timer_id"]).last["status"], "woke")

    async def test_a_timer_into_a_scope_the_founder_stopped_is_refused_as_any_wake_is(self) -> None:
        due = self.at(MINUTE)
        armed = await self.call(self.th_token, "wake_me", at=due, note="Deploy at noon.")
        await founder.stop(self.kit.engine, "thread", f"{self.chat['id']}/{self.root['id']}")
        await self.passed(MINUTE + 1)
        self.assertEqual(self.timer_wakes(), [])
        [refused] = [e for e in self.commits.batches for e in e if e.type == "wake.refused"]
        self.assertEqual((refused.data["id"], refused.data["reason"]), (f"timer:{armed['timer_id']}:{due}", "stopped"))
        fired = self.timer(armed["timer_id"])
        self.assertEqual((fired.state, fired.last["status"]), ("fired", "refused"))
        self.assertTrue(fired.last["reason"].startswith("Stopped by the founder at "), fired.last)

    async def test_wake_me_is_checked_retried_alike_and_cancelled_by_its_own_role_only(self) -> None:
        self.assertIn("in the future", await self.refused(self.th_token, "wake_me", at=self.at(-5), note="x"))
        self.assertIn("either at", await self.refused(self.th_token, "wake_me", note="x"))
        self.assertIn("either at", await self.refused(self.th_token, "wake_me", at=self.at(9), on="task_done",
                                                      about=self.task, note="x"))
        self.assertIn("goal_id is a coordinator's", await self.refused(self.th_token, "wake_me", at=self.at(9),
                                                                       note="x", goal_id="goal_1"))
        first = await self.call(self.th_token, "wake_me", at=self.at(HOUR), note="Look again.")
        again = await self.call(self.th_token, "wake_me", at=self.at(HOUR), note="Look again.")    # invariant 7
        self.assertEqual(first["timer_id"], again["timer_id"])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'timer'"), [(1,)])
        listed = await self.call(self.th_token, "read_state")
        self.assertEqual([t["timer_id"] for t in listed["timers"]], [first["timer_id"]])
        self.assertEqual((await self.call(self.pc_token, "read_state"))["timers"], [])
        self.assertIn("no timer", await self.refused(self.pc_token, "cancel_wake", timer_id=first["timer_id"]))
        cancelled = await self.call(self.th_token, "cancel_wake", timer_id=first["timer_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIn("already cancelled", await self.refused(self.th_token, "cancel_wake",
                                                              timer_id=first["timer_id"]))
        self.assertEqual((await self.call(self.th_token, "read_state"))["timers"], [])
        self.assertEqual(await self.findings(), [])


class EveryTests(_Timers):
    async def test_an_every_timer_rearms_on_its_grid_without_drift(self) -> None:
        start = parse_ts(self.at(HOUR)).timestamp()
        job = await storm_jobs.current().save({"kind": "agent", "name": "Hourly check", "chat": self.chat["id"],
                                               "prompt": "Check the queue.", "every_seconds": int(HOUR),
                                               "first_run_at": iso(start)})
        self.assertEqual(job["next_run"], iso(start))
        await self.passed(start + 7 - self.kit.clock.time())               # the loop runs seven seconds late
        first = self.timer(job["id"])
        self.assertEqual((first.last["at"], first.last["status"], first.due_at), (iso(start), "ok", iso(start + HOUR)))
        await self.passed(start + HOUR * 2.5 - self.kit.clock.time())      # 1.5 slots late, one fire
        second = self.timer(job["id"])
        self.assertEqual((second.last["at"], second.due_at, second.state),
                         (iso(start + 2 * HOUR), iso(start + 3 * HOUR), "armed"))
        self.assertEqual(second.last["status"], "skipped")                  # the first run still works
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'timer' AND "
                                   "json_extract(data, '$.action') = 'fire'"), [(2,)])


class RestartTests(_Timers):
    async def restart(self, down: float) -> None:
        """The server stops, stays down for ``down`` seconds, and starts again."""
        await storm_jobs.shutdown()
        await storm_chats.shutdown()
        await self.queue.stop()
        await storm_engine.shutdown()
        self.kit.clock.advance(down)
        self.queue = ReviewQueue(Conversation())
        engine = await self.kit.boot(desk=self.queue, listeners=[self.commits])
        await self.queue.start(engine)
        self.chats = storm_chats.boot(engine, self.queue)
        storm_jobs.boot(self.chats)
        await self.started()
        await self.passed()

    async def test_after_a_restart_a_missed_timer_fires_once_never_once_per_missed_slot(self) -> None:
        due = self.at(MINUTE)
        once = await self.call(self.th_token, "wake_me", at=due, note="Check the deploy.")
        start = parse_ts(self.at(HOUR)).timestamp()
        job = await storm_jobs.current().save({"kind": "agent", "name": "Hourly check", "chat": self.chat["id"],
                                               "prompt": "Check the queue.", "every_seconds": int(HOUR),
                                               "first_run_at": iso(start)})
        await self.restart(10 * HOUR)                                      # ten of its slots went by
        self.assertEqual([w for w, _s, _state in self.timer_wakes()], [f"timer:{once['timer_id']}:{due}"])
        hourly = self.timer(job["id"])
        latest = start + 9 * HOUR
        self.assertEqual((hourly.last["at"], hourly.last["status"], hourly.due_at),
                         (iso(latest), "ok", iso(latest + HOUR)))
        self.assertEqual(self.rows("SELECT count(*) FROM threads WHERE title = 'Hourly check'"), [(1,)])
        await self.restart(MINUTE)                                         # and nothing again
        self.assertEqual(len(self.timer_wakes()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM threads WHERE title = 'Hourly check'"), [(1,)])
        self.assertEqual(await self.findings(), [])


class EventTests(_Timers):
    async def test_a_lease_freed_wakes_who_waits_for_it(self) -> None:
        await self.call(self.th_token, "lease", resource="browser-seat-1")
        armed = await self.call(self.pc_token, "wake_me", on="lease_freed", about="browser-seat-1",
                                note="The browser is free: start the screenshot thread.")
        self.assertEqual(armed["on"], "lease_freed:browser-seat-1")
        await self.call(self.th_token, "release", resource="browser-seat-1")
        await self.passed()
        [(wake_id, session, _state)] = self.timer_wakes()
        self.assertEqual((wake_id.rsplit(":", 1)[0].startswith(f"timer:{armed['timer_id']}"), session), (True, self.pc))
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id = ?", (wake_id,))
        self.assertIn("the lease on browser-seat-1 ended (released)", text)
        self.assertIn("Nobody holds browser-seat-2", await self.refused(self.pc_token, "wake_me", on="lease_freed",
                                                                        about="browser-seat-2", note="x"))

    async def test_a_process_exited_wakes_who_waits_for_it_and_ends_the_leases_it_held(self) -> None:
        proc = await asyncio.create_subprocess_exec("sleep", "60")
        self.addAsyncCleanup(_reaped, proc)
        armed = await self.call(self.th_token, "wake_me", on="process_exited", about=str(proc.pid),
                                note="The build finished: read its log.")
        held = await self.call(self.th_token, "lease", resource="port-5173", pid=proc.pid)
        await self.passed()
        self.assertEqual(self.timer_wakes(), [])
        proc.send_signal(signal.SIGTERM)
        await proc.wait()
        await self.kit.until(lambda: self.timer(armed["timer_id"]).state == "fired")
        self.assertEqual([s for _w, s, _state in self.timer_wakes()], [self.th])
        self.assertEqual(self.rows("SELECT released_why FROM leases WHERE id = ?", (held["lease_id"],)),
                         [("process_exited",)])
        self.assertIn("No process 999999999 is running", await self.refused(
            self.th_token, "wake_me", on="process_exited", about="999999999", note="x"))

    async def test_a_pool_reopening_wakes_who_waits_for_it(self) -> None:
        engine = self.kit.engine
        self.assertIn("isn't holding work", await self.refused(self.pc_token, "wake_me", on="pool_reopened",
                                                               note="x"))
        resets = math.floor(self.kit.clock.time() // 60) * 60 + HOUR
        engine.advisory.record_usage(AGENT, None, report(None, resets_at=resets, window="five_hour",
                                                         status="limited", possible=False))
        await self.kit.until(lambda: engine.pool(AGENT, None).state in admission.HOLDS)
        await self.kit.until(lambda: self.rows("SELECT state FROM pools") == [("closed",)])
        armed = await self.call(self.pc_token, "wake_me", on="pool_reopened", note="Restart the paused work.")
        self.assertTrue(armed["on"].startswith("pool_reopened:"), armed)
        await self.passed(resets + 1 - self.kit.clock.time())
        await self.kit.until(lambda: self.timer(armed["timer_id"]).state == "fired")
        self.assertEqual([s for _w, s, _state in self.timer_wakes()], [self.pc])

    async def test_a_task_done_wakes_its_coordinator_waiting_for_it(self) -> None:
        armed = await self.call(self.pc_token, "wake_me", on="task_done", about=self.task,
                                note="Review the login fix.")
        await self.call(self.th_token, "submit_result", summary="Login posts to /api/login.")
        await self.passed()
        [(wake_id, session, _state)] = self.timer_wakes()
        self.assertEqual(session, self.pc)
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id = ?", (wake_id,))
        self.assertIn(f"task {self.task} ended: done", text)
        self.assertEqual(self.timer(armed["timer_id"]).state, "fired")
        self.assertIn("already ended", await self.refused(self.pc_token, "wake_me", on="task_done", about=self.task,
                                                          note="x"))


class LeaseTests(_Timers):
    async def test_a_second_holder_is_refused_with_who_holds_it_and_since_when(self) -> None:
        held = await self.call(self.th_token, "lease", resource="browser-seat-1", seconds=600)
        self.assertEqual((held["resource"], held["until"]), ("browser-seat-1", iso(parse_ts(held["since"])
                                                                                   .timestamp() + 600)))
        refused = await self.refused(self.pc_token, "lease", resource="browser-seat-1")
        self.assertIn(f"browser-seat-1 is held by session {self.th} since {held['since']}", refused)
        self.assertIn("wake_me with on lease_freed", refused)
        again = await self.call(self.th_token, "lease", resource="browser-seat-1")     # the holder gets it back
        self.assertEqual(again["lease_id"], held["lease_id"])
        with self.assertRaises(sqlite3.IntegrityError):                   # invariant 4, in the store itself
            self.kit.execute("INSERT INTO leases (id, resource, holder_session, acquired_at) VALUES "
                             "('lse_x', 'browser-seat-1', ?, '2026-11-12T00:00:00Z')", (self.pc,))
        self.assertIn("You hold no lease", await self.refused(self.pc_token, "release", resource="browser-seat-1"))
        await self.passed(601)                                             # its time is up
        self.assertEqual(self.rows("SELECT released_why FROM leases"), [("expired",)])
        await self.call(self.pc_token, "lease", resource="browser-seat-1")
        self.assertEqual([lease["resource"] for lease in (await self.call(self.pc_token, "read_state"))["leases"]],
                         ["browser-seat-1"])
        self.assertEqual(await self.findings(), [])

    async def test_a_lease_is_released_when_its_holder_process_exits(self) -> None:
        proc = await asyncio.create_subprocess_exec("sleep", "60")
        self.addAsyncCleanup(_reaped, proc)
        held = await self.call(self.th_token, "lease", resource="port-5173", pid=proc.pid)
        self.assertEqual(held["pid"], proc.pid)
        await self.passed()
        self.assertEqual(self.rows("SELECT released_at FROM leases"), [(None,)])
        proc.kill()
        await proc.wait()
        await self.kit.until(lambda: self.rows("SELECT released_why FROM leases") == [("process_exited",)])
        await self.call(self.pc_token, "lease", resource="port-5173")


class OrphanTests(_Timers):
    async def test_when_a_task_ends_its_timers_leases_and_timer_wakes_go_with_it(self) -> None:
        fired = await self.call(self.th_token, "wake_me", at=self.at(MINUTE), note="First look.")
        await self.passed(MINUTE + 1)
        [(fired_wake, _s, _state)] = self.timer_wakes()                    # queued, not taken yet
        armed = await self.call(self.th_token, "wake_me", at=self.at(HOUR), note="Second look.")
        held = await self.call(self.th_token, "lease", resource="browser-seat-1")
        owner = await self.owner_message(self.chat, "Also check the signup page.", thread_ts=self.root["id"])
        await self.kit.engine.deliver(self.th, storm_engine.Wake(id=f"msg:{owner['id']}", text="Also check.",
                                                                 origin="person", reason="message"))
        await self.call(self.th_token, "submit_result", summary="Login posts to /api/login.")
        [batch] = [b for b in self.commits.batches if any(e.type == "result" for e in b)]
        self.assertEqual([(e.type, e.data.get("action") or e.data.get("reason")) for e in batch
                          if e.type in ("timer", "lease", "wake.dropped")],
                         [("timer", "expired"), ("wake.dropped", "task_ended"), ("lease", "release")])
        self.assertEqual(self.timer(armed["timer_id"]).state, "expired")
        self.assertEqual(self.timer(fired["timer_id"]).state, "fired")
        self.assertEqual(self.rows("SELECT id, state FROM wakes WHERE session_id = ? ORDER BY id", (self.th,)),
                         sorted([(f"msg:{owner['id']}", "queued"), (fired_wake, "dropped"),
                                 *[(w, s) for w, s in self.rows("SELECT id, state FROM wakes WHERE session_id = ? "
                                                                "AND id LIKE 'deleg:%'", (self.th,))]]))
        self.assertEqual(self.rows("SELECT released_why FROM leases WHERE id = ?", (held["lease_id"],)),
                         [("task_ended",)])
        await self.passed(2 * HOUR)
        self.assertEqual(len(self.timer_wakes()), 1)                      # the expired timer never goes off
        self.assertEqual(await self.findings(), [])

    async def test_when_a_goal_ends_its_timers_expire(self) -> None:
        goal = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login works",
                               ends_at=self.at(5 * HOUR))
        armed = await self.call(self.pc_token, "wake_me", at=self.at(HOUR), note="Check the goal.",
                                goal_id=goal["goal_id"])
        self.assertEqual(armed["goal_id"], goal["goal_id"])
        await self.call(self.pc_token, "set_goal", goal_id=goal["goal_id"], state="done", reason="Shipped.")
        self.assertEqual(self.timer(armed["timer_id"]).state, "expired")
        self.assertIn(f"its goal {goal['goal_id']} ended (done)", self.timer(armed["timer_id"]).state_reason)


class FlowTests(CommsTestCase):
    async def test_a_coordinators_timer_wakes_its_live_process_with_its_note(self) -> None:
        due = iso(math.floor(self.kit.clock.time()) + 2 * MINUTE)
        roles = {PC: [*start("pc"), claude.mcp_call("wake_me", {"at": due, "note": "Check on the build."}),
                      claude.mcp_call("no_reply_needed", {"reason": "set a timer"}), *done("pc"), next_input(),
                      claude.mcp_call("no_reply_needed", {"reason": "ok"}), *done("pc", 2)]}
        self.kit.fake = install("claude", self.kit.root / "bin", by_prompt(
            roles, argv=[claude.version(), claude.auth_status()], probe=(claude.PROBE_FLAG, claude.initialize())))
        os.environ["CLAUDE_CLI_PATH"] = str(self.kit.fake.path)
        engine = await self.kit.boot(desk=self.queue, listeners=[self.commits])
        await self.queue.start(engine)
        self.chats = storm_chats.boot(engine, self.queue)
        storm_jobs.boot(self.chats)
        await storm_jobs.current().running()
        chat = await self.project()
        response = await self.http.post(f"/api/chat/chats/{chat['id']}/messages",
                                        json={"text": "Watch the build", "client_id": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.kit.pacing():
            await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0]
                                 == 2, 30)
        [run] = self.kit.runs()                                           # the same process took both inputs
        [second] = [json.dumps(s) for s in run["stdin"] if "reason=\\\"timer\\\"" in json.dumps(s)]
        self.assertIn("Check on the build.", second)
        self.assertEqual(self.rows("SELECT state FROM timers"), [("fired",)])


async def _reaped(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
