from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.commit_relay import config, poller, status


class NudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        status.reset()
        poller.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(Path(self._tmp.name) / ".quirq"),
            "XO_PROJECTS_ROOT": str(Path(self._tmp.name) / "projects"),
            "XO_PROJECT_ID": "ws-a", "RELAY_POLL_JITTER_RATIO": "0",
        })
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_nudge_ends_the_wait_early(self) -> None:
        async def scenario():
            waiter = asyncio.create_task(poller.wait_for_next_tick(30.0, scan_every=0.05))
            await asyncio.sleep(0.02)
            poller.nudge()
            return await asyncio.wait_for(waiter, timeout=1.0)
        reason = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(reason, "nudge")

    def test_wait_times_out_without_nudge(self) -> None:
        async def scenario():
            return await poller.wait_for_next_tick(0.05, scan_every=0.02)
        reason = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(reason, "interval")

    def test_nudge_during_tick_yields_one_extra_tick(self) -> None:
        ticks = []

        async def fake_tick():
            ticks.append(1)
            if len(ticks) == 1:
                poller.nudge()          # arrives while a tick is running
            return 30.0

        async def scenario():
            with patch.object(poller, "run_tick", new=fake_tick):
                task = asyncio.create_task(poller.run_relay_poller())
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(len(ticks), 2)

    def test_new_clone_is_detected_by_the_scan(self) -> None:
        projects = Path(self._tmp.name) / "projects"
        projects.mkdir()

        async def scenario():
            waiter = asyncio.create_task(poller.wait_for_next_tick(30.0, scan_every=0.02))
            await asyncio.sleep(0.03)
            (projects / "new-repo" / ".git").mkdir(parents=True)
            return await asyncio.wait_for(waiter, timeout=1.0)
        reason = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(reason, "local_change")

    def test_repeated_poll_failures_log_once(self) -> None:
        lines = []
        loop = asyncio.new_event_loop()
        with patch.object(config, "auth_token", return_value="tok"), \
             patch.object(poller, "log_line", side_effect=lines.append), \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value=None)):
            for _ in range(3):
                loop.run_until_complete(poller.run_tick())
        failures = [l for l in lines if "unreachable" in l]
        self.assertEqual(len(failures), 1)
        with patch.object(config, "auth_token", return_value="tok"), \
             patch.object(poller, "log_line", side_effect=lines.append), \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": []})):
            loop.run_until_complete(poller.run_tick())
        self.assertTrue(any("recovered" in l for l in lines))
