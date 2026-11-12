"""Thread status follows work records, not an agent's optimistic checklist."""

from services.cowork_agent.storm.chats import rows, thread_status
from services.cowork_agent.storm.control import founder
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

    async def status(self):
        return await self.kit.engine.store.read(lambda conn: thread_status.read(
            conn, rows.get_thread(conn, self.chat["id"], self.root["id"]), now=self.kit.clock.time()))

    async def test_queued_is_waiting_idle_open_is_stalled_result_is_done(self):
        self.assertEqual((await self.status())["state"], "waiting")
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE session_id = ?", (self.sid,))
        self.assertEqual((await self.status())["state"], "stalled")
        await self.call(self.token_, "submit_result", summary="Fixed.")
        self.assertEqual((await self.status())["state"], "done")

    async def test_founder_control_overrides_queued_work(self):
        await founder.pause(self.kit.engine, "chat", self.chat["id"])
        self.assertEqual((await self.status())["state"], "paused")
        await founder.stop(self.kit.engine, "chat", self.chat["id"])
        self.assertEqual((await self.status())["state"], "stopped")

    async def test_timer_explains_idle_wait(self):
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE session_id = ?", (self.sid,))
        await self.call(self.token_, "wake_me", at="2099-01-01T00:00:00Z", note="Check deployment.")
        found = await self.status()
        self.assertEqual((found["state"], found["wait_reason"], found["waiting_until"]),
                         ("waiting", "Check deployment.", "2099-01-01T00:00:00Z"))

    async def test_coordinator_stop_holds_timer_wakes_until_explicit_followup(self):
        from services.cowork_agent.storm.engine import Wake
        await self.call(self.pc_token, "stop_thread", thread_id=self.root["id"], reason="Wait for review.")
        self.assertEqual((await self.status())["state"], "stopped")
        inserted = await self.kit.engine.write(lambda w: w.wake(self.sid, Wake(id="timer:late", text="Continue.")))
        self.assertFalse(inserted)
        await self.call(self.pc_token, "message_thread", thread_id=self.root["id"], note="Continue the work.")
        self.assertEqual((await self.status())["state"], "waiting")

    async def test_stream_publishes_state_after_a_result(self):
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE session_id = ?", (self.sid,))
        await self.call(self.token_, "submit_result", summary="Fixed.")
        await self.kit.until(lambda: self.rows("SELECT json_extract(data, '$.state') FROM events WHERE "
                                              "type = 'thread' AND thread_ts = ? ORDER BY seq DESC LIMIT 1",
                                              (self.root["id"],)) == [("done",)])
