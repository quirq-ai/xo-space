"""Incident acceptance flows through the real engine with subscription-free fake CLIs."""

from datetime import datetime, timezone
import asyncio
import unittest

from tests.fakes import claude
from tests.test_storm_chats_flows import PC, FlowTestCase, next_input, start
from tests.test_storm_engine import EngineTestCase
from tests.storm_engine_kit import SID, message


class GoalIncidentTests(FlowTestCase):
    async def test_quiet_five_hour_goal_gets_one_recovery_then_stalls_without_an_all_night_loop(self):
        end = datetime.fromtimestamp(self.kit.clock.time() + 5 * 3600, timezone.utc).isoformat()
        await self.boot_roles({PC: [
            *start("pc", bind="MID"),
            claude.mcp_call("set_goal", {"message_id": "$MID", "title": "Watch the build", "ends_at": end}),
            claude.mcp_call("no_reply_needed", {"reason": "Goal recorded."}),
            claude.result("pc", "ok"), claude.idle("pc"),
            next_input(),
            claude.mcp_call("no_reply_needed", {"reason": "No useful progress."}),
            claude.result("pc", "ok"), claude.idle("pc"),
        ]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Watch the build for five hours.")
        await self.settle(lambda: self.rows("SELECT state FROM goals") == [("stalled",)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE reason = 'goal'"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM turns"), [(2,)])
        for _ in range(4):
            self.kit.clock.advance(3600)
            await self.chats.keeper.sweep()
        self.assertEqual(self.rows("SELECT state FROM goals"), [("stalled",)])
        self.assertEqual(self.rows("SELECT count(*) FROM turns"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE client_id LIKE 'stalled:%'"), [(1,)])
        self.kit.clock.advance(3601)
        await self.chats.keeper.sweep()
        self.assertEqual(self.rows("SELECT state FROM goals"), [("expired",)])


class LoadTests(EngineTestCase):
    async def test_ten_processes_complete_and_shutdown_leaves_no_children(self):
        self.kit.scenario(self.started({"sleep": 0.5}, *claude.text_turn(SID, "READY"), claude.idle(SID)))
        engine = await self.kit.boot()
        sessions = [await self.kit.session() for _ in range(10)]
        await asyncio.gather(*(engine.deliver(sid, message(i)) for i, sid in enumerate(sessions)))
        async with self.kit.pacing():
            await self.kit.until(lambda: self.kit.rows("SELECT count(*) FROM turns WHERE status = 'finished'") == [(10,)],
                                 timeout=30)
        self.assertEqual(len(self.kit.runs()), 10)
        self.assertEqual(self.kit.rows("SELECT count(*) FROM wakes WHERE state = 'consumed'"), [(10,)])
        # Kit.close checks both the process registry and launched children after shutdown.

    async def test_opt_in_load_script_uses_an_isolated_store_and_cleans_its_processes(self):
        from argparse import Namespace
        import os
        from pathlib import Path
        import shutil
        from unittest.mock import AsyncMock, patch
        from dataclasses import replace
        from scripts.storm_load import rehearsal
        from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
        from services.cowork_agent.adapters.contract import PaidUsage, UsageSnapshot, UsageWindow
        from services.cowork_agent.storm import engine as storm
        from tests.storm_engine_kit import AGENT

        self.kit.scenario(self.started(*claude.text_turn(SID, "READY"), claude.idle(SID)))
        runtime = ClaudeRuntime(claude_home=self.kit.home / ".claude")
        info = replace(await runtime.describe(), account="test-subscription")
        snapshot = UsageSnapshot(account=info.account, source="measured", paid=PaidUsage(possible=False),
                                 windows=(UsageWindow(name="window", utilization=0.1),))
        clean = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
        with patch.dict(os.environ, clean, clear=True), patch.object(storm, "load_runtime", return_value=runtime), \
                patch.object(runtime, "read_usage", AsyncMock(return_value=snapshot)), \
                patch.object(runtime, "describe", AsyncMock(return_value=info)):
            report = await rehearsal(Namespace(agent=AGENT, model="haiku", sessions=5, run=True))
        self.assertEqual(report["finished"], 5)
        store = Path(report["store"])
        self.addCleanup(shutil.rmtree, store.parents[2])
        self.assertNotEqual(store, self.kit.state / "engine" / "xo.db")
        self.assertEqual(os.environ["QUIRQ_STATE_ROOT"], str(self.kit.state))


class LoadPreflightTests(unittest.TestCase):
    def test_load_requires_fresh_windows_and_explicitly_disabled_paid_usage(self):
        from scripts.storm_load import subscription_ready
        from services.cowork_agent.adapters.contract import PaidUsage, UsageSnapshot, UsageWindow
        from dataclasses import replace
        good = UsageSnapshot(account="test", source="measured", paid=PaidUsage(possible=False),
                             windows=(UsageWindow(name="window", utilization=0.1),))
        self.assertTrue(subscription_ready(good))
        for bad in (replace(good, paid=None), replace(good, paid=PaidUsage(possible=True)),
                    replace(good, paid=PaidUsage(possible=False, in_use=True)), replace(good, overage=True),
                    replace(good, windows=()), replace(good, source="reported"),
                    replace(good, windows=(UsageWindow(name="window", utilization=0.8),))):
            self.assertFalse(subscription_ready(bad))
