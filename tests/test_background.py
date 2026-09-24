"""services/background.py: records, never raises, never leaks secrets."""

from __future__ import annotations

import asyncio
import unittest

from services import background


class _Loop(unittest.TestCase):
    def setUp(self) -> None:
        background.reset_for_tests()
        self.loop = asyncio.new_event_loop()

    def tearDown(self) -> None:
        self.loop.close()
        background.reset_for_tests()

    def run_(self, coro):
        return self.loop.run_until_complete(coro)


class RecordTests(_Loop):
    def test_a_crashed_task_is_recorded_with_its_error(self) -> None:
        async def boom():
            raise RuntimeError("watcher crashed")

        async def main():
            task = asyncio.ensure_future(boom())
            background.register("watcher", task)
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)  # let the done-callback run

        self.run_(main())
        record = background.snapshot()["watcher"]
        self.assertEqual(record["state"], "crashed")
        self.assertEqual(record["error"], "RuntimeError: watcher crashed")
        self.assertIsNotNone(record["ended_at"])

    def test_a_task_that_returns_and_one_that_is_cancelled(self) -> None:
        async def quick():
            return None

        async def forever():
            await asyncio.sleep(3600)

        async def main():
            done = asyncio.ensure_future(quick())
            stuck = asyncio.ensure_future(forever())
            background.register("gateway reconcile", done, finishes_by_design=True)
            background.register("relay poller", stuck)
            await done
            stuck.cancel()
            await asyncio.gather(stuck, return_exceptions=True)
            await asyncio.sleep(0)

        self.run_(main())
        snap = background.snapshot()
        self.assertEqual(snap["gateway reconcile"]["state"], "returned")
        self.assertTrue(snap["gateway reconcile"]["finishes_by_design"])
        self.assertEqual(snap["relay poller"]["state"], "cancelled")

    def test_ticks_count_failures_in_a_row_and_reset_on_success(self) -> None:
        async def main():
            task = asyncio.ensure_future(asyncio.sleep(3600))
            background.register("github poller", task)
            for outcome in ("fail", "fail", "ok", "fail"):
                background.tick_started("github poller")
                if outcome == "ok":
                    background.tick_succeeded("github poller")
                else:
                    background.tick_failed("github poller", ValueError("bad response"))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.run_(main())
        record = background.snapshot()["github poller"]
        self.assertEqual(record["ticks"], 4)
        self.assertEqual(record["consecutive_failures"], 1)
        self.assertEqual(record["last_failure"], "ValueError: bad response")
        self.assertIsNotNone(record["last_tick_ok_at"])

    def test_ticks_for_an_unregistered_name_are_ignored(self) -> None:
        background.tick_started("nobody")
        background.tick_failed("nobody", "x")
        self.assertEqual(background.snapshot(), {})

    def test_nothing_raises_even_on_nonsense(self) -> None:
        background.register("x", object())  # no add_done_callback
        background.tick_failed("x", object())  # not an exception
        self.assertIn("x", background.snapshot())


class SnapshotCopyTest(_Loop):
    def test_the_snapshot_is_a_copy(self) -> None:
        async def main():
            task = asyncio.ensure_future(asyncio.sleep(3600))
            background.register("watcher", task)
            await asyncio.sleep(0)  # let registration complete
            return task

        task = self.run_(main())
        background.snapshot()["watcher"]["state"] = "tampered"
        self.assertEqual(background.snapshot()["watcher"]["state"], "running")
        task.cancel()
        self.loop.run_until_complete(asyncio.gather(task, return_exceptions=True))


class RedactTests(unittest.TestCase):
    def test_redact_removes_urls_and_token_like_runs(self) -> None:
        text = background.redact(
            "GET https://api.example.com/x?coder_session_token=abc failed; key sk_" "live_0123456789abcdefghij0123456789ab")
        self.assertNotIn("https://", text)
        self.assertNotIn("coder_session_token", text)
        self.assertNotIn("0123456789abcdefghij", text)
        self.assertIn("<url>", text)
        self.assertIn("<redacted>", text)

    def test_redact_caps_the_length(self) -> None:
        self.assertEqual(len(background.redact("word " * 200)), background.ERROR_MAX)

    def test_describe_uses_the_type_when_the_message_is_empty(self) -> None:
        self.assertEqual(background.describe(TimeoutError()), "TimeoutError")


if __name__ == "__main__":
    unittest.main()
