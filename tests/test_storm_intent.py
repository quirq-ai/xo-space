"""The founder's Stop and pause, kept on every path (plan W2; w2-w3 design
§3, §4, §6): the wake gate, the comms tools, admission, session creation,
the founder's acts and routes, and the replaced session's stuck answers.
Each test is one invariant or one incident; its number is the design's §7."""

from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm import plain
from services.cowork_agent.storm.chats import continuation, delivery
from services.cowork_agent.storm.control import founder, sweep
from services.cowork_agent.storm.control.marker import Mark
from services.cowork_agent.storm.engine import IntentRefused, Wake
from services.cowork_agent.storm.engine.memory import MemoryReading
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.records import intents
from services.launchpad import agent as deploy_agent
from tests.fakes import claude, install
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import SID, Kit, message
from tests.test_storm_control import SLEEP, Processes, running
from utils.commands import process_start_ticks, scheduler

FIRE = datetime(2026, 10, 22, 3, 30, 0, tzinfo=timezone.utc)
FIRE_STAMP = "2026-10-22T03:30:00Z"


def started(*after: dict) -> list[dict]:
    """A session process that takes its first input, then plays ``after``."""
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


#: A turn that runs a long tool, with a child of its own and one that left its process group, until interrupted.
LONG_TURN = started(claude.tool_call(SID, "t1", "Bash", {"command": "sleep 30"}),
                    {"spawn": ["sleep", SLEEP]}, {"spawn": ["sleep", SLEEP], "setsid": True},
                    *claude.host_control("interrupt"),
                    claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
                    claude.idle(SID))


async def retire(engine: storm_engine.Engine, old: str, successor: str | None) -> None:
    """Retire ``old`` as a recycle does, ``successor`` taking its place."""
    def write(w) -> None:
        w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?", (w.now, old))
        if successor is not None:
            w.execute("UPDATE sessions SET predecessor = ? WHERE id = ?", (old, successor))
    await engine.write(write)


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.procs = Processes()

    async def asyncTearDown(self) -> None:
        self.procs.close()
        await self.kit.close()

    async def spawned(self, count: int) -> list[dict]:
        """The processes the fake CLI started, once it has started ``count``
        (kept, so the test stops any still running at its end)."""
        await self.kit.until(lambda: len(self.kit.runs()) == 1
                             and len(self.kit.runs()[0].get("spawned", ())) >= count)
        out = self.kit.runs()[0]["spawned"]
        for entry in out:
            self.procs.started.append((entry["pid"], process_start_ticks(entry["pid"])))
        return out

    def queued(self, session_id: str) -> list[str]:
        return [r[0] for r in self.kit.rows("SELECT id FROM wakes WHERE session_id = ? AND state = 'queued' "
                                            "ORDER BY rowid", (session_id,))]


class StopTests(EngineCase):
    async def test_2_a_stop_ends_a_running_turn_within_a_second_and_leaves_no_marked_process(self) -> None:
        kit = self.kit
        kit.scenario(LONG_TURN)
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        turn = await kit.recorder.seen("turn.started", session_id=sid)
        await self.spawned(2)

        began = time.perf_counter()
        acted = await founder.stop(engine, "session", sid, cites="cmsg_stop")
        took = time.perf_counter() - began
        self.assertLess(took, 1.0, f"stop to every process gone took {took * 1000:.0f} ms")
        (halted,) = acted.halted
        self.assertEqual((halted.session_id, halted.turn_id, halted.interrupted, halted.released),
                         (sid, turn.turn_id, True, True))
        self.assertEqual(halted.swept.survived, ())
        for pid, ticks in self.procs.started:
            self.assertFalse(running(pid, ticks))
        self.assertEqual(sweep.scan(engine.space, {sid})[0], [])
        self.assertEqual(kit.rows("SELECT status FROM turns"), [("stopped",)])
        self.assertEqual(kit.recorder.of("turn.stopped", sid)[0].data["by"], "founder")
        self.assertEqual(acted.intent, intents.Intent("session", sid, "run", "stopped", None, "cmsg_stop",
                                                      acted.intent.set_at))

        # It stays ended: another agent's wake is refused, and the founder's own message is held.
        self.assertFalse(await engine.write(lambda w: w.wake(sid, Wake(id="deleg:1", text="more"))))
        self.assertEqual(kit.recorder.of("wake.refused", sid)[0].data,
                         {"id": "deleg:1", "reason": "stopped", "scope": {"scope": "session", "scope_id": sid}})
        self.assertTrue(await engine.deliver(sid, message(2, "and this")))
        await kit.until(lambda: engine._queued.get(sid) == f"stopped by the founder (session {sid})")
        self.assertEqual(len(kit.runs()), 1)
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))

    async def test_a_stop_also_ends_what_an_idle_session_left_running_and_nothing_else(self) -> None:
        engine = await self.kit.boot()
        sid = await self.kit.session()
        leftover = self.procs.start(["sleep", SLEEP], Mark(engine.space, sid, 0))     # a background job it left
        others = [self.procs.start(["sleep", SLEEP], mark) for mark in (Mark(engine.space, "cse_other", 0), None)]

        acted = await founder.stop(engine, "space", "")
        self.assertEqual(acted.halted, ())
        self.assertEqual([(p.pid, p.session_id, sig) for p, sig in acted.swept.killed],
                         [(leftover.pid, sid, "SIGTERM")])
        for proc in others:
            self.assertIsNone(proc.poll())
        (event,) = self.kit.recorder.of("scope.swept")
        self.assertEqual((event.data["scope"], event.data["reason"], event.data["found"]), ("space", "stopped", 1))

    async def test_a_turn_the_pause_cut_short_leaves_one_held_note_saying_so(self) -> None:
        kit = self.kit
        kit.scenario(LONG_TURN)
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        turn = await kit.recorder.seen("turn.started", session_id=sid)
        await self.spawned(2)

        acted = await founder.pause(engine, "session", sid)
        self.assertEqual([(h.turn_id, h.interrupted) for h in acted.halted], [(turn.turn_id, True)])
        self.assertEqual(self.queued(sid), [f"paused:{turn.turn_id}"])
        (note,) = kit.rows("SELECT envelope FROM wakes WHERE id = ?", (f"paused:{turn.turn_id}",))[0]
        self.assertIn("because the founder paused this work", note)
        self.assertEqual(sweep.scan(engine.space, {sid})[0], [])
        await kit.recorder.wait(lambda: not kit.rows("SELECT pid FROM procs"))


class ResumeTests(EngineCase):
    async def test_3_pause_then_five_wakes_then_resume_is_one_input_with_your_words_as_typed(self) -> None:
        kit = self.kit
        turn = [*claude.text_turn(SID, "done"), claude.idle(SID)]
        kit.fake = install("claude", kit.root / "bin", {"runs": [started(*turn), started(*turn)],
                                                        "argv": [claude.version(), claude.auth_status()]})
        os.environ["CLAUDE_CLI_PATH"] = str(kit.fake.path)
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "start"))
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)

        paused = await founder.pause(engine, "space", "")
        self.assertEqual([(h.session_id, h.interrupted) for h in paused.halted], [(sid, False)])
        words = "Ship it, but keep <the> old & new flags: exactly as I wrote."
        for n in range(1, 5):
            self.assertTrue(await engine.write(lambda w, n=n: w.wake(sid, Wake(id=f"deleg:{n}", text=f"note {n}"))))
        self.assertTrue(await engine.deliver(sid, message(2, words)))
        await kit.until(lambda: engine._queued.get(sid) == "paused by the founder (the whole Space)")
        self.assertEqual((await engine.get_session(sid))["state"], "queued")
        self.assertEqual(len(kit.runs()), 1)

        self.assertTrue(await founder.resume(engine, "space", ""))
        async with kit.pacing():
            await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        (user,) = [line for line in kit.runs()[1]["stdin"] if line.get("type") == "user"]
        text = user["message"]["content"][0]["text"]
        self.assertIn(f"[5 of 5: message]\n{words}", text)
        for n in range(1, 5):
            self.assertIn(f"note {n}", text)
        self.assertEqual(kit.rows("SELECT count(*) FROM turns WHERE session_id = ?", (sid,)), [(2,)])
        self.assertFalse(await founder.resume(engine, "space", ""))           # nothing left to lift


class SuccessorTests(EngineCase):
    async def test_5_an_answer_to_a_replaced_session_reaches_the_live_one(self) -> None:
        kit = self.kit
        kit.scenario([])
        kit.reading = MemoryReading(pressure_avg10=100.0)      # no process starts: the queue is what is tested
        engine = await kit.boot()
        first, second, live = [await kit.session() for _ in range(3)]
        await retire(engine, first, second)
        await retire(engine, second, live)
        answer = Wake(id="review:rv_1:7", text="The person answered: yes.", priority="now", reason="review",
                      origin="system")
        self.assertTrue(await engine.write(lambda w: w.wake(first, answer)))
        self.assertEqual(kit.rows("SELECT session_id, state FROM wakes"), [(live, "queued")])
        self.assertEqual([e.data for e in kit.recorder.of("wake.moved")], [{"id": answer.id, "from": first,
                                                                            "to": live}])
        # A line of successors that ends retired takes nothing, and says so.
        ended = await kit.session()
        await retire(engine, ended, None)
        self.assertFalse(await engine.write(lambda w: w.wake(ended, Wake(id="deleg:x", text="x"))))
        self.assertEqual(kit.recorder.of("wake.refused", ended)[0].data,
                         {"id": "deleg:x", "reason": "no_live_session", "scope": None})

    async def test_5_the_boot_moves_stuck_answers_to_the_successor_once_and_delivers_them(self) -> None:
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "got them"), claude.idle(SID)))
        engine = await kit.boot()
        old, new = await kit.session(), await kit.session()
        await retire(engine, old, new)
        await storm_engine.shutdown()
        # Answers that reached the replaced session before every wake went through the gate.
        for n in (1, 2):
            kit.execute("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, created_at) "
                        "VALUES (?, ?, 'next', 'review', ?, '{\"origin\": \"system\"}', 'queued', ?)",
                        (f"review:rv_{n}:1", old, f"answer {n}", f"2026-11-10T08:0{n}:00Z"))

        await kit.boot()
        await kit.recorder.seen("turn.finished", session_id=new)
        self.assertEqual([e.data for e in kit.recorder.of("wake.moved")],
                         [{"id": f"review:rv_{n}:1", "from": old, "to": new} for n in (1, 2)])
        self.assertEqual(kit.rows("SELECT id, session_id, state FROM wakes ORDER BY id"),
                         [(f"review:rv_{n}:1", new, "consumed") for n in (1, 2)])
        (user,) = [line for line in kit.runs()[0]["stdin"] if line.get("type") == "user"]
        self.assertIn("answer 1", user["message"]["content"][0]["text"])
        self.assertIn("answer 2", user["message"]["content"][0]["text"])
        await storm_engine.shutdown()
        await kit.boot()                                        # once: nothing is left to move
        self.assertEqual(len(kit.recorder.of("wake.moved")), 2)


class RouteTests(EngineCase):
    def client(self, host: str = "127.0.0.1") -> httpx.AsyncClient:
        application = FastAPI()
        for router in storm_routers:
            application.include_router(router)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=application, client=(host, 50000)),
                                base_url="http://127.0.0.1")

    async def test_the_founders_routes_stop_list_and_resume_from_this_machine_only(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        sid = await kit.session()
        async with self.client() as http, self.client("203.0.113.9") as remote:
            stopped = await http.post("/api/chat/intent/stop", json={"scope": "session", "scope_id": sid,
                                                                     "cites": "cmsg_1"})
            self.assertEqual(stopped.status_code, 200, stopped.text)
            body = stopped.json()
            self.assertEqual({k: body["intent"][k] for k in ("scope", "scope_id", "key", "value", "cites")},
                             {"scope": "session", "scope_id": sid, "key": "run", "value": "stopped",
                              "cites": "cmsg_1"})
            self.assertEqual((body["sessions"], body["dropped"], body["withdrawn"], body["halted"]),
                             ([sid], [], [], []))
            self.assertIn("ms", body)
            listed = (await http.get("/api/chat/intent")).json()["intents"]
            self.assertEqual([(i["scope"], i["scope_id"], i["value"]) for i in listed], [("session", sid, "stopped")])

            refused = await remote.post("/api/chat/intent/resume", json={"scope": "session", "scope_id": sid})
            self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (403, "not_local"))
            bad = await http.post("/api/chat/intent/pause", json={"scope": "thread", "scope_id": "no-slash"})
            self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "intent_invalid"))
            self.assertEqual((await http.post("/api/chat/intent/stop", json={"scope": "pool", "scope_id": "x"}))
                             .status_code, 422)

            resumed = await http.post("/api/chat/intent/resume", json={"scope": "session", "scope_id": sid})
            self.assertEqual(resumed.json(), {"resumed": True})
            self.assertEqual((await http.get("/api/chat/intent")).json(), {"intents": []})
        self.assertEqual(engine.holding(intents.chain(sid, role="plain", chat_id=None, thread_ts=None)), None)


class ThreadCase(CommsTestCase):
    """A project chat with its coordinator and one thread. No session's
    process starts (the memory guard holds them), so the tools are called
    with tokens minted for the sessions' epochs."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.engine = self.kit.engine
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.ts, self.thread = await self.start_thread("Fix the login page")
        self.thread_token = await self.token(self.thread)

    async def start_thread(self, text: str) -> tuple[str, str]:
        root = await self.owner_message(self.chat, text)
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title=text[:30],
                                  instructions="Go.", **CHOICE)
        return root["id"], started["session_id"]

    def scope(self, ts: str) -> str:
        return f"{self.chat['id']}/{ts}"

    def holds(self) -> set[tuple[str, str]]:
        return {(scope, scope_id) for scope, scope_id in self.rows("SELECT scope, scope_id FROM intents")}

    def waits(self, session_id: str) -> str | None:
        """Why the session waits, as its last ``session`` event said."""
        rows = self.rows("SELECT json_extract(data, '$.status'), json_extract(data, '$.reason') FROM events "
                         "WHERE type = 'session' AND session_id = ? ORDER BY seq DESC LIMIT 1", (session_id,))
        return rows[0][1] if rows and rows[0][0] == "queued" else None


class StoppedThreadTests(ThreadCase):
    async def test_1_a_stopped_thread_given_70_messages_never_runs_and_agents_hear_why(self) -> None:
        engine = self.engine
        (brief,) = [w[0] for w in self.wakes(self.thread)]
        acted = await founder.stop(engine, "thread", self.scope(self.ts))
        self.assertEqual(acted.dropped, (brief,))
        for n in range(35):
            sentence = await self.refused(self.pc_token, "message_thread", thread_id=self.ts, note=f"follow-up {n}")
            self.assertTrue(sentence.startswith("Stopped by the founder at "), sentence)
            self.assertFalse(await engine.write(lambda w, n=n: w.wake(self.thread, Wake(
                id=f"gate:{n}", text="check the gate", priority="now", origin="system"))))
        self.kit.reading = MemoryReading()             # nothing else holds it back now
        await engine._admission_pass()

        self.assertEqual(self.rows("SELECT count(*) FROM turns WHERE session_id = ?", (self.thread,)), [(0,)])
        self.assertNotIn(self.thread, engine._live)
        self.assertEqual(self.rows("SELECT id, state FROM wakes WHERE session_id = ?", (self.thread,)),
                         [(brief, "dropped")])
        self.assertEqual(len(self.kit.recorder.of("wake.refused", self.thread)), 35)
        self.assertEqual(self.rows("SELECT json_extract(data, '$.tool'), json_extract(data, '$.error'), count(*) "
                                   "FROM events WHERE type = 'comms.call' AND json_extract(data, '$.ok') = 0 "
                                   "GROUP BY 1, 2"), [("message_thread", "stopped_by_founder", 35)])
        self.assertEqual(self.rows("SELECT kind FROM hops WHERE thread_ts = ?", (self.ts,)), [("brief",)])

    async def test_8_xo_tools_refuse_a_held_scope_except_no_reply_needed(self) -> None:
        engine = self.engine
        await founder.stop(engine, "thread", self.scope(self.ts))
        sentence = await self.refused(self.thread_token, "reply", text="Done.")
        self.assertTrue(sentence.startswith("Stopped by the founder at "), sentence)
        self.assertIn(f"(thread {self.scope(self.ts)})", sentence)
        self.assertEqual(await self.call(self.thread_token, "no_reply_needed", reason="stopped"), {"ok": True})
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE text = 'Done.'"), [(0,)])
        await self.call(self.pc_token, "post_message", text="The coordinator, outside the thread, still works.")

        self.assertTrue(await founder.resume(engine, "thread", self.scope(self.ts)))
        await founder.pause(engine, "thread", self.scope(self.ts))
        self.assertTrue((await self.refused(self.thread_token, "update_status", text="x")).startswith(
            "Paused by the founder at "))
        self.assertEqual(self.rows("SELECT json_extract(data, '$.tool'), json_extract(data, '$.error') FROM events "
                                   "WHERE type = 'comms.call' AND json_extract(data, '$.ok') = 0 ORDER BY seq"),
                         [("reply", "stopped_by_founder"), ("update_status", "paused_by_founder")])


    async def test_8_an_ask_to_the_founder_goes_through_and_one_into_a_stopped_scope_is_refused(self) -> None:
        await founder.stop(self.engine, "coordinator", self.chat["id"])
        asked = await self.call(self.thread_token, "ask", to="founder", kind="approval", title="Merge the branch",
                                why="It is ready")
        self.assertEqual(self.rows("SELECT state FROM review_items WHERE id = ?", (asked["review_id"],)), [("open",)])
        self.assertEqual(self.kit.recorder.of("wake.refused", self.pc), [])     # an ask to the founder wakes no one
        # A message of its own to the stopped coordinator is refused whole.
        sentence = await self.refused(self.thread_token, "ask", to="coordinator", question="Status?")
        self.assertIn(f"(chat {self.chat['id']}'s coordinator)", sentence)
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'ask'"), [(0,)])


class PausedChatTests(ThreadCase):
    async def test_6_work_added_in_a_paused_chat_is_held_and_nothing_new_starts(self) -> None:
        engine = self.engine
        (brief,) = [w[0] for w in self.wakes(self.thread)]
        acted = await founder.pause(engine, "chat", self.chat["id"])
        self.assertEqual((acted.dropped, set(acted.sessions)), ((), {self.pc, self.thread}))
        # Another agent's or the system's wake is held, never refused; a steer too.
        self.assertTrue(await engine.write(lambda w: w.wake(self.thread, Wake(
            id="gate:1", text="check the gate", origin="system", delivery="add"))))
        await engine._admission_pass()
        self.assertEqual([w[0] for w in self.wakes(self.thread)], [brief, "gate:1"])
        self.assertEqual(self.waits(self.thread), f"paused by the founder (chat {self.chat['id']})")
        self.assertEqual((await engine.get_session(self.thread))["state"], "queued")
        # The chat's agents can't act, and nothing new starts in it.
        self.assertTrue((await self.refused(self.pc_token, "post_message", text="hi")).startswith(
            "Paused by the founder at "))
        anchor = await self.owner_message(self.chat, "Another task")
        with self.assertRaises(IntentRefused) as caught:
            await self.chats.start_thread(self.chat, anchor["id"], "Another", Intelligence(),
                                          lambda w, session_id, note: session_id)
        self.assertEqual(caught.exception.code, "paused_by_founder")


class YourMessageTests(ThreadCase):
    async def test_9_your_message_in_a_stopped_thread_lifts_that_thread_only(self) -> None:
        engine, chat_id = self.engine, self.chat["id"]
        other_ts, other = await self.start_thread("Write the docs")
        await founder.stop(engine, "thread", self.scope(self.ts))
        await founder.stop(engine, "thread", self.scope(other_ts))

        first = await delivery.post(self.chats, chat_id, text="Carry on, please.", thread_ts=self.ts, client_id="c-1")
        self.assertEqual(self.holds(), {("thread", self.scope(other_ts))})
        self.assertEqual([w[0] for w in self.wakes(self.thread) if w[0].startswith("msg:")], [f"msg:{first['id']}"])
        self.assertEqual(self.rows("SELECT state FROM wakes WHERE id = ?", (f"msg:{first['id']}",)), [("queued",)])

        # A stop on the whole project still holds the thread: your next message there waits, never refused.
        await founder.stop(engine, "chat", chat_id)
        second = await delivery.post(self.chats, chat_id, text="And this too.", thread_ts=self.ts, client_id="c-2")
        self.assertEqual(self.rows("SELECT id FROM wakes WHERE session_id = ? AND state = 'queued'", (self.thread,)),
                         [(f"msg:{second['id']}",)])
        await engine._admission_pass()
        self.assertEqual(self.waits(self.thread), f"stopped by the founder (chat {chat_id})")
        self.assertEqual(self.holds(), {("thread", self.scope(other_ts)), ("chat", chat_id)})

        # A timeline message lifts the coordinator alone.
        await founder.stop(engine, "coordinator", chat_id)
        await delivery.post(self.chats, chat_id, text="Status?", thread_ts=None, client_id="c-3")
        self.assertEqual(self.holds(), {("thread", self.scope(other_ts)), ("chat", chat_id)})
        self.assertEqual(self.rows("SELECT state FROM wakes WHERE session_id = ?", (other,)), [("dropped",)])


class CreationTests(ThreadCase):
    async def test_10_no_creator_starts_a_session_where_the_founder_stopped(self) -> None:
        engine, chat_id = self.engine, self.chat["id"]
        await founder.stop(engine, "chat", chat_id)
        sessions = self.rows("SELECT id FROM sessions ORDER BY id")

        # A thread (the coordinators' tool and a decision model start theirs through it).
        anchor = await self.owner_message(self.chat, "Another task")
        with self.assertRaises(IntentRefused) as thread:
            await self.chats.start_thread(self.chat, anchor["id"], "Another", Intelligence(),
                                          lambda w, session_id, note: session_id)
        self.assertEqual(thread.exception.code, "stopped_by_founder")
        # A job's run: skipped, and its record says why.
        definition = scheduler.validate_definition({"kind": "agent", "name": "Nightly", "chat": chat_id,
                                                    "prompt": "Summarise.", "every_seconds": 86400,
                                                    "first_run_at": "2026-10-22T09:00:00+05:30"})
        job = await engine.write(lambda w: storm_jobs.arm_job(w, definition, now=FIRE - timedelta(hours=1),
                                                              asked_by="founder", approved_by="founder", by="founder",
                                                              request_key="nightly"))
        record = await storm_jobs.current().run_fire(job, FIRE_STAMP)
        self.assertEqual(record["status"], "skipped")
        self.assertTrue(record["reason"].startswith("Stopped by the founder at "), record)
        # A Move: refused, and the old thread stays frozen (no quiet resume).
        with self.assertRaises(IntentRefused):
            await continuation.continue_thread(self.chats, self.chat, self.ts, agent=None, reason="move it",
                                               by="owner")
        self.assertIsNotNone(self.rows("SELECT paused FROM sessions WHERE id = ?", (self.thread,))[0][0])
        # The deploy thread.
        with self.assertRaises(IntentRefused):
            await deploy_agent.ask_agent("demo")
        # A coordinator, for a chat that has none.
        other = await self.project("other")
        await founder.stop(engine, "chat", other["id"])
        with self.assertRaises(IntentRefused):
            await self.coordinator(other)
        # A plain chat, while the Space is stopped.
        await founder.stop(engine, "space", "")
        to = await plain.route({"text": "hi", "client_id": "p-1"})
        with self.assertRaises(IntentRefused):
            await plain.prompt(engine, to, plain.Prompt(text="hi", client_id="p-1"))
        self.assertEqual(self.rows("SELECT id FROM sessions ORDER BY id"), sessions)


if __name__ == "__main__":
    unittest.main()
