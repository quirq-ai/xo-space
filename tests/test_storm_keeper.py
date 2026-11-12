"""A goal gets one recovery attempt per progress change, including across restarts."""

from services.cowork_agent.storm.chats import keeper
from services.cowork_agent.storm.records import goals, ledger
from tests.storm_comms_kit import CommsTestCase


class KeeperTests(CommsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()
        await self.chats.keeper.stop()  # These tests drive the durable state machine explicitly.
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.root = await self.owner_message(self.chat, "Watch the build for five hours.")
        self.goal = await self.kit.engine.write(lambda w: goals.create(
            w, chat_id=self.chat["id"], cites=self.root["id"], title="Watch the build",
            ends_at="2099-01-01T00:00:00Z", asked_by="founder", request_key="test-goal")[0])

    async def sweep(self):
        await self.kit.engine.write(lambda w: keeper.reconcile(w, self.goal.id, self.pc))

    def state(self):
        return self.rows("SELECT state FROM goals WHERE id = ?", (self.goal.id,))[0][0]

    async def test_one_wake_then_one_stall_message_until_new_progress(self):
        await self.sweep()
        await self.sweep()
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(1,)])
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE reason = 'goal'")
        await self.sweep()
        await self.sweep()
        self.assertEqual(self.state(), "stalled")
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE author_kind = 'system' "
                                  "AND client_id LIKE 'stalled:%'"), [(1,)])
        await self.owner_message(self.chat, "The build finished; check it.")
        await self.sweep()
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(2,)])
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_founder_pause_holds_goal_and_resume_restores_it(self):
        from services.cowork_agent.storm.control import founder
        await founder.pause(self.kit.engine, "chat", self.chat["id"])
        await self.sweep()
        self.assertEqual(self.state(), "paused")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(0,)])
        await founder.resume(self.kit.engine, "chat", self.chat["id"])
        await self.sweep()
        self.assertEqual(self.state(), "active")

    async def test_expiry_does_not_need_permission_to_start_a_coordinator(self):
        self.kit.clock.advance(100 * 365 * 24 * 3600)
        await keeper.GoalKeeper(self.chats).sweep()
        self.assertEqual(self.state(), "expired")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(0,)])

    async def test_recreated_keeper_does_not_repeat_a_stalled_attempt(self):
        await self.sweep()
        self.kit.execute("UPDATE wakes SET state = 'consumed' WHERE reason = 'goal'")
        await self.sweep()
        for _ in range(3):
            await keeper.GoalKeeper(self.chats).sweep()
        self.assertEqual(self.state(), "stalled")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(1,)])

    async def test_ending_a_goal_drops_its_queued_keeper_wake(self):
        await self.sweep()
        await self.kit.engine.write(lambda w: goals.move(
            w, goals.get(w, self.goal.id), state="done", reason="Finished.", actor=self.pc))
        self.assertEqual(self.rows("SELECT state FROM wakes WHERE reason = 'goal'"), [("dropped",)])

    async def test_account_hold_pauses_goal_until_pool_reopens(self):
        pool = self.rows("SELECT pool FROM sessions WHERE id = ?", (self.pc,))[0][0]
        self.kit.execute("INSERT OR REPLACE INTO pools (id, state, updated_at) VALUES (?, 'closed', ?)",
                         (pool, self.goal.created_at))
        await self.sweep()
        self.assertEqual(self.state(), "paused")
        self.kit.execute("UPDATE pools SET state = 'open' WHERE id = ?", (pool,))
        await self.sweep()
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(1,)])
