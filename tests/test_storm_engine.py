"""Storm's session engine on the fake claude CLI (design ch 07; ch 18 §2's
proof for PR 7): one turn end to end, idle release at 10 minutes, Stop, hold
and answer, park and wake, epochs fencing a stale process, boot recovery, wake
idempotency and batching, the admission gates."""

from __future__ import annotations

import asyncio
import json
import unittest
import unittest.mock

from services.cowork_agent.adapters import contract as c
from tests.fakes import claude, expect
from services.cowork_agent.storm.engine import Decision, Wake, WouldStopRuns
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.engine.memory import MemoryReading
from tests.storm_engine_kit import AGENT, SID, Desk, Kit, message


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


class TurnTests(EngineTestCase):
    async def test_one_turn_end_to_end(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi there"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()

        self.assertTrue(await engine.deliver(sid, message(1, "hello")))
        finished = await kit.recorder.seen("turn.finished", session_id=sid)

        # The wake was consumed in the turn-end transaction itself.
        self.assertEqual(finished.data["wakes"], ["msg:cmsg_1"])
        self.assertEqual(finished.data["status"], "finished")
        self.assertEqual(kit.rows("SELECT state, turn_id FROM wakes"), [("consumed", finished.turn_id)])
        (turn,) = kit.rows("SELECT status, origin, steps, tool_calls, wall_ms IS NOT NULL, cost_source, "
                           "json_extract(usage, '$.input') FROM turns")
        self.assertEqual(turn, ("finished", "person", 1, 0, 1, "reported", 10))
        # Spawned through the engine's launcher, registered while it ran.
        (run,) = kit.fake.invocations()
        self.assertEqual(run["stdin"][1]["message"]["content"][0]["text"], "hello")
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)   # attached, then idle
        self.assertEqual(len(kit.rows("SELECT pid FROM procs WHERE session_id = ?", (sid,))), 1)
        view = await engine.get_session(sid)
        self.assertEqual((view["state"], view["native_id"] is not None, view["epoch"]), ("warm", True, 1))
        # The state log, in seq order, in one vocabulary.
        types = kit.recorder.types(sid)
        self.assertEqual(types[:3], ["session", "session", "session"])        # created, starting, attached
        self.assertEqual([t for t in types if t.startswith("turn.")], ["turn.started", "turn.finished"])
        self.assertIn("step.text", types)

    async def test_a_wake_is_delivered_once_and_waiting_wakes_share_one_turn_in_order(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"sleep": 1.0}, *claude.text_turn(SID, "one"), claude.idle(SID),
                                  claude.user_message(), *claude.text_turn(SID, "two", message_id="m2"),
                                  claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "first"))
        await kit.recorder.seen("turn.started", session_id=sid)
        # While the turn runs: two more inputs, one of them delivered twice.
        self.assertTrue(await engine.deliver(sid, message(2, "second")))
        self.assertTrue(await engine.deliver(sid, message(3, "third")))
        self.assertFalse(await engine.deliver(sid, message(2, "second, again")))
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(second.data["wakes"], ["msg:cmsg_2", "msg:cmsg_3"])
        (run,) = kit.fake.invocations()                          # one process for both turns
        user = [line for line in run["stdin"] if line.get("type") == "user"]
        self.assertEqual([u["message"]["content"][0]["text"] for u in user], ["first", "second\n\nthird"])
        self.assertEqual(kit.rows("SELECT id, state FROM wakes ORDER BY rowid"),
                         [("msg:cmsg_1", "consumed"), ("msg:cmsg_2", "consumed"), ("msg:cmsg_3", "consumed")])

    async def test_add_joins_the_running_turn_where_the_runtime_steers(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.user_message(), *claude.text_turn(SID, "both"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "first"))
        started = await kit.recorder.seen("turn.started", session_id=sid)
        await engine.deliver(sid, message(2, "and this", delivery="add"))
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(finished.turn_id, started.turn_id)
        self.assertEqual(finished.data["wakes"], ["msg:cmsg_1", "msg:cmsg_2"])
        self.assertEqual(len(kit.rows("SELECT id FROM turns")), 1)


class IdleTests(EngineTestCase):
    async def test_an_idle_process_is_released_after_ten_minutes_and_resumed_on_the_next_wake(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        native = (await engine.get_session(sid))["native_id"]
        await kit.idle_timer()
        kit.clock.advance(599)
        await asyncio.sleep(0.05)
        self.assertEqual((await engine.get_session(sid))["state"], "warm")
        kit.clock.advance(1)
        released = await kit.recorder.seen("session", session_id=sid, status="cold", count=2)
        self.assertEqual(released.data["reason"], "idle")
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))

        await engine.deliver(sid, message(2))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        first, second = kit.fake.invocations()
        self.assertNotIn("--resume", first["argv"])
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], native)
        self.assertEqual((await engine.get_session(sid))["epoch"], 2)

    async def test_a_live_sub_agent_keeps_the_session_busy(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"),
                                  claude.idle(SID), {"sleep": 1.0},
                                  claude.system(SID, "task_notification", task_id="task-1", status="completed",
                                                summary="done")))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        kit.clock.advance(3600)
        await asyncio.sleep(0.05)
        self.assertEqual((await engine.get_session(sid))["state"], "running")     # the run still works
        await kit.recorder.seen("run.finished", session_id=sid)
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        await kit.idle_timer()
        kit.clock.advance(600)
        await kit.recorder.seen("session", session_id=sid, status="cold", count=2)


class StopTests(EngineTestCase):
    async def test_stop_interrupts_the_turn_and_records_what_ran_last(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "make test"}),
                                  *claude.host_control("interrupt"),
                                  claude.result(SID, "", subtype="error_during_execution",
                                                terminal_reason="aborted_tools"),
                                  claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("step.tool", session_id=sid)
        result = await engine.stop(sid, by="person")
        stopped = await kit.recorder.seen("turn.stopped", session_id=sid)
        self.assertEqual((result.stopped, result.turn_id), (True, stopped.turn_id))
        self.assertEqual(stopped.data["by"], "person")
        self.assertTrue(stopped.data["last_step"])
        self.assertEqual(kit.rows("SELECT status FROM turns"), [("stopped",)])
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertIn("stopping", [e.data["status"] for e in kit.recorder.of("session", sid)])
        self.assertEqual((await engine.stop(sid, by="person")).stopped, False)    # nothing runs now

    async def test_a_turn_that_ignores_the_interrupt_loses_its_process_group(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "sleep 100"}),
                                  expect({"type": "control_request", "request": {"subtype": "interrupt"}}),
                                  {"sleep": 60}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("step.tool", session_id=sid)
        with unittest.mock.patch("services.cowork_agent.storm.engine.session.STOP_GRACE_S", 0.5):
            await engine.stop(sid, by="person")
        await kit.recorder.seen("turn.stopped", session_id=sid)
        closed = await kit.recorder.seen("session", session_id=sid, status="cold", count=2)
        self.assertEqual(closed.data["reason"], "interrupted")
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))


PUSH = {"command": "git push origin main"}


class HoldParkTests(EngineTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.desk = Desk(loop=asyncio.get_running_loop())

    async def test_a_call_is_held_until_the_person_answers(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
                                  claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "pushed"),
                                  *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await kit.boot(desk=self.desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        opened = await kit.recorder.seen("review.opened", session_id=sid)
        waiting = await kit.recorder.seen("session", session_id=sid, status="waiting_on_person")
        self.assertEqual(waiting.seq, opened.seq + 1)                 # one transaction
        (item, ask), = self.desk.opened
        self.assertEqual((ask.kind, ask.request.command, ask.call.tool), ("permission", "git push origin main",
                                                                          "Bash"))
        # Held as long as it takes: an hour later, nothing has moved.
        kit.clock.advance(3600)
        await asyncio.sleep(0.1)
        self.assertEqual((await engine.get_session(sid))["state"], "waiting_on_person")
        self.assertEqual(self.desk.parked, [])

        delivery = await engine.deliver_decision(item, Decision(allow=True), ask.call,
                                                 self.desk.record(item, "allow"))
        self.assertEqual(delivery, "in_call")
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertGreaterEqual(finished.data["wait_person_ms"], 3_600_000)
        self.assertEqual(kit.rows("SELECT state, delivery FROM review_items"), [("decided", "in_call")])
        self.assertEqual(kit.rows("SELECT count(*) FROM grants"), [(0,)])

    async def test_routine_work_is_allowed_without_asking(self) -> None:
        kit = self.kit
        (kit.work / "a.txt").write_text("x")
        read = {"file_path": str(kit.work / "a.txt")}
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Read", read),
                                  claude.can_use_tool("p1", "Read", read, tool_use_id="t1"),
                                  claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "x"),
                                  *claude.text_turn(SID, "read it"), claude.idle(SID)))
        engine = await kit.boot(desk=self.desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.desk.opened, [])

    async def test_without_a_review_queue_an_ask_is_denied(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
                                  claude.permission_answer("p1", "deny"), claude.tool_result(SID, "t1", "no"),
                                  *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        (run,) = kit.fake.invocations()
        answer = next(line for line in run["stdin"] if line.get("type") == "control_response"
                      and line["response"].get("request_id") == "p1")
        self.assertIn("review queue", answer["response"]["response"]["message"])
        self.assertNotIn("waiting_on_person", [e.data["status"] for e in kit.recorder.of("session", sid)])

    async def test_memory_parks_a_held_call_then_the_decision_is_a_grant_and_a_wake(self) -> None:
        kit = self.kit
        retry = claude.can_use_tool("p2", "Bash", PUSH, tool_use_id="t2")
        kit.scenario(self.started(
            claude.tool_call(SID, "t1", "Bash", PUSH), claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
            claude.permission_answer("p1", "deny"), claude.tool_result(SID, "t1", "denied", is_error=True),
            {"sleep": 1.0}, *claude.text_turn(SID, "waiting for approval"), claude.idle(SID),
            claude.user_message(), claude.tool_call(SID, "t2", "Bash", PUSH, message_id="m3"), retry,
            claude.permission_answer("p2", "allow"), claude.tool_result(SID, "t2", "pushed"),
            *claude.text_turn(SID, "pushed", message_id="m4"), claude.idle(SID)))
        engine = await kit.boot(desk=self.desk)
        holder = await kit.session()
        other = await kit.session()
        await engine.deliver(holder, message(1, "push it"))
        await kit.recorder.seen("session", session_id=holder, status="waiting_on_person")
        (item, ask), = self.desk.opened

        # Memory runs short just as another session needs a process: the
        # guard has no idle process to release, so it parks the held call.
        kit.reading = MemoryReading(limit_bytes=8 << 30, used_bytes=(8 << 30) - (100 << 20))
        await engine.deliver(other, message(2, "hi"))
        waiting = await kit.recorder.seen("session", session_id=other, status="queued")
        self.assertEqual(waiting.data["reason"], "waiting for memory")
        await kit.until(lambda: self.desk.parked)
        self.assertEqual(self.desk.parked, [(item, "memory")])
        # Memory recovers while the holder's turn is still busy, so the guard
        # has no need to release it once it is idle.
        kit.reading = MemoryReading()
        await kit.recorder.seen("turn.finished", session_id=holder)
        (run,) = kit.fake.invocations()
        deny = next(line for line in run["stdin"] if line.get("type") == "control_response"
                    and line["response"].get("request_id") == "p1")["response"]["response"]
        self.assertEqual(deny["behavior"], "deny")
        self.assertIn(item, deny["message"])
        self.assertIn("git push origin main", deny["message"])

        # The person allows the parked call: a single-use grant and a `now`
        # wake; the agent's retry hits the grant and spends it.
        delivery = await engine.deliver_decision(item, Decision(allow=True), ask.call,
                                                 self.desk.record(item, "allow"))
        self.assertEqual(delivery, "grant")
        second = await kit.recorder.seen("turn.finished", session_id=holder, count=2)
        self.assertEqual(second.data["wakes"], [f"review:{item}:{kit.recorder.of('review.decided')[0].seq}"])
        self.assertEqual([i for i, a in self.desk.opened if a.session_id == holder], [item])   # the retry asked no one
        (used,) = kit.rows("SELECT used_at IS NOT NULL, tool FROM grants")
        self.assertEqual(used, (1, "Bash"))
        (run,) = kit.fake.invocations()
        wake = [line for line in run["stdin"] if line.get("type") == "user"][1]
        self.assertIn("The person allowed `git push origin main`. Run it again.",
                      wake["message"]["content"][0]["text"])

    async def test_a_native_question_is_held_and_answered_in_the_call(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.ask_user_question("q1", "Which branch?", ["main", "dev"]),
                                  claude.permission_answer("q1", "allow"), *claude.text_turn(SID, "ok"),
                                  claude.idle(SID)))
        engine = await kit.boot(desk=self.desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("review.opened", session_id=sid, kind="question")
        (item, ask), = self.desk.opened
        await engine.deliver_decision(item, Decision(allow=True, answers={"Which branch?": "dev"}), ask.call,
                                      self.desk.record(item, "answer"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        (run,) = kit.fake.invocations()
        answer = next(line for line in run["stdin"] if line.get("type") == "control_response"
                      and line["response"].get("request_id") == "q1")["response"]["response"]
        self.assertEqual(answer["updatedInput"]["answers"], {"Which branch?": "dev"})

    async def test_a_call_the_cli_withdraws_withdraws_its_item(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 0.3},
                                  {"emit": {"type": "control_cancel_request", "request_id": "p1"}},
                                  *claude.text_turn(SID, "never mind"), claude.idle(SID)))
        engine = await kit.boot(desk=self.desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await kit.until(lambda: self.desk.withdrawn)
        self.assertEqual(self.desk.withdrawn, [(self.desk.opened[0][0], "withdrawn")])
        self.assertEqual(self.desk.parked, [])

    async def test_a_call_held_when_its_process_dies_is_parked_and_decided_as_a_grant(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 0.3},
                                  {"exit": 1}))
        engine = await kit.boot(desk=self.desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        parked = await kit.recorder.seen("session", session_id=sid, status="parked")
        (item, ask), = self.desk.opened
        self.assertEqual(self.desk.parked, [(item, "process_ended")])
        self.assertEqual(parked.data["reason"], "process_died")
        (turn,) = kit.rows("SELECT status, stop_reason FROM turns")
        self.assertEqual(turn, ("failed", "failed"))
        self.assertIn("claude exited", kit.recorder.of("turn.finished", sid)[0].data["error"])
        # Its epoch is gone: the allow becomes a grant and a wake, never an
        # answer into another process.
        self.assertEqual(await engine.deliver_decision(item, Decision(allow=True), ask.call,
                                                       self.desk.record(item, "allow")), "grant")
        self.assertEqual(kit.rows("SELECT tool, input_sha256 = ? FROM grants", (ask.call.input_sha256,)),
                         [("Bash", 1)])
        kit.pace()
        await kit.recorder.seen("session", session_id=sid, status="starting", count=2)   # woken


class EpochTests(EngineTestCase):
    async def test_a_stale_process_is_fenced_and_what_it_says_is_dropped(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"sleep": 0.5}, *claude.text_turn(SID, "late"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        started = await kit.recorder.seen("turn.started", session_id=sid)
        # Another attach moves the epoch on (as a restart's recovery does).
        await engine.store.write(lambda tx: tx.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ?", (sid,)))
        with self.assertLogs("services.cowork_agent.storm.engine.session", "WARNING") as logs:
            await kit.until(lambda: not kit.rows("SELECT pid FROM procs"))
        self.assertIn("stale process", logs.output[0])
        self.assertEqual(kit.rows("SELECT status FROM turns WHERE id = ?", (started.turn_id,)), [("running",)])
        self.assertEqual(kit.recorder.of("turn.finished"), [])
        self.assertEqual(kit.rows("SELECT state FROM wakes"), [("taken",)])  # a later boot hands it back


class RecoveryTests(EngineTestCase):
    async def crashed(self, count: int, *, steps: list[dict]) -> list[str]:
        """Sessions caught mid-turn by a crash: a running turn, its wake taken."""
        kit = self.kit
        kit.scenario(steps)
        await kit.boot()
        ids = [await kit.session() for _ in range(count)]
        await self.kit_restart_store(ids)
        return ids

    async def kit_restart_store(self, ids: list[str]) -> None:
        from services.cowork_agent.storm import engine as storm_engine
        await storm_engine.shutdown()
        for n, sid in enumerate(ids):
            kit_rows = [("UPDATE sessions SET status = 'running', epoch = 1 WHERE id = ?", (sid,)),
                        ("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES (?, ?, 1, "
                         "'person', 'running', '2026-10-15T09:00:00Z')", (f"turn_crash{n}", sid)),
                        ("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, turn_id, "
                         "created_at, taken_at) VALUES (?, ?, 'now', 'message', ?, '{\"origin\": \"person\"}', "
                         "'taken', ?, ?, ?)", (f"msg:cmsg_{n}", sid, f"do {n}", f"turn_crash{n}",
                                               f"2026-10-15T09:00:0{n}Z", "2026-10-15T09:00:10Z"))]
            for sql, params in kit_rows:
                self.kit.execute(sql, params)

    async def test_boot_requeues_taken_wakes_and_re_admits_them_four_at_a_time(self) -> None:
        kit = self.kit
        steps = [{"sleep": 1.0}, *claude.initialize(), claude.user_message(), claude.init(SID),
                 *claude.text_turn(SID, "resumed"), claude.idle(SID)]
        ids = await self.crashed(6, steps=steps)
        engine = await kit.boot()
        peak = 0

        async def pace() -> None:                 # time passes; spawn pacing lets about two a second through
            nonlocal peak
            while True:
                peak = max(peak, engine._starting)
                kit.pace()
                await asyncio.sleep(0.02)
        pacer = asyncio.create_task(pace())
        try:
            for sid in ids:
                await kit.recorder.seen("turn.finished", session_id=sid)
        finally:
            pacer.cancel()
        self.assertEqual(peak, 4)                  # each one opens for a second: never more than four at once
        starts = [e.session_id for e in kit.recorder.of("session") if e.data["status"] == "starting"]
        self.assertEqual(sorted(starts), sorted(ids))
        runs = kit.fake.invocations()
        self.assertEqual(len(runs), 6)
        # Six processes wrote one record file at once: read their inputs together.
        inputs = [line["message"]["content"][0]["text"] for r in runs for line in r["stdin"]
                  if line.get("type") == "user"]
        self.assertEqual(len(inputs), 6)
        self.assertTrue(all(t.startswith("Your previous turn was interrupted after it started") for t in inputs))
        self.assertEqual(kit.rows("SELECT redelivery, state FROM wakes ORDER BY id"), [(1, "consumed")] * 6)

    async def test_a_session_whose_process_survived_recovery_is_not_re_admitted(self) -> None:
        kit = self.kit
        steps = [*claude.initialize(), claude.user_message(), claude.init(SID), *claude.text_turn(SID, "ok"),
                 claude.idle(SID)]
        survivor, other = await self.crashed(2, steps=steps)
        kit.execute("INSERT INTO procs VALUES (999999, 1, ?, 1, 'claude', '2999-01-01T00:00:00Z')", (survivor,))
        with unittest.mock.patch("services.cowork_agent.storm.store.recovery.reap_orphan",
                                 unittest.mock.AsyncMock(return_value=False)):
            engine = await kit.boot()
        await kit.recorder.seen("turn.finished", session_id=other)
        await asyncio.sleep(0.2)
        self.assertEqual(kit.recorder.of("turn.started", survivor), [])
        self.assertEqual((await engine.status())["sessions"]["held_back"], [survivor])
        self.assertEqual(kit.rows("SELECT state FROM wakes WHERE session_id = ?", (survivor,)), [("queued",)])
        # The registry keeps the survivor's row for the next boot; this test's process never existed.
        await engine.store.write(lambda tx: tx.execute("DELETE FROM procs WHERE pid = 999999"))


class AdmissionTests(EngineTestCase):
    def limited(self, *rate: dict) -> list[dict]:
        return self.started(*rate, *claude.text_turn(SID, "ok"), claude.idle(SID))

    async def test_a_closed_pool_admits_nothing_new_until_its_reset(self) -> None:
        kit = self.kit
        resets = kit.clock.time() + 1.0
        kit.scenario(self.limited(claude.rate_limit("rejected", resets_at=resets)))
        engine = await kit.boot()
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("pool", state="closed")
        await kit.recorder.seen("turn.finished", session_id=first)    # a running turn always finishes
        await engine.deliver(second, message(2))
        waiting = await kit.recorder.seen("session", session_id=second, status="queued")
        self.assertTrue(waiting.data["reason"].startswith("pool closed until"))
        await asyncio.sleep(1.1)                                       # the runtime's own guard is real time
        kit.clock.advance(2)
        await kit.recorder.seen("pool", state="open")
        await kit.recorder.seen("turn.finished", session_id=second)

    async def test_a_warn_pool_holds_later_work_until_it_ages_into_next(self) -> None:
        kit = self.kit
        kit.scenario(self.limited(claude.rate_limit("allowed_warning")))
        engine = await kit.boot()
        first, later, urgent = await kit.session(), await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("pool", state="warn")
        await kit.recorder.seen("turn.finished", session_id=first)
        kit.pace()
        await engine.deliver(later, Wake(id="sched:job:1", text="nightly", priority="later", reason="schedule"))
        await engine.deliver(urgent, message(3))
        await kit.recorder.seen("turn.finished", session_id=urgent)
        self.assertEqual(kit.recorder.of("turn.started", later), [])
        held = await kit.recorder.seen("session", session_id=later, status="queued")
        self.assertEqual(held.data["reason"], "pool warn")
        await kit.until(lambda: "aging:" + later in engine._timers)
        kit.clock.advance(600)
        await kit.recorder.seen("turn.finished", session_id=later)

    async def test_paid_usage_pauses_new_turns_until_the_person_chooses(self) -> None:
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(self.limited(claude.rate_limit("allowed", overage=True)))
        engine = await kit.boot(desk=desk)
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("pool", state="paid")
        await kit.recorder.seen("review.opened", kind="usage")
        await kit.recorder.seen("turn.finished", session_id=first)
        kit.pace()
        await engine.deliver(second, message(2))
        await kit.recorder.seen("session", session_id=second, status="queued")
        await engine.deliver_usage_decision(AGENT, continue_until=iso(kit.clock.time() + 3600),
                                            record=lambda w: w.append("review.decided", {"pool": AGENT}))
        await kit.recorder.seen("turn.finished", session_id=second)

    async def test_the_memory_guard_queues_a_new_process_until_memory_returns(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        kit.reading = MemoryReading(limit_bytes=4 << 30, used_bytes=(4 << 30) - (900 << 20),
                                    pressure_avg10=0.0)
        with unittest.mock.patch("services.cowork_agent.storm.engine.engine.READ_INTERVAL_S", 0.05):
            engine = await kit.boot()
            sid = await kit.session()
            await engine.deliver(sid, message(1))
            waiting = await kit.recorder.seen("session", session_id=sid, status="queued")
            self.assertEqual(waiting.data["reason"], "waiting for memory")
            self.assertEqual(kit.fake.invocations(), [])               # no process was started
            kit.reading = MemoryReading(limit_bytes=8 << 30, used_bytes=1 << 30, pressure_avg10=0.0)
            await kit.recorder.seen("turn.finished", session_id=sid)   # the next 5 s reading admits it
        status = await engine.status()
        self.assertEqual(status["memory"]["reading"]["limit_bytes"], 8 << 30)

    async def test_memory_pressure_and_descriptors_trip_the_guard_too(self) -> None:
        from services.cowork_agent.storm.engine.memory import MemoryGuard
        guard = MemoryGuard()
        self.assertIsNone(guard.tripped(MemoryReading(limit_bytes=8 << 30, used_bytes=1 << 30), "x"))
        self.assertIn("pressure", guard.tripped(MemoryReading(pressure_avg10=12.5), "x"))
        self.assertIn("file descriptors", guard.tripped(MemoryReading(fds=900, fd_limit=1000), "x"))
        self.assertIn("processes", guard.tripped(MemoryReading(pids=81, pid_limit=100), "x"))
        guard.learn("x", 3 << 30)                                      # a big CLI raises the bar
        self.assertIsNotNone(guard.tripped(MemoryReading(limit_bytes=8 << 30, used_bytes=5 << 30), "x"))


class InputWhileRunningTests(EngineTestCase):
    async def test_send_now_with_live_sub_agents_asks_to_confirm_then_interrupts(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.task_started(SID, "task-1"), *claude.host_control("interrupt"),
                                  claude.result(SID, "", subtype="error_during_execution",
                                                terminal_reason="aborted_tools"),
                                  *claude.task_killed(SID, "task-1"), claude.idle(SID),
                                  claude.user_message(), *claude.text_turn(SID, "now then", message_id="m2"),
                                  claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("run.started", session_id=sid)
        with self.assertRaises(WouldStopRuns) as caught:
            await engine.deliver(sid, message(2, "urgent", delivery="now"))
        self.assertEqual((caught.exception.status, caught.exception.count), (409, 1))
        self.assertTrue(await engine.deliver(sid, message(2, "urgent", delivery="now"), confirm_stop_runs=1))
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        first = kit.recorder.of("turn.finished", sid)[0]
        self.assertEqual((first.data["status"], first.data["wakes"]), ("interrupted", ["msg:cmsg_1"]))
        self.assertEqual(second.data["wakes"], ["msg:cmsg_2"])
        self.assertEqual(kit.recorder.of("turn.stopped"), [])         # not a Stop


class ReconnectTests(EngineTestCase):
    async def test_a_new_login_recycles_the_pool_at_the_turn_boundary(self) -> None:
        kit = self.kit
        creds = kit.home / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old"}}))
        kit.scenario(self.started({"sleep": 0.5}, *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.started", session_id=sid)
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "new"}}))   # the person reconnected
        recycled = await kit.recorder.seen("session", session_id=sid, status="cold", count=2)
        self.assertEqual(recycled.data["reason"], "recycle")
        self.assertEqual(kit.recorder.of("turn.finished", sid)[0].data["status"], "finished")   # it finished first
        self.assertEqual(kit.rows("SELECT recycle_pending FROM sessions"), [(1,)])
        kit.pace()
        await engine.deliver(sid, message(2))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(kit.rows("SELECT recycle_pending FROM sessions"), [(0,)])
        self.assertIn("--resume", kit.fake.invocations()[1]["argv"])


class ObservationTests(EngineTestCase):
    async def test_an_engine_session_writes_todays_session_index_row(self) -> None:
        from services.cowork_agent.adapters.claude_code import adapter as today
        from services.cowork_agent.engine import sessions_io
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await kit.until(lambda: any(r.get("usage", {}).get("input_tokens") for r in
                                    sessions_io.read_session_index("demo").values()))
        (key, row), = sessions_io.read_session_index("demo").items()
        written = {}
        with unittest.mock.patch.object(today, "_write_agent_row", lambda _a, _k, r: written.update(r)):
            today.write_preliminary_entry("claude:demo:web:1", "space-1", str(kit.work), native_session_id="n")
        self.assertEqual(set(row), set(written))
        self.assertEqual(set(row["usage"]), set(written["usage"]))
        native = (await engine.get_session(sid))["native_id"]
        self.assertEqual((row["sessionId"], row["nativeSessionId"], row["backend"], row["directory"]),
                         (native, native, AGENT, str(kit.work)))
        self.assertEqual((row["usage"]["input_tokens"], row["usage"]["output_tokens"]), (10, 5))
        self.assertEqual(key.split(":")[1], "demo")                    # the key's project segment, as today's


if __name__ == "__main__":
    unittest.main()
