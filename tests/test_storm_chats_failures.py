"""Storm's conversation under failure and races (design ch 11 §2, §3.1, §5,
§6.2, §9, §16.2; sedc 09 #12): when an input ends, which calls answer it, a
pause on an idle thread, a coordinator recycled only between inputs, a
decision model's routing that survives a retry and a restart, and a project
created under the name its folder gets. On the fake claude, through the
routes and the real comms endpoint.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import unittest
from unittest import mock

import services.decision_models as dm
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import chats as storm_chats
from services.cowork_agent.storm.chats import ChatRefused
from services.cowork_agent.storm.chats import delivery, recycle, router, rows
from services.cowork_agent.storm.chats.lifecycle import Lifecycle
from services.cowork_agent.storm.engine import Wake
from services.cowork_agent.storm.engine.memory import MemoryReading
from tests.fakes import claude
from tests.test_storm_chats import _request
from tests.test_storm_chats_flows import PC, TH, FlowTestCase, done, next_input, start


def _intent(chosen: str, p: float) -> dm.Answer:
    return dm.Answer(router.INTENT_ID, "choice", chosen=chosen, probability=p, probabilities={chosen: p})


def _coordinator_id(case: FlowTestCase) -> str:
    return case.rows("SELECT id FROM sessions WHERE role = 'project-coordinator' AND retired_at IS NULL")[0][0]


class InputEndTests(FlowTestCase):
    async def test_an_answer_in_claudes_second_result_is_not_gated(self) -> None:
        """A worker finishing wakes a second turn; the input ends at
        ``SessionIdle``, judged over both turns (ch 11 §5)."""
        await self.boot_roles({PC: [
            *start("pc"),
            claude.task_started("pc", "task-1"),
            *claude.text_turn("pc", "Asked a worker to read the code.", message_id="m1"),    # result 1: no comms
            {"sleep": 0.5},
            claude.system("pc", "task_notification", task_id="task-1", status="completed", summary="read"),
            *claude.streamed("pc", "m2", [("text", "The worker is back.")]),               # claude's own turn
            claude.mcp_call("post_message", {"text": "The build is green."}),
            claude.result("pc", "done"),
            claude.idle("pc")]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Is the build green?")
        pc = _coordinator_id(self)
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (pc,))[0][0] == 1)
        self.assertEqual(self.rows("SELECT origin FROM turns WHERE session_id = ? ORDER BY started_at", (pc,)),
                         [("person",), ("injected",)])
        ended = json.loads(self.rows("SELECT data FROM events WHERE type = 'input.ended'")[0][0])
        self.assertEqual((len(ended["turns"]), ended["answered"]), (2, True))
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])

    async def test_the_boot_pass_leaves_an_input_in_claudes_worker_gap_to_its_idle(self) -> None:
        """B3 §9.5: boot's pass over inputs that ended unheard can run while
        a session it re-admitted is in claude's worker gap. Its first result
        is not the input's end: it is judged at its idle, over both turns,
        and isn't gated."""
        await self.boot_roles({PC: [
            *start("pc"),
            claude.task_started("pc", "task-1"),
            *claude.text_turn("pc", "Asked a worker.", message_id="m1"),
            claude.system("pc", "task_notification", task_id="task-1", status="completed", summary="read"),
            {"sleep": 1.5},
            *claude.streamed("pc", "m2", [("text", "The worker is back.")]),
            claude.mcp_call("post_message", {"text": "The build is green."}),
            claude.result("pc", "done"),
            claude.idle("pc")]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Is the build green?")
        pc = _coordinator_id(self)
        await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'run.finished' "
                                               "AND session_id = ?", (pc,))[0][0] == 1, 10)
        lifecycle = next(x for x in self.kit.engine._listeners if isinstance(x, Lifecycle))
        await lifecycle.recover()
        await self.settle(lambda: not self.chats._tasks)
        self.assertEqual(self.rows("SELECT count(*) FROM turns WHERE session_id = ?", (pc,)), [(1,)])   # the gap
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'"), [(0,)])
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 1)
        ended = json.loads(self.rows("SELECT data FROM events WHERE type = 'input.ended'")[0][0])
        self.assertEqual((len(ended["turns"]), ended["answered"]), (2, True))
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])

    async def test_a_decision_models_calls_answer_nothing_of_the_coordinators(self) -> None:
        """Calls made on the coordinator's behalf belong to no turn of its
        own: its next silent input is still gated (ch 11 §3.1, §5)."""
        await self.boot_roles({
            PC: [*start("pc"), claude.mcp_call("post_message", {"text": "Hi."}), *done("pc"),
                 next_input(), *done("pc", 2),                                         # the status wake: silent
                 next_input(), claude.mcp_call("no_reply_needed", {"reason": "gated"}), *done("pc", 3)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Done."}), *done("th")]})
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        ids = await self.chat_ids()
        answers = iter([_intent("chat", 0.9), _intent("new_task", 0.95)])

        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            return dm.Result("jev", {router.INTENT_ID: next(answers)})
        with mock.patch.object(dm, "ask", ask):
            await self.post(ids["demo"], "hello")                       # handed over: the coordinator answers
            await self.settle(lambda: ("agent", "Hi.") in self.texts(ids["demo"]))
            await self.post(ids["demo"], "Add a CSV export")            # routed by jev as the coordinator
            await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%' AND "
                                                "state = 'consumed'")[0][0] == 1)
        pc = _coordinator_id(self)
        on_behalf = self.rows("SELECT turn_id, json_extract(data, '$.decision') FROM events WHERE type = 'comms.call' "
                              "AND session_id = ? AND json_extract(data, '$.tool') = 'start_thread_session'", (pc,))
        decision = self.rows("SELECT id FROM router_decisions WHERE decided_by = 'jev' AND "
                             "json_extract(route, '$.actions[0].kind') = 'start_thread'")[0][0]
        self.assertEqual(on_behalf, [(None, decision)])
        # its hop points to the decision that caused it (ch 11 §16.2)
        self.assertEqual(self.rows("SELECT cause FROM hops WHERE kind = 'brief'"), [(decision,)])
        agent_actions = [json.loads(r)["actions"] for (r,) in self.rows(
            "SELECT route FROM router_decisions WHERE decided_by = 'agent'")]
        self.assertNotIn("start_thread", [a["kind"] for actions in agent_actions for a in actions])

    async def test_an_input_judged_after_its_session_retired_is_unanswered(self) -> None:
        """A retired session takes no more input: no gate for it, the
        input is recorded unanswered."""
        await self.boot_roles({PC: [*start("pc"), {"sleep": 1.0}, *done("pc")]})        # silent
        ids = await self.chat_ids()
        await self.post(ids["demo"], "hello")
        pc = _coordinator_id(self)
        await self.kit.until(lambda: self.rows("SELECT count(*) FROM turns WHERE session_id = ? AND status = "
                                               "'running'", (pc,))[0][0] == 1, 10)
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, pc)))
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 1)
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'turn.unanswered'"), [(1,)])

    async def test_an_input_that_ended_before_a_restart_is_judged_at_boot(self) -> None:
        """The thread replied and its turn finished, but the server stopped
        before the conversation judged it: the next boot does (card, status
        hop to the coordinator)."""
        await self.boot()
        chat = await self.project()
        pc = await self.coordinator(chat)
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(await self.token(pc), "start_thread_session", message_id=root["id"],
                                  title="Fix login", instructions="Fix it.")
        th = started["session_id"]

        def running(w) -> None:                  # what the engine records: a turn took the thread's wakes
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES "
                      "('turn_t1', ?, 1, 'wake', 'running', ?)", (th, w.now))
            w.execute("UPDATE wakes SET state = 'taken', turn_id = 'turn_t1' WHERE session_id = ?", (th,))
        await self.kit.engine.write(running)
        await self.call(await self.token(th), "reply", text="Done: the form posts to /login now.")

        def finished(w) -> None:                 # and the turn ended
            w.execute("UPDATE turns SET status = 'finished' WHERE id = 'turn_t1'")
            w.execute("UPDATE wakes SET state = 'consumed' WHERE turn_id = 'turn_t1'")
            w.append("turn.finished", {"status": "finished"}, session_id=th, chat_id=chat["id"],
                     thread_ts=root["id"], turn_id="turn_t1")
        await self.chats.stop()                  # the server stops before judging it
        await self.kit.engine.write(finished)
        await storm_chats.shutdown()
        self.chats = storm_chats.boot(self.kit.engine, self.queue)
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (th,))[0][0] == 1)
        self.assertEqual([json.loads(c)["state"] for (c,) in self.rows("SELECT card FROM cards")], ["done"])
        self.assertEqual(self.rows("SELECT kind, to_session FROM hops WHERE kind = 'status'"), [("status", pc)])

    async def test_a_status_after_an_approval_says_what_the_owner_decided(self) -> None:
        """Live (run 2): the coordinator couldn't see the owner's approval and
        asked the owner to confirm it. The status after the input that
        carried it out says what the owner decided."""
        await self.boot()
        chat = await self.project()
        pc = await self.coordinator(chat)
        root = await self.owner_message(chat, "Delete hello.txt, asking me first")
        started = await self.call(await self.token(pc), "start_thread_session", message_id=root["id"],
                                  title="Cleanup", instructions="x")
        th = started["session_id"]
        asked = await self.call(await self.token(th), "ask_owner", kind="approval", title="Delete hello.txt",
                                why="You asked me to check first", blocking=True)
        await self.queue.decide(asked["review_id"], _request("approve"))
        [(wake_id,)] = self.rows("SELECT id FROM wakes WHERE reason = 'review-answered'")

        def running(w) -> None:                  # the thread's next input takes the answer
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES "
                      "('turn_d1', ?, 1, 'system', 'running', ?)", (th, w.now))
            w.execute("UPDATE wakes SET state = 'taken', turn_id = 'turn_d1' WHERE session_id = ?", (th,))
        await self.kit.engine.write(running)
        await self.call(await self.token(th), "reply", text="Deleted hello.txt.")

        def finished(w) -> None:
            w.execute("UPDATE turns SET status = 'finished' WHERE id = 'turn_d1'")
            w.execute("UPDATE wakes SET state = 'consumed' WHERE turn_id = 'turn_d1'")
            w.append("turn.finished", {"status": "finished"}, session_id=th, chat_id=chat["id"],
                     thread_ts=root["id"], turn_id="turn_d1")
        await self.chats.stop()
        await self.kit.engine.write(finished)
        await storm_chats.shutdown()
        self.chats = storm_chats.boot(self.kit.engine, self.queue)          # judged by the boot pass
        await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'status:%'")[0][0] == 1)
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id LIKE 'status:%'")
        self.assertIn(f"The owner decided review item {asked['review_id']} (“Delete hello.txt”): approve", text)
        self.assertIn(f"thread_id {root['id']}", text)
        self.assertTrue(wake_id.startswith(f"review:{asked['review_id']}:"))

    async def test_a_crash_that_is_retried_is_not_reported_until_it_gives_up(self) -> None:
        """Each retry of a crashed input runs it again: no card or status hop
        until the engine drops it; then one failed card (ch 11 §7.3)."""
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "T", "instructions": "x"}),
                 *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2)],
            TH: [*start("th"), {"exit": 1}]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Do it")
        await self.settle(lambda: len(self.rows("SELECT ts FROM threads")) == 1)
        session = self.rows("SELECT session_id FROM threads")[0][0]
        await self.settle(lambda: self.rows("SELECT count(*) FROM cards")[0][0] >= 1, timeout=60)
        await asyncio.sleep(0.5)
        failed = self.rows("SELECT count(*) FROM turns WHERE session_id = ? AND status = 'failed'", (session,))[0][0]
        self.assertGreater(failed, 1)                                   # it was retried
        self.assertEqual([json.loads(card)["state"] for (card,) in self.rows("SELECT card FROM cards")], ["failed"])
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'status'"), [(1,)])


class PauseTests(FlowTestCase):
    async def test_pausing_an_idle_thread_shows_on_it_and_a_reply_resumes_it(self) -> None:
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "T", "instructions": "x"}),
                 *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Done."}), *done("th"),
                 next_input(), claude.mcp_call("reply", {"text": "Went on."}), *done("th", 2)]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Do it")
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("done",)])
        session, ts = self.rows("SELECT session_id, ts FROM threads")[0]
        await self.settle(lambda: self.rows("SELECT status FROM sessions WHERE id = ?", (session,)) == [("warm",)])
        await self.kit.engine.pause(session, reason="stuck on a denied step")
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("paused",)])
        await self.post(ids["demo"], "please go on", thread_ts=ts)
        await self.settle(lambda: ("agent", "Went on.") in self.texts(ids["demo"], ts))
        self.assertFalse((await self.kit.engine.get_session(session))["paused"])

    async def test_a_retried_post_in_a_thread_stores_and_delivers_one_message(self) -> None:
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "T", "instructions": "x"}),
                 *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Done."}), *done("th")]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Do it")
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("done",)])
        session, ts = self.rows("SELECT session_id, ts FROM threads")[0]
        self.kit.reading = MemoryReading(pressure_avg10=100.0)          # hold the wake where it is
        first, second = await asyncio.gather(*(self.post(ids["demo"], "more", thread_ts=ts, client_id="same")
                                               for _ in range(2)))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE client_id = 'same'"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE session_id = ? AND id = ?",
                                   (session, f"msg:{first['id']}")), [(1,)])


class RecycleTests(FlowTestCase):
    async def _idle_old_coordinator(self, *more: dict) -> tuple[dict, str]:
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("post_message", {"text": "One."}), *done("pc"),
                                    *more]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "first")
        await self.settle(lambda: ("agent", "One.") in self.texts(ids["demo"]))
        old = _coordinator_id(self)
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (old,))[0][0] == 1)
        # The input's end also checks for a recycle, just after input.ended: let it finish before a test moves
        # the clock, or it may recycle the coordinator first.
        await self.settle(lambda: not self.chats._tasks)
        return ids, old

    async def test_a_coordinator_that_took_an_input_meanwhile_is_not_retired(self) -> None:
        """Due by age, but an input reaches it while its successor is
        prepared: it stays, and the message goes to it (ch 11 §6.2)."""
        ids, old = await self._idle_old_coordinator(next_input(), {"sleep": 20})
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)
        chats, real = self.chats, self.chats._coordinator

        async def meanwhile(chat):
            await self.kit.engine.write(lambda w: w.wake(old, Wake(id="status:x:y", text="a status", priority="next",
                                                                   reason="delegation-status")))
            await self.kit.until(lambda: self.rows("SELECT count(*) FROM turns WHERE session_id = ? AND "
                                                   "status = 'running'", (old,))[0][0] == 1, 10)
            return await real(chat)
        with mock.patch.object(chats, "_coordinator", meanwhile):
            said = await self.post(ids["demo"], "second")
        self.assertEqual(self.rows("SELECT retired_at FROM sessions WHERE id = ?", (old,)), [(None,)])
        self.assertEqual(self.rows("SELECT count(*) FROM sessions WHERE role = 'project-coordinator'"), [(1,)])
        self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"msg:{said['id']}",)), [(old,)])

    async def test_queued_wakes_and_their_hops_move_to_the_successor(self) -> None:
        ids, old = await self._idle_old_coordinator()
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)                   # due; its idle process is released
        await self.settle(lambda: self.rows("SELECT status FROM sessions WHERE id = ?", (old,)) == [("cold",)])
        self.kit.reading = MemoryReading(pressure_avg10=100.0)          # nothing starts: the wakes stay queued
        chat = await self.chats.chat(ids["demo"])

        def queued(w) -> None:
            rows.add_hop(w, chat_id=chat["id"], thread_ts=None, kind="status", from_role="thread", from_session=None,
                         to_role="project-coordinator", to_session=old, wake_id="status:t:1")
            w.wake(old, Wake(id="status:t:1", text="a status", priority="later", reason="delegation-status"))
        await self.kit.engine.write(queued)
        said = await self.post(ids["demo"], "second")
        new = _coordinator_id(self)
        self.assertNotEqual(new, old)
        self.assertEqual([r[0] for r in self.rows("SELECT id FROM wakes WHERE session_id = ? ORDER BY rowid", (new,))],
                         [f"handoff:{old}", "status:t:1", f"msg:{said['id']}"])
        self.assertEqual(self.rows("SELECT to_session FROM hops WHERE wake_id = 'status:t:1'"), [(new,)])

    async def test_a_coordinator_in_claudes_worker_gap_is_not_retired(self) -> None:
        """A worker finished and claude's own second turn is coming: the
        engine is idle but the input isn't over, so a due coordinator stays
        and its answer arrives (ch 11 §5, §6.2)."""
        await self.boot_roles({PC: [
            *start("pc"),
            claude.task_started("pc", "task-1"),
            *claude.text_turn("pc", "Asked a worker.", message_id="m1"),
            claude.system("pc", "task_notification", task_id="task-1", status="completed", summary="read"),
            {"sleep": 1.5},                                                   # the gap before claude's own turn
            *claude.streamed("pc", "m2", [("text", "The worker is back.")]),
            claude.mcp_call("post_message", {"text": "The build is green."}),
            claude.result("pc", "done"),
            claude.idle("pc"),
            next_input(), claude.mcp_call("post_message", {"text": "Deploy is next."}), *done("pc", 2)]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Is the build green?")
        old = _coordinator_id(self)
        # Unpaced: the kit's pacing runs the fake clock, whose idle release would fire in the gap.
        await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'run.finished' "
                                               "AND session_id = ?", (old,))[0][0] == 1, 10)
        # Busy in the gap: the input is open until claude's own turn ends (ch 11 §6.3).
        self.assertEqual(self.rows("SELECT status FROM sessions WHERE id = ?", (old,)), [("running",)])
        with mock.patch.object(recycle, "reason", lambda *a, **k: "turns"):            # due, in the gap
            said = await self.post(ids["demo"], "And the deploy?")
            self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"msg:{said['id']}",)), [(old,)])
            await self.kit.until(lambda: ("agent", "Deploy is next.") in self.texts(ids["demo"]), 10)
        self.assertIn(("agent", "The build is green."), self.texts(ids["demo"]))
        self.assertEqual(self.rows("SELECT retired_at FROM sessions WHERE id = ?", (old,)), [(None,)])

    async def test_a_start_that_finds_it_retired_doesnt_happen(self) -> None:
        """An admission chose it before its successor took its wakes: the
        start finds it retired and opens nothing."""
        _ids, old = await self._idle_old_coordinator()
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)
        await self.settle(lambda: self.rows("SELECT status FROM sessions WHERE id = ?", (old,)) == [("cold",)])
        epoch = self.rows("SELECT epoch FROM sessions WHERE id = ?", (old,))[0][0]
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, old)))
        self.assertIsNone(await self.kit.engine._attach(old))
        self.assertEqual(self.rows("SELECT epoch, status FROM sessions WHERE id = ?", (old,)), [(epoch, "cold")])

    async def test_a_crashed_coordinator_whose_retry_cant_start_is_still_recycled(self) -> None:
        """Its input was cut short (the wake requeued) and no process runs it:
        nothing more of that input can happen, so it is recycled when due and
        the successor takes the requeued message (ch 11 §6.2)."""
        await self.boot_roles({PC: [*start("pc"), {"exit": 1}]})
        ids = await self.chat_ids()
        said = await self.post(ids["demo"], "first")
        old = _coordinator_id(self)
        await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'turn.finished' AND "
                                               "session_id = ?", (old,))[0][0] >= 1, 30)
        self.kit.reading = MemoryReading(pressure_avg10=100.0)          # the retry can't start
        await self.settle(lambda: self.rows("SELECT status FROM sessions WHERE id = ?", (old,))[0][0]
                          in ("cold", "queued"), timeout=30)
        await self.settle(lambda: not self.chats._tasks)
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND session_id = ?",
                                   (old,)), [(0,)])                        # never judged: it is to run again
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)
        self.assertEqual(await self.chats.recycle_if_due(await self.chats.chat(ids["demo"])), "age")
        new = _coordinator_id(self)
        self.assertNotEqual(new, old)
        self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"msg:{said['id']}",)), [(new,)])

    async def test_a_paused_coordinator_is_not_recycled(self) -> None:
        ids, old = await self._idle_old_coordinator()
        await self.kit.engine.pause(old, reason="three identical denials")
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)
        said = await self.post(ids["demo"], "second")
        self.assertEqual(_coordinator_id(self), old)
        self.assertEqual(self.rows("SELECT state FROM wakes WHERE id = ?", (f"msg:{said['id']}",)), [("queued",)])

    async def test_a_failing_login_check_refuses_no_message(self) -> None:
        """Deciding whether to recycle probes nothing; a successor that can't
        start leaves the live coordinator in place."""
        ids, old = await self._idle_old_coordinator(
            next_input(), claude.mcp_call("post_message", {"text": "Two."}), *done("pc", 2))
        runtime = self.kit.engine.runtime("claude_code")

        async def failing():
            raise c.RuntimeFailure("cli_missing", "claude --version failed (exit 1)")
        with mock.patch.object(runtime, "describe", failing):
            await self.post(ids["demo"], "second")                      # not due
            await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                                "session_id = ?", (old,))[0][0] == 2)
            self.kit.clock.advance(recycle.AGE_MAX_S + 1)
            with self.assertLogs("services.cowork_agent.storm.chats", level="WARNING") as logged:
                said = await self.post(ids["demo"], "third")            # due, but its successor can't start
        self.assertIn("cli_missing", "\n".join(logged.output))
        self.assertEqual(_coordinator_id(self), old)
        self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"msg:{said['id']}",)), [(old,)])


class RecycleReasonTests(unittest.TestCase):
    def _reason(self, *, inputs: int = 1, usage: dict | None = None, upgrade: bool = False,
                agent: bool = False) -> str | None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE sessions (id, created_at)")
        conn.execute("CREATE TABLE turns (session_id, origin, usage, started_at)")
        conn.execute("INSERT INTO sessions VALUES ('s', '2026-10-19T00:00:00Z')")
        conn.executemany("INSERT INTO turns VALUES ('s', 'person', ?, ?)",
                         [(json.dumps(usage) if usage else None, f"2026-10-19T00:{n:02d}:00Z") for n in range(inputs)])
        conn.execute("INSERT INTO turns VALUES ('s', 'injected', NULL, '2026-10-19T00:59:00Z')")
        now = datetime.datetime(2026, 10, 19, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
        return recycle.reason(conn, "s", now_s=now, fingerprint_changed=upgrade, agent_changed=agent)

    def test_each_reason(self) -> None:
        self.assertIsNone(self._reason())
        self.assertEqual(self._reason(inputs=recycle.TURNS_MAX), "turns")
        self.assertIsNone(self._reason(inputs=recycle.TURNS_MAX - 1))          # an injected turn isn't an input
        self.assertEqual(self._reason(usage={"context_tokens": recycle.CONTEXT_MAX_TOKENS + 1}), "context")
        self.assertEqual(self._reason(upgrade=True), "upgrade")
        self.assertEqual(self._reason(agent=True), "agent")

    def test_context_is_the_runtimes_measure_never_a_sum(self) -> None:
        # Six model calls on a 25k context: claude sums them per turn.
        summed = {"input": 900, "output": 2000, "cache_read": 125_000, "cache_write": 3000}
        self.assertIsNone(self._reason(usage={**summed, "context_tokens": 25_500}))
        self.assertIsNone(self._reason(usage=summed))                          # not reported: no context reason


class DecisionRoutingTests(FlowTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}),
                                    *done("pc")],
                               TH: [*start("th"), claude.mcp_call("reply", {"text": "ok"}), *done("th")]})
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        self.ids = await self.chat_ids()

    def decisions(self, message_id: str) -> list[tuple[str, dict]]:
        return [(d, json.loads(r)) for d, r in self.rows(
            "SELECT decided_by, route FROM router_decisions WHERE message_id = ? ORDER BY rowid", (message_id,))]

    async def test_overlapping_retries_route_once(self) -> None:
        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            await asyncio.sleep(0.2)
            return dm.Result("jev", {router.INTENT_ID: _intent("new_task", 0.95)})
        with mock.patch.object(dm, "ask", ask):
            first, second = await asyncio.gather(*(delivery.post(self.chats, self.ids["demo"], text="Add a CSV export",
                                                                 thread_ts=None, client_id="same") for _ in range(2)))
            await self.settle(lambda: not self.decisions(first["id"])[0][1].get("pending"))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([(d, r["actions"]) for d, r in self.decisions(first["id"])],
                         [("jev", [{"kind": "start_thread"}])])
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id = ?", (f"msg:{first['id']}",)), [(0,)])

    async def test_a_restart_mid_routing_hands_the_message_to_the_coordinator(self) -> None:
        never = asyncio.Event()

        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            await never.wait()                  # the server stops before the model answers
        url = f"/api/chat/chats/{self.ids['demo']}/messages"
        with mock.patch.object(dm, "ask", ask):
            said = (await self.http.post(url, json={"text": "Add a CSV export", "client_id": "c1"})).json()
            self.assertEqual([r for _d, r in self.decisions(said["id"])], [{"schema": router.SCHEMA, "pending": True}])
            await storm_chats.shutdown()        # the lifespan at a restart: the routing task is cancelled
            self.chats = storm_chats.boot(self.kit.engine, self.queue)
            again = (await self.http.post(url, json={"text": "Add a CSV export", "client_id": "c1"})).json()
            await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id = ?",
                                                (f"msg:{said['id']}",))[0][0] == 1)
        self.assertEqual(again["id"], said["id"])
        decided_by, route = self.decisions(said["id"])[0]
        self.assertEqual((decided_by, route["escalated_from"], route.get("pending")), ("jev", "jev", None))
        self.assertIn("restarted", route["reasons"][0])
        self.assertIn("server restarted while the jev decision model", self.rows(
            "SELECT envelope FROM wakes WHERE id = ?", (f"msg:{said['id']}",))[0][0])

    async def test_a_failing_model_hands_the_message_over(self) -> None:
        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            raise RuntimeError("boom")
        with mock.patch.object(dm, "ask", ask), self.assertLogs("services.cowork_agent.storm.chats", "ERROR"):
            said = await self.post(self.ids["demo"], "Add a CSV export")
            await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id = ?",
                                                (f"msg:{said['id']}",))[0][0] == 1)
        _by, route = self.decisions(said["id"])[0]
        self.assertEqual(route["reasons"], ["it failed (RuntimeError)"])


    async def test_a_coordinator_that_cant_take_it_leaves_a_note_for_the_person(self) -> None:
        """The POST answered long ago: when the coordinator can't start, the
        decision says so and the timeline tells the person."""
        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            return dm.Result("jev", {router.INTENT_ID: _intent("chat", 0.9)})        # not routed: handed over

        async def logged_out(pinned, purpose):
            raise ChatRefused("auth_required", f"claude_code can't {purpose}: it isn't logged in.", status=409)
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE role = "
                                                        "'project-coordinator'", (w.now,)))
        with mock.patch.object(dm, "ask", ask), mock.patch.object(self.chats, "runtime_for", logged_out), \
                self.assertLogs("services.cowork_agent.storm.chats", "ERROR"):
            said = await self.post(self.ids["demo"], "What's next?")
            await self.settle(lambda: not self.decisions(said["id"])[0][1].get("pending"))
        _by, route = self.decisions(said["id"])[0]
        self.assertIn("the coordinator could not take it", route["reasons"][-1])
        self.assertIn(("system", "This message reached no one: claude_code can't run this chat's project-coordinator: "
                                 "it isn't logged in. Send it again once that is fixed."), self.texts(self.ids["demo"]))


class ProjectNameTests(FlowTestCase):
    async def test_an_edited_name_creates_announces_and_hands_over(self) -> None:
        """The person types "Status Board"; its folder is status-board, and
        that is the project announced and handed the request (ch 11 §9)."""
        await self.boot()
        workspace = await self.chats.workspace()
        ws_token = await self.token(await self.coordinator(workspace))
        said = await self.owner_message(workspace, "Build a public status page")
        proposed = await self.call(ws_token, "propose_project", name="status-page", purpose="A public status page",
                                   why_new="No project serves it", context_message_ids=[said["id"]])
        await self.queue.decide(proposed["review_id"], _request("allow", answer="Status Board"))
        await self.settle(lambda: ("system", "Project status-board created.") in self.texts(workspace["id"]))
        self.assertTrue((self.kit.projects / "status-board").is_dir())
        project = await self.chats.project("status-board")
        self.assertIn(("owner", "Build a public status page"), self.texts(project["id"]))
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'deleg:%'"), [(1,)])
