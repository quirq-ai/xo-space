"""The loop watchdog names the code that blocks the event loop."""
import asyncio
import logging
import os
import time
import unittest
from unittest.mock import patch

from services import loop_watchdog
from services.loop_watchdog import LoopWatchdog


def block_the_loop_for(seconds: float) -> None:
    time.sleep(seconds)  # the culprit the log must name


class LoopWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_loop_is_logged_with_its_stack_and_length(self):
        watchdog = LoopWatchdog(asyncio.get_running_loop(), 0.3, beat_s=0.05)
        watchdog.start()
        try:
            with self.assertLogs("xo_space.loop_watchdog", logging.WARNING) as logs:
                await asyncio.sleep(0.1)
                block_the_loop_for(0.8)
                await asyncio.sleep(0.3)
        finally:
            await watchdog.stop()
        text = "\n".join(logs.output)
        self.assertIn("block_the_loop_for", text)
        self.assertIn("event loop was blocked for", text)
        self.assertEqual(len(watchdog.stalls), 1)
        stall = watchdog.stalls[0]
        self.assertGreaterEqual(stall.seconds, 0.5)
        self.assertIn("block_the_loop_for", stall.where)

    async def test_a_healthy_loop_logs_nothing(self):
        watchdog = LoopWatchdog(asyncio.get_running_loop(), 0.3, beat_s=0.05)
        watchdog.start()
        try:
            with self.assertNoLogs("xo_space.loop_watchdog", logging.WARNING):
                for _ in range(10):
                    await asyncio.sleep(0.05)
        finally:
            await watchdog.stop()
        self.assertFalse(watchdog.stalls)

    async def test_threshold_setting(self):
        for raw, expected in (("", 2.0), ("0", 0.0), ("5", 5.0), ("junk", 2.0), ("-1", 0.0)):
            with patch.dict(os.environ, {"QUIRQ_LOOP_STALL_S": raw}):
                self.assertEqual(loop_watchdog.threshold_s(), expected)
        with patch.dict(os.environ, {"QUIRQ_LOOP_STALL_S": "0"}):
            self.assertIsNone(loop_watchdog.start())
        with patch.dict(os.environ, {"QUIRQ_LOOP_STALL_S": "2"}):
            self.assertIsNotNone(loop_watchdog.start())
            await loop_watchdog.stop()
            self.assertIsNone(loop_watchdog.current())


if __name__ == "__main__":
    unittest.main()
