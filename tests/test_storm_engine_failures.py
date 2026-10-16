"""Storm's engine on its failure paths, and the seams PR 8 and PR 9 use (the
PR 7 review's must-fixes, should-fixes and seams). Each test fails on the
engine before its fix (635808a)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
import unittest.mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.adapters.claude_code.runtime import session as claude_session
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import session as session_module
from services.cowork_agent.storm.engine import AlreadyDecided, Decision
from services.cowork_agent.storm.engine.engine import pool_id
from services.cowork_agent.storm.engine.memory import MemoryReading
from services.cowork_agent.storm.engine.records import Ids, TurnIdTaken, ensure_turn
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.store import EngineLock, StoreFailed
from tests.fakes import claude, expect
from tests.storm_engine_kit import AGENT, SID, Desk, Kit, message

PUSH = {"command": "git push origin main"}


def started(*after: dict) -> list[dict]:
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


def session_events(kit: Kit, sid: str, reason: str | None = None) -> list:
    return [e for e in kit.recorder.of("session", sid) if reason is None or e.data.get("reason") == reason]


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()


class ShutdownTests(Case):
    async def test_a_turn_the_server_stops_hands_its_wakes_back_and_the_next_boot_resumes_it(self) -> None:
        """Must-fix 1: a graceful restart mid-turn is ch 07 §9.3's case."""
        kit = self.kit
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", {"command": "make build"}), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "build it"))
        await kit.recorder.seen("step.tool", session_id=sid)
        await storm_engine.shutdown()
        (state, redelivery, envelope), = kit.rows("SELECT state, redelivery, envelope FROM wakes")
        self.assertEqual((state, redelivery), ("queued", 0))           # a shutdown doesn't count toward the bound
        self.assertTrue(envelope.startswith("Your previous turn was interrupted after “make build” because the "
                                            "server restarted"))
        self.assertTrue(envelope.endswith("build it"))
        finished = kit.recorder.of("turn.finished", sid)[0]
        self.assertEqual((finished.data["wakes"], finished.data["wakes_requeued"]), ([], ["msg:cmsg_1"]))
        await kit.boot()
        await kit.recorder.seen("turn.started", session_id=sid, count=2)       # resumed after the restart
        self.assertEqual(kit.rows("SELECT state FROM wakes"), [("taken",)])


class StopRaceTests(Case):
    async def test_a_stop_that_returns_after_the_turn_end_still_admits_and_releases(self) -> None:
        """Must-fix 2: the interrupt's reply can come after the turn's end."""
        kit = self.kit
        original = claude_session.ClaudeSession.interrupt

        async def late(session, grace_s=10):
            await original(session, grace_s)
            await asyncio.sleep(0.3)
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", {"command": "make test"}),
                             *claude.host_control("interrupt"),
                             claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
                             claude.idle(SID),
                             claude.user_message(), *claude.text_turn(SID, "second", message_id="m2"),
                             claude.idle(SID)))
        with unittest.mock.patch.object(claude_session.ClaudeSession, "interrupt", late):
            engine = await kit.boot()
            sid = await kit.session()
            await engine.deliver(sid, message(1))
            await kit.recorder.seen("step.tool", session_id=sid)
            stop = asyncio.create_task(engine.stop(sid, by="person"))
            await kit.recorder.seen("session", session_id=sid, status="stopping")
            await engine.deliver(sid, message(2, "sent during the Stop"))
            self.assertTrue((await stop).stopped)
            await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        await kit.idle_timer()                                       # and the warm process is released later
        # No state change committed ahead of the turn end that caused it.
        types = [(e.type, e.data.get("status")) for e in kit.recorder.committed_events if e.session_id == sid]
        first_end = types.index(("turn.finished", "stopped"))
        self.assertNotIn(("session", "warm"), types[types.index(("session", "stopping")):first_end])

    async def test_stop_with_only_a_background_sub_agent_stops_it(self) -> None:
        """Must-fix 3: the root turn ended; its sub-agent still runs."""
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"),
                             claude.idle(SID), *claude.host_control("stop_task"), *claude.task_killed(SID, "task-1"),
                             claude.system(SID, "background_tasks_changed", tasks=[])))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((await engine.get_session(sid))["live_runs"], 1)
        result = await engine.stop(sid, by="person")
        self.assertEqual((result.stopped, result.turn_id), (True, None))
        await kit.recorder.seen("run.finished", session_id=sid, status="stopped")
        stop = [line["request"] for run in kit.runs() for line in run["stdin"]
                if line.get("request", {}).get("subtype") == "stop_task"]
        self.assertEqual(stop, [{"subtype": "stop_task", "task_id": "task-1"}])
        await kit.idle_timer()


class DecisionTests(Case):
    def held(self) -> list[dict]:
        return started(claude.tool_call(SID, "t1", "Bash", PUSH), claude.can_use_tool("p1", "Bash", PUSH,
                                                                                      tool_use_id="t1"),
                       claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "pushed"),
                       *claude.text_turn(SID, "done"), claude.idle(SID))

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.desk = Desk(loop=asyncio.get_running_loop())
        self.kit.scenario(self.held())
        self.engine = await self.kit.boot(desk=self.desk)
        self.sid = await self.kit.session()
        await self.engine.deliver(self.sid, message(1))
        await self.kit.recorder.seen("session", session_id=self.sid, status="waiting_on_person")
        (self.item, self.ask), = self.desk.opened

    async def test_a_decision_delivered_twice_has_one_effect(self) -> None:
        """Must-fix 4b: a double click on Allow."""
        decide = lambda: self.engine.deliver_decision(self.item, Decision(allow=True), self.ask.call,  # noqa: E731
                                                      self.desk.record(self.item, "allow"))
        first, second = await asyncio.gather(decide(), decide(), return_exceptions=True)
        self.assertEqual(first, "in_call")
        self.assertIsInstance(second, AlreadyDecided)
        self.assertEqual((second.status, second.code), (409, "already_decided"))
        with self.assertRaises(AlreadyDecided):                       # and after the first has committed
            await decide()
        await self.kit.recorder.seen("turn.finished", session_id=self.sid)
        self.assertEqual(self.kit.rows("SELECT count(*) FROM grants"), [(0,)])
        self.assertEqual(self.kit.rows("SELECT count(*) FROM wakes WHERE id LIKE 'review:%'"), [(0,)])

    async def test_a_decision_whose_write_fails_leaves_the_call_held(self) -> None:
        """Must-fix 4a: the decision commits first; nothing is delivered
        without it."""
        def failing(w: Writes, delivery: str) -> int:
            raise StoreFailed("the store at xo.db could not be used: database or disk is full")
        with self.assertRaises(StoreFailed):
            await self.engine.deliver_decision(self.item, Decision(allow=True), self.ask.call, failing)
        view = await self.engine.get_session(self.sid)
        self.assertEqual((view["state"], view["held"], view["held_items"]), ("waiting_on_person", 1, [self.item]))
        retry = await self.engine.deliver_decision(self.item, Decision(allow=True), self.ask.call,
                                                   self.desk.record(self.item, "allow"))
        self.assertEqual(retry, "in_call")
        await self.kit.recorder.seen("turn.finished", session_id=self.sid)


class PaidUsageTests(Case):
    async def test_paid_then_wait_reopens_at_the_reset(self) -> None:
        """Must-fix 5."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        resets = kit.clock.time() + 60
        kit.scenario(started(claude.rate_limit("allowed", overage=True, resets_at=resets),
                             *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot(desk=desk)
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("pool", state="paid")
        await kit.recorder.seen("turn.finished", session_id=first)
        pool = (await engine.get_session(first))["pool"]
        await engine.deliver_usage_decision(pool, continue_until=None,                  # "wait"
                                            record=lambda w: w.append("review.decided", {"pool": pool}))
        kit.pace()
        await engine.deliver(second, message(2))
        await kit.recorder.seen("session", session_id=second, status="queued")
        kit.clock.advance(61)
        reopened = await kit.recorder.seen("pool", pool=pool, state="open")
        self.assertEqual(reopened.data["was"], "paid")


class RetryTests(Case):
    async def test_an_overloaded_api_hands_the_message_back_backs_off_and_warns_the_pool(self) -> None:
        """Must-fix 7 (ch 07 §5.1): after claude's own retries, the turn fails
        on a 529; the person's message is not consumed."""
        kit = self.kit
        overloaded = {"emit": {"type": "result", "subtype": "error_during_execution", "is_error": True,
                               "result": "", "session_id": SID, "api_error_status": 529,
                               "errors": ["Overloaded"], "usage": {}}}
        kit.scenario(started(overloaded, claude.idle(SID), claude.user_message(),
                             *claude.text_turn(SID, "now it worked"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "hello"))
        failed = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((failed.data["status"], failed.data["error_class"], failed.data["wakes_requeued"]),
                         ("failed", "overloaded", ["msg:cmsg_1"]))
        await kit.recorder.seen("pool", state="warn")                 # counts toward warn
        self.assertIn(sid, (await engine.status())["sessions"]["backing_off"])
        await asyncio.sleep(0.2)
        self.assertEqual(len(kit.recorder.of("turn.started", sid)), 1)   # not before the backoff
        kit.clock.advance(2)
        done = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual((done.data["status"], done.data["wakes"]), ("finished", ["msg:cmsg_1"]))
        await kit.recorder.seen("pool", state="open")                 # a completed turn: the overload is over
        user = [line["message"]["content"][0]["text"] for line in kit.runs()[0]["stdin"] if line.get("type") == "user"]
        self.assertTrue(user[1].startswith("Your previous turn was interrupted after it started because the "
                                           "model's service failed (overloaded)"))
        self.assertNotIn(sid, (await engine.status())["sessions"]["backing_off"])

    async def test_a_cli_that_dies_mid_turn_hands_back_until_the_bound_then_drops_and_says_so(self) -> None:
        """Must-fixes 6 and 7: a crash mid-turn is handed back with the note,
        at most MAX_REDELIVERIES times, with a backoff between tries."""
        kit = self.kit
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", {"command": "make"}), {"exit": 3}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        async with kit.pacing():
            dropped = await kit.recorder.seen("session", session_id=sid, reason="input_dropped")
        self.assertEqual(dropped.data["wakes_dropped"], ["msg:cmsg_1"])
        self.assertTrue(dropped.data["error"].startswith("Couldn't run your message: it was tried 4 times"))
        self.assertEqual(kit.rows("SELECT state, redelivery FROM wakes"), [("dropped", 3)])
        self.assertEqual(len(kit.runs()), 4)

    async def test_a_cli_that_dies_right_after_it_starts_is_restarted_with_a_growing_backoff(self) -> None:
        """Must-fix 6a: no respawn at the pacing rate."""
        kit = self.kit
        kit.scenario([*claude.initialize(), {"exit": 1}])
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        for _ in range(20):                                        # ten seconds of engine time
            await asyncio.sleep(0.1)
            kit.pace()
        self.assertLessEqual(len(kit.runs()), 4)                   # tries at about 0, 1, 3 and 7 s
        self.assertGreaterEqual(len(kit.runs()), 2)

    async def test_an_open_that_fails_for_good_waits_for_the_next_message(self) -> None:
        """Must-fix 6b: ``retryable=False`` is honoured, and the person sees why."""
        kit = self.kit
        opens = []

        class LoggedOut(ClaudeRuntime):
            async def open(self, spec, host, *, session_id):
                opens.append(session_id)
                raise c.RuntimeFailure("auth_required", "claude is not logged in; connect Claude", retryable=False)
        kit.scenario([])
        engine = await kit.boot_with(LoggedOut(claude_home=kit.home / ".claude"))
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        failed = await kit.recorder.seen("session", session_id=sid, reason="open_failed")
        self.assertEqual((failed.data["error"], failed.data["retryable"]),
                         ("claude is not logged in; connect Claude", False))
        for _ in range(60):                                        # an hour, a minute at a time
            kit.clock.advance(60)
            await asyncio.sleep(0.005)
        self.assertEqual(len(opens), 1)
        self.assertEqual(kit.rows("SELECT state FROM wakes"), [("queued",)])
        await engine.deliver(sid, message(2))                      # the person tries again
        await kit.until(lambda: len(opens) == 2)


class ReconnectTests(Case):
    async def test_a_reconnect_to_another_account_starts_from_that_accounts_pool(self) -> None:
        """Must-fix 8: the pool is agent × account."""
        kit = self.kit
        creds = kit.home / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "account-a"}}))
        resets = kit.clock.time() + 5 * 3600
        kit.scenario(started({"sleep": 0.5}, claude.rate_limit("rejected", resets_at=resets),
                             *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("turn.started", session_id=first)
        old = (await engine.get_session(first))["pool"]
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "account-b"}}))   # Connect Claude: account B
        await kit.recorder.seen("pool", pool=old, state="closed")
        await kit.recorder.seen("session", session_id=first, reason="recycle")
        kit.pace()
        await engine.deliver(second, message(2))
        await kit.recorder.seen("turn.started", session_id=second)  # account A's limit doesn't hold B back
        new = (await engine.get_session(second))["pool"]
        self.assertNotEqual(new, old)
        self.assertTrue(new.startswith(f"{AGENT}:acct-"))

    async def test_a_reconnect_recycles_only_processes_that_started_before_it(self) -> None:
        """Should-fix: a notice from an old process never recycles one that
        started on the new login."""
        kit = self.kit
        login(kit, "a")
        kit.scenario(started(*claude.text_turn(SID, "one"), claude.idle(SID), claude.user_message(),
                             *claude.text_turn(SID, "two", message_id="m2"), claude.idle(SID)))
        engine = await kit.boot()
        old, fresh = await kit.session(), await kit.session()
        await engine.deliver(old, message(1))
        await kit.recorder.seen("turn.finished", session_id=old)     # warm, on account A's login
        login(kit, "b")                                              # Connect Claude: account B
        kit.pace()
        await engine.deliver(fresh, message(2))
        await kit.recorder.seen("turn.finished", session_id=fresh)   # started on B's login: no notice
        await engine.deliver(old, message(3))                        # the old process notices at its boundary
        await kit.recorder.seen("session", session_id=old, reason="recycle")
        await asyncio.sleep(0.2)
        self.assertEqual(session_events(kit, fresh, "recycle"), [])
        self.assertEqual(kit.rows("SELECT id, recycle_pending FROM sessions ORDER BY seq"), [(old, 1), (fresh, 0)])
        (recycled,) = kit.recorder.of("pool", None)[-1:]
        self.assertEqual(recycled.data["recycle"], [old])


class MemoryTests(Case):
    async def test_one_approval_is_parked_at_a_time_and_its_process_released(self) -> None:
        """Should-fix: parking frees nothing until the process goes."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", PUSH),
                             claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
                             claude.permission_answer("p1", "deny"), claude.tool_result(SID, "t1", "no", is_error=True),
                             {"sleep": 1.0}, *claude.text_turn(SID, "other work"), claude.idle(SID)))
        with unittest.mock.patch("services.cowork_agent.storm.engine.engine.READ_INTERVAL_S", 0.05):
            engine = await kit.boot(desk=desk)
            a, b, other = await kit.session(), await kit.session(), await kit.session()
            async with kit.pacing():
                await engine.deliver(a, message(1))
                await engine.deliver(b, message(2))
                await kit.recorder.seen("session", session_id=a, status="waiting_on_person")
                await kit.recorder.seen("session", session_id=b, status="waiting_on_person")
            kit.reading = MemoryReading(limit_bytes=8 << 30, used_bytes=(8 << 30) - (100 << 20))
            await engine.deliver(other, message(3))
            released = await kit.recorder.seen("session", reason="memory", status="cold")
            (parked, _why), = desk.parked                       # one, not every held approval
            owner = next(ask.session_id for item, ask in desk.opened if item == parked)
            self.assertEqual(released.session_id, owner)        # and its process is released at its turn's end


class StoreFailureTests(Case):
    async def test_a_store_that_fails_mid_turn_closes_the_process_and_leaves_the_wake_for_recovery(self) -> None:
        """Should-fix: an unrecorded process must not keep running."""
        kit = self.kit
        kit.scenario(started({"sleep": 0.3}, *claude.text_turn(SID, "late"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.started", session_id=sid)
        with unittest.mock.patch("services.cowork_agent.storm.engine.records.finish_turn",
                                 side_effect=StoreFailed("database or disk is full")):
            with self.assertLogs("services.cowork_agent.storm.engine.session", "ERROR"):
                await kit.until(lambda: not kit.rows("SELECT pid FROM procs"))
        self.assertEqual(kit.rows("SELECT state FROM wakes"), [("taken",)])
        self.assertEqual(kit.rows("SELECT status FROM turns"), [("running",)])   # boot recovery's to settle


class SurvivorTests(Case):
    async def test_a_message_for_a_held_back_session_says_why_and_rechecks(self) -> None:
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        await kit.boot()
        sid = await kit.session()
        await storm_engine.shutdown()
        kit.execute("INSERT INTO procs VALUES (999999, 1, ?, 1, 'claude', '2999-01-01T00:00:00Z')", (sid,))
        with unittest.mock.patch("services.cowork_agent.storm.store.recovery.reap_orphan",
                                 unittest.mock.AsyncMock(return_value=False)):
            engine = await kit.boot()
        with unittest.mock.patch("services.cowork_agent.storm.engine.engine.reap_orphan",
                                 unittest.mock.AsyncMock(return_value=False)):
            await engine.deliver(sid, message(1))
            said = await kit.recorder.seen("session", session_id=sid, reason="held_back")
        self.assertIn("pid 999999", said.data["error"])
        with unittest.mock.patch("services.cowork_agent.storm.engine.engine.reap_orphan",
                                 unittest.mock.AsyncMock(return_value=True)):     # the person stopped it
            await engine.deliver(sid, message(2))
            await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((await engine.status())["sessions"]["held_back"], [])


class BootTests(Case):
    async def test_a_failed_start_lets_go_of_the_lock(self) -> None:
        kit = self.kit
        kit.scenario([])
        with unittest.mock.patch.object(storm_engine.Engine, "start", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await kit.boot()
        with self.assertRaises(storm_engine.EngineUnavailable) as caught:
            storm_engine.current()
        self.assertEqual(caught.exception.code, "engine_failed")
        EngineLock.acquire().release()                              # free for the next server


class RecordTests(Case):
    async def test_a_turn_id_another_session_has_is_refused(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        a, b = await kit.session(), await kit.session()
        await engine.write(lambda w: ensure_turn(w, Ids(a), "turn_1", "person", 0))
        with self.assertRaises(TurnIdTaken):
            await engine.write(lambda w: ensure_turn(w, Ids(b), "turn_1", "person", 0))
        self.assertEqual(kit.rows("SELECT session_id FROM turns"), [(a,)])

    async def test_before_seq_gives_the_page_right_before_it(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        ids = [await kit.session() for _ in range(5)]
        last = (await engine.list_sessions())[-1]["seq"]
        self.assertEqual([r["id"] for r in await engine.list_sessions(before_seq=last, limit=2)], ids[2:4])
        self.assertEqual([r["id"] for r in await engine.list_sessions(limit=2)], ids[:2])
        with self.assertRaises(storm_engine.SessionRefused):
            await engine.list_sessions(state="sleeping")


class RuleTests(Case):
    async def test_rules_are_read_and_a_bad_one_never_widens_anything(self) -> None:
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        lint = {"command": "npm run lint"}
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", lint),
                             claude.can_use_tool("p1", "Bash", lint, tool_use_id="t1"),
                             claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "ok"),
                             *claude.text_turn(SID, "linted"), claude.idle(SID)))
        engine = await kit.boot(desk=desk)

        def rows(w: Writes) -> None:
            w.execute("INSERT INTO chats VALUES ('chat_p1', 'project', 'pid-1', 'demo', 't', '{}')")
            for rid, match in (("r_ok", {"prefix": ["npm", "run", "lint"]}), ("r_bad", {"nonsense": 1})):
                w.execute("INSERT INTO rules (id, scope, project_pid, tool, match, effect, sentence, tier, "
                          "created_at) VALUES (?, 'project', 'pid-1', 'command', ?, 'allow', 'run npm run lint', "
                          "'notable', 't')", (rid, json.dumps(match)))
        await engine.write(rows)
        sid = await engine.create_session(c.SessionSpec(cwd=str(kit.work), permissions=c.PermissionPolicy(mode="ask")),
                                          agent=AGENT, role="thread", preset="thread", chat_id="chat_p1",
                                          thread_ts="1.0")
        with self.assertLogs("services.cowork_agent.storm.engine.session", "ERROR"):
            await engine.deliver(sid, message(1))
            await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(desk.opened, [])                           # the good rule allowed it, unasked
        self.assertEqual(kit.rows("SELECT uses FROM rules WHERE id = 'r_ok'"), [(1,)])


class SendFailureTests(Case):
    async def test_a_rate_limited_send_closes_the_pool_and_keeps_the_wake(self) -> None:
        kit = self.kit
        resets = kit.clock.time() + 3600
        kit.scenario(started(claude.rate_limit("rejected", resets_at=resets), *claude.text_turn(SID, "ok"),
                             claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        # The pool closed from claude's event; a new message waits for it.
        await engine.deliver(sid, message(2))
        await asyncio.sleep(0.2)
        self.assertEqual(kit.rows("SELECT state FROM wakes WHERE id = 'msg:cmsg_2'"), [("queued",)])

    async def test_an_input_the_runtime_refuses_is_dropped_and_said(self) -> None:
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        with unittest.mock.patch.object(claude_session.ClaudeSession, "send",
                                        side_effect=c.RuntimeFailure("unsupported", "no such image type")):
            await engine.deliver(sid, message(2))
            refused = await kit.recorder.seen("session", session_id=sid, reason="input_refused")
        self.assertEqual((refused.data["error"], refused.data["wakes_dropped"]), ("no such image type", ["msg:cmsg_2"]))


class UpdateTests(Case):
    async def test_what_the_runtime_refuses_is_reported_and_what_it_took_is_kept(self) -> None:
        kit = self.kit
        refuse_model = [expect({"type": "control_request", "request": {"subtype": "set_model"}}, rid="request_id"),
                        {"emit": {"type": "control_response", "response": {"subtype": "error", "request_id": "$rid",
                                                                           "error": "no such model"}}}]
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID), *refuse_model,
                             *claude.host_control("set_permission_mode"), {"sleep": 5}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        with self.assertRaises(storm_engine.SessionRefused) as caught:
            await engine.update(sid, model="haiku", permission_mode="plan")
        self.assertEqual(caught.exception.code, "update_refused")
        view = await engine.get_session(sid)
        self.assertEqual((view["model"], view["permission_mode"]), (None, "plan"))

    async def test_an_effort_change_waits_for_the_live_sub_agent(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"),
                             claude.idle(SID), {"sleep": 0.5},
                             claude.system(SID, "task_notification", task_id="task-1", status="completed",
                                           summary="done"), {"sleep": 5}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await engine.update(sid, effort="high")
        self.assertEqual(len(kit.runs()), 1)                       # not restarted under a live sub-agent
        await kit.recorder.seen("run.finished", session_id=sid)
        await kit.until(lambda: len(kit.runs()) == 2)              # restarted at idle, resumed, with the effort
        second = kit.runs()[1]["argv"]
        self.assertEqual(second[second.index("--effort") + 1], "high")


class SeamTests(Case):
    async def test_the_streams_seams(self) -> None:
        """PR 8: listeners from boot, unsubscribe, the epoch on ephemerals,
        held items in the session view."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", PUSH),
                             claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 30}))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        self.assertEqual(kit.recorder.of("session", sid)[0].data["reason"], "created")   # heard from the start
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="waiting_on_person")
        self.assertEqual((await engine.get_session(sid))["held_items"], [desk.opened[0][0]])
        self.assertTrue(kit.recorder.epochs and set(kit.recorder.epochs) == {1})
        heard = len(kit.recorder.committed_events)
        engine.unsubscribe(kit.recorder)
        await engine.pause(sid, reason="stuck on a denied step")
        self.assertEqual(len(kit.recorder.committed_events), heard)

    async def test_the_review_queues_seams(self) -> None:
        """PR 9: pause and resume, the input hash on a stored permission step,
        restart parking through the desk."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", PUSH),
                             claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 30}))
        engine = await kit.boot(desk=desk)
        sid, paused = await kit.session(), await kit.session()
        await engine.pause(paused, reason="stuck on a denied step")
        await engine.deliver(paused, message(9))
        await engine.deliver(sid, message(1))
        step = await kit.recorder.seen("step.permission", session_id=sid)
        self.assertEqual(step.data["request"]["input_sha256"], desk.opened[0][1].call.input_sha256)
        self.assertNotIn("input", step.data["request"])
        await asyncio.sleep(0.2)
        self.assertEqual(kit.recorder.of("turn.started", paused), [])   # paused: no new turn
        self.assertEqual((await engine.get_session(paused))["paused"], "stuck on a denied step")
        # A restart: the held call is parked through the review queue.
        await storm_engine.shutdown()
        self.assertEqual(desk.parked, [(desk.opened[0][0], "process_ended")])
        kit.execute("UPDATE review_items SET parked = 0")          # as if the server had died holding it
        await kit.boot(desk=desk)
        self.assertEqual(desk.parked[-1], (desk.opened[0][0], "restart"))
        await engine_resume(paused)
        await kit.until(lambda: kit.rows("SELECT paused FROM sessions WHERE id = ?", (paused,)) == [(None,)])


def login(kit: Kit, account: str) -> None:
    """Connect Claude to ``account``: claude's own files, as its login writes them."""
    (kit.home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": f"rt-{account}", "accessToken": "at", "expiresAt": 4102444800000}}))
    (kit.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": f"uuid-{account}"}}))


class ReReviewTests(Case):
    """The PR 7 re-review's must-fixes (R1–R3), should-fixes (S1, S2) and
    items A–C. Each fails on 9d9f434."""

    async def test_a_reconnect_with_nothing_live_moves_new_work_to_the_new_accounts_pool(self) -> None:
        """R1: hours after a limit every process has been released; the
        account is read at every cold start."""
        kit = self.kit
        login(kit, "a")
        kit.scenario(started(claude.rate_limit("rejected", resets_at=kit.clock.time() + 5 * 3600),
                             *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        await kit.recorder.seen("pool", state="closed")
        await kit.recorder.seen("turn.finished", session_id=first)
        await kit.idle_timer()
        kit.clock.advance(600)
        await kit.recorder.seen("session", session_id=first, reason="idle")
        self.assertEqual((await engine.status())["sessions"]["live"], 0)
        login(kit, "b")                                             # Connect Claude: account B
        kit.pace()
        await engine.deliver(second, message(2))
        await kit.recorder.seen("turn.started", session_id=second)
        self.assertEqual([e for e in session_events(kit, second) if e.data["status"] == "queued"], [])
        self.assertNotEqual((await engine.get_session(second))["pool"], (await engine.get_session(first))["pool"])

    async def test_a_recovered_api_retry_never_makes_a_later_failure_retryable(self) -> None:
        """R2: claude retried the API once and recovered; the turn then failed
        on max turns, which is the turn's own failure."""
        kit = self.kit
        kit.scenario(started(claude.system(SID, "api_retry", attempt=1, error_status=529, error="overloaded"),
                             *claude.text_turn(SID, "working", message_id="m1")[:-1],
                             claude.result(SID, "Reached max turns", subtype="error_max_turns"), claude.idle(SID),
                             {"sleep": 5}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "push it"))
        done = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((done.data["status"], done.data["wakes"], done.data["wakes_requeued"]),
                         ("failed", ["msg:cmsg_1"], []))
        self.assertEqual(kit.recorder.of("pool", None), [])          # no overload counted

    async def test_a_cli_that_keeps_dying_at_start_waits_for_the_person(self) -> None:
        """R3: after MAX_FAILED_STARTS starts in a row it stops, says why, and
        tries again on the person's next message."""
        from services.cowork_agent.storm.engine.engine import MAX_FAILED_STARTS
        kit = self.kit
        kit.scenario([*claude.initialize(), {"exit": 1}])
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        held = None
        for _ in range(24 * 60):                                    # up to a day, a minute at a time
            kit.clock.advance(60)
            await asyncio.sleep(0.005)
            held = next(iter(session_events(kit, sid, "start_failed")), None)
            if held is not None:
                break
        self.assertIsNotNone(held)
        self.assertEqual(len(kit.runs()), MAX_FAILED_STARTS)
        self.assertIn(f"{MAX_FAILED_STARTS} times in a row", held.data["error"])
        for _ in range(60):
            kit.clock.advance(60)
            await asyncio.sleep(0.005)
        self.assertEqual(len(kit.runs()), MAX_FAILED_STARTS)        # nothing more until the person acts
        self.assertTrue((await engine.status())["sessions"]["backing_off"][sid]["until_next_message"])
        await engine.deliver(sid, message(2))
        await kit.until(lambda: len(kit.runs()) == MAX_FAILED_STARTS + 1)

    async def test_a_failed_turn_whose_process_then_exits_is_not_taken_for_a_crash(self) -> None:
        """S2: the crash signal is the runtime's Error(process_died), not the
        order the pump happens to read events in."""
        kit = self.kit
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", {"command": "make"}),
                             claude.tool_result(SID, "t1", "done"),
                             claude.result(SID, "Reached max turns", subtype="error_max_turns"), {"exit": 0}))
        original = session_module.LiveSession._turn_finished

        async def behind(live, ev):
            await asyncio.sleep(0.5)                                # the pump is busy with an earlier write
            await original(live, ev)
        with unittest.mock.patch.object(session_module.LiveSession, "_turn_finished", behind):
            engine = await kit.boot()
            sid = await kit.session()
            await engine.deliver(sid, message(1))
            done = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((done.data["wakes"], done.data["wakes_requeued"]), (["msg:cmsg_1"], []))

    async def test_a_second_decision_while_the_first_one_is_written_is_refused_even_if_that_write_fails(self) -> None:
        """S1: the first write fails (disk full); the second never mints a
        grant, and the call stays answerable."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", PUSH),
                             claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
                             claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "pushed"),
                             *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="waiting_on_person")
        (item, ask), = desk.opened
        started_writing = asyncio.Event()

        def disk_full(w: Writes, delivery: str) -> int:
            raise StoreFailed("database or disk is full")

        async def first() -> str:
            original = engine.write

            async def slow(fn):
                started_writing.set()
                await asyncio.sleep(0.1)
                return await original(fn)
            with unittest.mock.patch.object(engine, "write", slow):
                return await engine.deliver_decision(item, Decision(allow=True), ask.call, disk_full)

        async def second() -> str:
            await started_writing.wait()
            return await engine.deliver_decision(item, Decision(allow=True), ask.call, desk.record(item, "allow"))
        results = await asyncio.gather(first(), second(), return_exceptions=True)
        self.assertEqual([type(r) for r in results], [StoreFailed, AlreadyDecided])
        self.assertEqual(kit.rows("SELECT count(*) FROM grants"), [(0,)])
        self.assertEqual(kit.rows("SELECT state FROM review_items"), [("open",)])
        self.assertEqual(await engine.held_items(sid), [item])
        self.assertEqual(await engine.deliver_decision(item, Decision(allow=True), ask.call,
                                                       desk.record(item, "allow")), "in_call")
        await kit.recorder.seen("turn.finished", session_id=sid)

    async def test_a_grant_matches_claudes_retry_with_a_new_description_and_nothing_else(self) -> None:
        """A (ch 15 §6.3): the grant binds the action, not the raw input."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        push = {"command": "git push origin main", "description": "Push the branch"}
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", push),
                             claude.can_use_tool("p1", "Bash", push, tool_use_id="t1"), {"sleep": 0.3}, {"exit": 1}))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="parked")    # the call died with its process
        (item, ask), = desk.opened
        retried = {"command": "git push origin main", "description": "Push to origin again"}   # claude's rewording
        other = {"command": "git push origin dev", "description": "Push to origin again"}
        kit.scenario(started(claude.tool_call(SID, "t2", "Bash", retried),
                             claude.can_use_tool("p2", "Bash", retried, tool_use_id="t2"),
                             claude.permission_answer("p2", "allow"), claude.tool_result(SID, "t2", "pushed"),
                             claude.tool_call(SID, "t3", "Bash", other, message_id="m3"),
                             claude.can_use_tool("p3", "Bash", other, tool_use_id="t3"), {"sleep": 30}))
        await engine.deliver_decision(item, Decision(allow=True), ask.call, desk.record(item, "allow"))
        kit.clock.advance(2)                                        # past the crash's backoff
        async with kit.pacing():
            await kit.until(lambda: len(desk.opened) == 2)
        self.assertEqual(kit.rows("SELECT used_at IS NOT NULL FROM grants"), [(1,)])   # spent, once
        self.assertEqual(desk.opened[1][1].request.command, "git push origin dev")     # other arguments ask
        self.assertNotEqual(desk.opened[1][1].call.input_sha256, ask.call.input_sha256)

    async def test_wait_reads_the_pool_in_its_own_transaction(self) -> None:
        """B: "wait" never saves the pool from stale memory."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.rate_limit("allowed", overage=True, resets_at=kit.clock.time() + 3600),
                             *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("pool", state="paid")
        pool = (await engine.get_session(sid))["pool"]
        # The window the review found: memory not yet updated after a commit (simulated: stale memory).
        with unittest.mock.patch.dict(engine._pools, {pool: dataclasses.replace(engine._pools[pool], state="open")}):
            await engine.deliver_usage_decision(pool, continue_until=None, record=lambda w: None)    # "wait"
        self.assertEqual(kit.rows("SELECT state FROM pools WHERE id = ?", (pool,)), [("paid",)])
        with self.assertRaises(storm_engine.SessionRefused) as caught:
            await engine.deliver_usage_decision("nope:default", continue_until=None, record=lambda w: None)
        self.assertEqual((caught.exception.code, caught.exception.status), ("pool_not_found", 404))

    async def test_a_sub_agents_ask_names_it_and_the_streams_views(self) -> None:
        """C (ch 14 §6), and the public views PR 8 uses: features, held items,
        the store's head."""
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.task_started(SID, "task-7", description="Check migrations"),
                             claude.can_use_tool("p1", "Bash", {"command": "npm test"}, agent_id="task-7"),
                             {"sleep": 30}))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="waiting_on_person")
        (item, ask), = desk.opened
        self.assertEqual((ask.run, ask.run_title), ("task-7", "Check migrations"))
        self.assertEqual(await engine.held_items(sid), [item])
        self.assertIn("send_while_running", (await engine.features(sid)).features)
        self.assertEqual(await engine.store.head(), kit.recorder.committed_events[-1].seq)


async def engine_resume(session_id: str) -> None:
    await storm_engine.current().resume(session_id)


if __name__ == "__main__":
    unittest.main()
