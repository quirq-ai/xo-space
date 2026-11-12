"""A thread's state follows the store's records, not an agent's last word;
the API keeps the words the Space's UI knows."""

import json

from services.cowork_agent.storm.chats import rows, thread_status
from services.cowork_agent.storm.control import founder
from services.cowork_agent.storm.engine import Wake
from tests.storm_comms_kit import CHOICE, CommsTestCase


class ThreadStatusTests(CommsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.root = await self.owner_message(self.chat, "Fix the login.")
        made = await self.call(self.pc_token, "create_task", message_id=self.root["id"], title="Login",
                               instructions="Fix it.", **CHOICE)
        self.sid = made["session_id"]
        self.token_ = await self.token(self.sid)

    async def status(self) -> dict:
        return await self.kit.engine.store.read(lambda conn: thread_status.read(
            conn, rows.get_thread(conn, self.chat["id"], self.root["id"]), now=self.kit.clock.time()))

    async def both(self) -> tuple[str, str]:
        """The thread's true state and the word the API gives for it."""
        found = await self.status()
        self.assertIn(found["state"], thread_status.API_STATES)      # the only words the Space's UI knows
        self.assertIn(found["activity"], thread_status.ACTIVITIES)
        return found["activity"], found["state"]

    async def idle(self) -> None:
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE session_id = ?", (self.sid,))

    async def test_queued_is_waiting_idle_open_is_stalled_result_is_done(self) -> None:
        self.assertEqual(await self.both(), ("waiting", "working"))
        await self.idle()
        self.assertEqual(await self.both(), ("stalled", "working"))
        await self.call(self.token_, "submit_result", summary="Fixed.")
        self.assertEqual(await self.both(), ("done", "done"))

    async def test_an_idle_thread_shows_what_its_agent_last_reported_and_is_never_working_for_it(self) -> None:
        await self.idle()
        for reported, shown in (("done", "done"), ("needs_reply", "needs_reply"), ("working", "working")):
            with self.subTest(reported=reported):
                def card(w, reported=reported) -> None:
                    seq = w.append("card", {"ts": self.root["id"], "card": {"state": reported}},
                                   chat_id=self.chat["id"], thread_ts=self.root["id"])
                    w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, ?, ?)",
                              (self.chat["id"], self.root["id"], seq, json.dumps({"state": reported}), w.now))
                await self.kit.engine.write(card)
                self.assertEqual(await self.both(), ("stalled", shown))     # its task is open and nothing runs

    async def test_founder_control_overrides_queued_work(self) -> None:
        await founder.pause(self.kit.engine, "chat", self.chat["id"])
        self.assertEqual(await self.both(), ("paused", "paused"))
        await founder.stop(self.kit.engine, "chat", self.chat["id"])
        self.assertEqual(await self.both(), ("stopped", "failed"))           # the UI's word for failed is "Stopped"

    async def test_timer_explains_idle_wait(self) -> None:
        await self.idle()
        await self.call(self.token_, "wake_me", at="2099-01-01T00:00:00Z", note="Check deployment.")
        found = await self.status()
        self.assertEqual((found["activity"], found["state"], found["wait_reason"], found["waiting_until"]),
                         ("waiting", "working", "Check deployment.", "2099-01-01T00:00:00Z"))

    async def test_a_question_for_the_founder_is_a_wait_the_api_shows_as_needs_reply(self) -> None:
        await self.idle()
        await self.call(self.token_, "ask", to="founder", kind="question", title="Which branch?", why="Two exist.",
                        blocking=True)
        found = await self.status()
        self.assertEqual((found["activity"], found["state"], found["wait_reason"]),
                         ("waiting", "needs_reply", "Which branch?"))

    async def test_a_paused_session_is_a_paused_thread_saying_why(self) -> None:
        """A thread continued elsewhere has its session paused, and the UI
        marks it "Continued" from ``paused`` and its link."""
        await self.idle()
        await self.kit.engine.write(lambda w: w.execute(
            "UPDATE sessions SET paused = 'continued elsewhere (moved to codex)' WHERE id = ?", (self.sid,)))
        found = await self.status()
        self.assertEqual((found["activity"], found["state"], found["wait_reason"]),
                         ("paused", "paused", "continued elsewhere (moved to codex)"))

    async def test_a_done_task_whose_agent_left_something_for_the_founder_still_needs_a_reply(self) -> None:
        await self.idle()
        await self.call(self.token_, "submit_result", summary="Fixed.")

        def card(w) -> None:
            seq = w.append("card", {"ts": self.root["id"], "card": {"state": "needs_reply"}},
                           chat_id=self.chat["id"], thread_ts=self.root["id"])
            w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, ?, ?)",
                      (self.chat["id"], self.root["id"], seq, json.dumps({"state": "needs_reply"}), w.now))
        await self.kit.engine.write(card)
        self.assertEqual(await self.both(), ("done", "needs_reply"))

    async def test_coordinator_stop_holds_timer_wakes_until_explicit_followup(self) -> None:
        await self.call(self.pc_token, "stop_thread", thread_id=self.root["id"], reason="Wait for review.")
        self.assertEqual(await self.both(), ("stopped", "failed"))
        inserted = await self.kit.engine.write(lambda w: w.wake(self.sid, Wake(id="timer:late", text="Continue.")))
        self.assertFalse(inserted)
        await self.call(self.pc_token, "message_thread", thread_id=self.root["id"], note="Continue the work.")
        self.assertEqual(await self.both(), ("waiting", "working"))

    async def test_agents_are_told_the_true_state(self) -> None:
        await self.idle()
        listed = await self.call(self.pc_token, "list_thread_sessions")
        self.assertEqual([(t["state"], t["wait_reason"]) for t in listed["threads"]],
                         [("stalled", "open task has no running turn or pending wake")])

    async def test_stream_publishes_state_after_a_result(self) -> None:
        await self.idle()
        await self.call(self.token_, "submit_result", summary="Fixed.")
        await self.kit.until(lambda: self.rows(
            "SELECT json_extract(data, '$.state'), json_extract(data, '$.activity') FROM events WHERE "
            "type = 'thread' AND thread_ts = ? ORDER BY seq DESC LIMIT 1", (self.root["id"],)) == [("done", "done")])

    async def test_the_founders_stop_is_published_to_the_threads_in_its_scope_only(self) -> None:
        other = await self.project("other")
        await founder.stop(self.kit.engine, "chat", other["id"])
        await founder.stop(self.kit.engine, "chat", self.chat["id"])
        await self.kit.until(lambda: self.rows(
            "SELECT json_extract(data, '$.state'), json_extract(data, '$.activity') FROM events WHERE "
            "type = 'thread' AND thread_ts = ? ORDER BY seq DESC LIMIT 1", (self.root["id"],))
            == [("failed", "stopped")])

    async def test_panel_snapshot_contains_current_work_and_cannot_cross_chat_boundaries(self):
        goal = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login goal",
                               ends_at="2099-01-01T00:00:00Z")
        await self.call(self.token_, "wake_me", at="2099-01-01T00:00:00Z", note="Check the build.")
        await self.call(self.token_, "lease", resource="browser-seat")
        response = await self.http.get(f"/api/chat/chats/{self.chat['id']}/work")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual([g["id"] for g in data["goals"]], [goal["goal_id"]])
        self.assertEqual([t["thread_ts"] for t in data["tasks"]], [self.root["id"]])
        self.assertNotIn("instructions", data["tasks"][0])
        self.assertNotIn("request_key", data["tasks"][0])
        self.assertEqual(data["timers"][0]["note"], "Check the build.")
        self.assertEqual(data["leases"][0]["resource"], "browser-seat")
        other = await self.project("other")
        empty = (await self.http.get(f"/api/chat/chats/{other['id']}/work")).json()
        self.assertTrue(all(empty[key] == [] for key in ("goals", "tasks", "timers", "leases")))
