"""The watcher records its ticks and step failures; server.py registers
every long-running task."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import background

try:
    from services.cowork_agent.visualizer import watcher as watcher_mod
    from services.cowork_agent.visualizer.state import watcher_heartbeat_path
except ImportError as exc:  # pragma: no cover - platform gate
    raise unittest.SkipTest(f"watcher needs POSIX: {exc}") from exc

ROOT = Path(__file__).resolve().parents[1]


class WatcherRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        background.reset_for_tests()
        self.addCleanup(background.reset_for_tests)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "state").mkdir()
        (root / "projects").mkdir()
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(root / "state"),
                                      "XO_PROJECTS_ROOT": str(root / "projects"),
                                      "QUIRQ_WATCHER_SOURCE_MODE": "active",
                                      "XO_SCHEDULER_ENABLED": "false"})
        env.start()
        self.addCleanup(env.stop)
        with (patch.object(watcher_mod, "get_active_agent", return_value=SimpleNamespace(name="stub")),
              patch.object(watcher_mod, "try_load_capability", return_value=None)):
            self.watcher = watcher_mod.Watcher()

    def test_a_failing_step_is_counted_and_published_in_the_heartbeat(self) -> None:
        with patch.object(watcher_mod.ws_stats, "apply", side_effect=ValueError("bad stats")):
            with self.assertLogs(watcher_mod.logger, level="ERROR"):
                self.watcher.tick()
        self.assertEqual(len(self.watcher.step_errors), 1)
        self.assertIn("workspace tier", self.watcher.step_errors[0])
        self.assertIn("ValueError: bad stats", self.watcher.step_errors[0])
        beat = json.loads(watcher_heartbeat_path().read_text(encoding="utf-8"))
        self.assertEqual(beat["step_errors"], 1)

    def test_a_clean_tick_has_no_step_errors(self) -> None:
        self.watcher.tick()
        self.assertEqual(self.watcher.step_errors, [])

    def test_a_step_error_whose_str_raises_still_writes_the_heartbeat(self) -> None:
        class Unprintable(ValueError):
            def __str__(self) -> str:
                raise RuntimeError("no string for you")

        with patch.object(watcher_mod.ws_stats, "apply", side_effect=Unprintable()):
            with self.assertLogs(watcher_mod.logger, level="ERROR"):
                self.watcher.tick()
        self.assertEqual(len(self.watcher.step_errors), 1)
        self.assertIn("Unprintable", self.watcher.step_errors[0])
        beat = json.loads(watcher_heartbeat_path().read_text(encoding="utf-8"))
        self.assertEqual(beat["step_errors"], 1)

    def test_run_records_a_failed_tick_then_a_clean_one(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        background.register(watcher_mod.WATCHER_COMPONENT, loop.create_future())
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError

        failing = [True, False]

        def tick():
            self.watcher.step_errors = ["workspace tier: ValueError: x"] if failing.pop(0) else []

        with patch.object(self.watcher, "tick", side_effect=tick), \
             patch.object(watcher_mod.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                loop.run_until_complete(self.watcher.run())
        record = background.snapshot()["watcher"]
        self.assertEqual(record["ticks"], 2)
        self.assertEqual(record["consecutive_failures"], 0)
        self.assertIn("1 step(s) failed", record["last_failure"])


class ServerRegistersTasksTests(unittest.TestCase):
    """lifespan is too entangled to run in a unit test; its source is checked."""

    def test_every_long_running_task_is_registered(self) -> None:
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        for name, handle in (("gateway reconcile", "_mcp_gateway_task"), ("usage sync", "_sync_task"),
                             ("github poller", "_github_poll_task"),
                             ("connections poller", "_connections_poll_task"),
                             ("watcher", "_watcher_task"), ("relay poller", "_relay_task")):
            with self.subTest(name=name):
                self.assertRegex(source, rf'background\.register\(\s*"{re.escape(name)}",\s*{handle}\b')
        self.assertRegex(source, r'background\.register\(\s*"gateway reconcile",\s*_mcp_gateway_task,\s*finishes_by_design=True')


if __name__ == "__main__":
    unittest.main()
