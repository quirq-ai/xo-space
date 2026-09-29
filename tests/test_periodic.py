"""``services.periodic.run_forever``, the loop both background pollers run
on, and the two pollers on top of it: their log lines and their
survive-a-failure, stop-on-cancel behaviour are the ones they had."""

from __future__ import annotations

import asyncio
import logging
import os
import unittest
from unittest.mock import AsyncMock, patch

from services import periodic
from services.connections import poller as connections_poller
from services.cowork_agent import github_poller

LOG = logging.getLogger("tests.periodic")


def _sleeps(stop_after: int):
    """A fake ``asyncio.sleep`` that records what it was asked for and cancels
    on the ``stop_after``-th call, so a test sees exact values and no timers."""
    calls: list[float] = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= stop_after:
            raise asyncio.CancelledError

    return calls, fake_sleep


class _Loop(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()

    def tearDown(self) -> None:
        self.loop.close()

    def run_(self, coro):
        return self.loop.run_until_complete(coro)


class RunForeverTests(_Loop):
    def test_returns_at_once_when_disabled(self) -> None:
        tick = AsyncMock()
        self.assertIsNone(self.run_(periodic.run_forever(
            "demo", tick, interval_s=lambda: 1.0, startup_delay_s=5.0, enabled=lambda: False)))
        tick.assert_not_awaited()

    def test_startup_delay_then_ticks_with_the_interval_re_read_each_pass(self) -> None:
        calls, fake_sleep = _sleeps(4)
        intervals = iter([1.0, 2.0, 3.0])
        tick = AsyncMock()
        with patch.object(periodic.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                self.run_(periodic.run_forever("demo", tick, interval_s=lambda: next(intervals),
                                               startup_delay_s=7.5, enabled=lambda: True))
        self.assertEqual(calls, [7.5, 1.0, 2.0, 3.0])
        self.assertEqual(tick.await_count, 3)

    def test_no_delay_and_no_gate_by_default(self) -> None:
        calls, fake_sleep = _sleeps(2)
        tick = AsyncMock()
        with patch.object(periodic.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                self.run_(periodic.run_forever("demo", tick, interval_s=lambda: 5.0))
        self.assertEqual(calls, [0.0, 5.0])
        self.assertEqual(tick.await_count, 1)

    def test_a_failing_tick_is_logged_with_the_name_and_the_loop_goes_on(self) -> None:
        calls, fake_sleep = _sleeps(4)
        tick = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        with patch.object(periodic.asyncio, "sleep", fake_sleep), \
             self.assertLogs(LOG, level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                self.run_(periodic.run_forever("demo", tick, interval_s=lambda: 0.5, logger=LOG))
        self.assertEqual(tick.await_count, 3)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].getMessage(), "demo: tick failed (non-fatal)")
        self.assertEqual(logs.records[0].name, "tests.periodic")
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertEqual(calls, [0.0, 0.5, 0.5, 0.5])

    def test_the_module_logger_is_the_default(self) -> None:
        calls, fake_sleep = _sleeps(2)
        tick = AsyncMock(side_effect=[RuntimeError("boom"), None])
        with patch.object(periodic.asyncio, "sleep", fake_sleep), \
             self.assertLogs("services.periodic", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                self.run_(periodic.run_forever("demo", tick, interval_s=lambda: 0.5))
        self.assertEqual(logs.records[0].getMessage(), "demo: tick failed (non-fatal)")

    def test_cancellation_raised_by_the_tick_propagates(self) -> None:
        tick = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            self.run_(periodic.run_forever("demo", tick, interval_s=lambda: 0.5, logger=LOG))
        tick.assert_awaited_once()


class RunForeverRecordsTests(_Loop):
    def setUp(self) -> None:
        super().setUp()
        from services import background
        self.background = background
        background.reset_for_tests()
        self.addCleanup(background.reset_for_tests)

    def test_each_tick_is_recorded_under_the_loop_name(self) -> None:
        results = iter([RuntimeError("boom"), None, RuntimeError("again")])

        async def tick():
            outcome = next(results)
            if outcome is not None:
                raise outcome

        calls, fake_sleep = _sleeps(4)
        self.background.register("demo", self.loop.create_future())
        with patch.object(periodic.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                self.run_(periodic.run_forever("demo", tick, interval_s=lambda: 1.0))
        record = self.background.snapshot()["demo"]
        self.assertEqual(record["ticks"], 3)
        self.assertEqual(record["consecutive_failures"], 1)
        self.assertEqual(record["last_failure"], "RuntimeError: again")


class ConnectionsPollerLoopTests(_Loop):
    SUMMARY = {"configured": 1, "polled": 1, "skipped": 0, "errors": 0}

    def test_loop_logs_a_failed_tick_then_the_next_summary_and_stops_on_cancel(self) -> None:
        once = AsyncMock(side_effect=[RuntimeError("tick boom")] + [self.SUMMARY] * 50)
        with patch.object(connections_poller, "_STARTUP_DELAY_S", 0), \
             patch.object(connections_poller, "tick_seconds", return_value=0.01), \
             patch.object(connections_poller, "poll_once", new=once), \
             self.assertLogs(connections_poller.logger, level="DEBUG") as logs:
            task = self.loop.create_task(connections_poller.start_connections_poller())
            self.loop.call_later(0.2, task.cancel)
            with self.assertRaises(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        self.assertGreaterEqual(once.await_count, 2, "the loop kept ticking after a failure")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages[0], "connections poller: started (0s tick)")
        self.assertIn("connections poller: tick failed (non-fatal)", messages)
        self.assertIn(f"connections poller: {self.SUMMARY}", messages)
        warn = next(r for r in logs.records if r.levelno == logging.WARNING)
        self.assertEqual(warn.name, "services.connections.poller")
        self.assertIsNotNone(warn.exc_info)

    def test_disabled_returns_at_once_with_its_own_log_line(self) -> None:
        with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": "false"}), \
             patch.object(connections_poller, "poll_once", new=AsyncMock()) as once, \
             self.assertLogs(connections_poller.logger, level="INFO") as logs:
            self.assertIsNone(self.run_(connections_poller.start_connections_poller()))
        once.assert_not_awaited()
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["connections poller: disabled by XO_CONNECTIONS_POLL_ENABLED"])


class GithubPollerLoopTests(_Loop):
    SUMMARY = {"polled": 1, "skipped": 0}

    def test_loop_logs_a_failed_tick_then_the_next_summary_and_stops_on_cancel(self) -> None:
        once = AsyncMock(side_effect=[RuntimeError("tick boom")] + [self.SUMMARY] * 50)
        with patch.object(github_poller, "_STARTUP_DELAY_S", 0), \
             patch.object(github_poller, "poll_interval_seconds", return_value=0.01), \
             patch.object(github_poller, "poll_once", new=once), \
             self.assertLogs(github_poller.logger, level="DEBUG") as logs:
            task = self.loop.create_task(github_poller.start_github_poller())
            self.loop.call_later(0.2, task.cancel)
            with self.assertRaises(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        self.assertGreaterEqual(once.await_count, 2, "the loop kept ticking after a failure")
        messages = [r.getMessage() for r in logs.records]
        self.assertRegex(messages[0], r"^github poller: started \(interval 0s, \d+ page\(s\) per repo, "
                                      r"GraphQL budget 5000/hour\)$")
        self.assertIn("github poller: tick failed (non-fatal)", messages)
        self.assertIn(f"github poller: {self.SUMMARY}", messages)
        warn = next(r for r in logs.records if r.levelno == logging.WARNING)
        self.assertEqual(warn.name, "services.cowork_agent.github_poller")
        self.assertIsNotNone(warn.exc_info)

    def test_disabled_returns_at_once_with_its_own_log_line(self) -> None:
        with patch.dict(os.environ, {"XO_GITHUB_POLL_ENABLED": "false"}), \
             patch.object(github_poller, "poll_once", new=AsyncMock()) as once, \
             self.assertLogs(github_poller.logger, level="INFO") as logs:
            self.assertIsNone(self.run_(github_poller.start_github_poller()))
        once.assert_not_awaited()
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["github poller: disabled by XO_GITHUB_POLL_ENABLED"])


if __name__ == "__main__":
    unittest.main()
