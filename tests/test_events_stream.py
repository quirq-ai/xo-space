from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from services.cowork_agent.events_stream import (
    EventsBroadcaster,
    flatten_providers,
    gather_named,
    rclone_remote_status,
)


class BuildSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_aggregates_registered_sections(self) -> None:
        async def fetch_models() -> dict:
            return {"claude_code": {"connected": True}}

        async def fetch_data() -> dict:
            return {"github": {"status": "connected"}}

        broadcaster = EventsBroadcaster(
            sections={"models": fetch_models, "data": fetch_data}
        )

        snapshot = await broadcaster.build_snapshot()

        self.assertEqual(
            snapshot,
            {
                "models": {"claude_code": {"connected": True}},
                "data": {"github": {"status": "connected"}},
            },
        )


class ErrorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_section_contributes_empty_dict_on_first_run(self) -> None:
        async def fetch_ok() -> dict:
            return {"github": {"status": "connected"}}

        async def fetch_broken() -> dict:
            raise RuntimeError("provider probe exploded")

        broadcaster = EventsBroadcaster(
            sections={"models": fetch_broken, "data": fetch_ok}
        )

        snapshot = await broadcaster.build_snapshot()

        self.assertEqual(
            snapshot,
            {"models": {}, "data": {"github": {"status": "connected"}}},
        )

    async def test_failing_section_keeps_last_known_value(self) -> None:
        calls = {"n": 0}

        async def fetch_flaky() -> dict:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("provider probe exploded")
            return {"claude_code": {"connected": True}}

        broadcaster = EventsBroadcaster(sections={"models": fetch_flaky})

        first = await broadcaster.build_snapshot()
        second = await broadcaster.build_snapshot()

        self.assertEqual(first, {"models": {"claude_code": {"connected": True}}})
        self.assertEqual(second, {"models": {"claude_code": {"connected": True}}})


class PublishOnChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_receives_push_only_when_snapshot_changes(self) -> None:
        state = {"models": {"claude_code": {"connected": False}}}

        async def fetch_models() -> dict:
            return state["models"]

        broadcaster = EventsBroadcaster(sections={"models": fetch_models})
        queue = broadcaster.subscribe()

        await broadcaster.poll_once()
        await broadcaster.poll_once()  # nothing changed — no second push

        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(
            queue.get_nowait(),
            {"models": {"claude_code": {"connected": False}}},
        )

        state["models"] = {"claude_code": {"connected": True}}
        await broadcaster.poll_once()

        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(
            queue.get_nowait(),
            {"models": {"claude_code": {"connected": True}}},
        )

    async def test_unsubscribed_queue_stops_receiving(self) -> None:
        async def fetch_models() -> dict:
            return {"claude_code": {"connected": True}}

        broadcaster = EventsBroadcaster(sections={"models": fetch_models})
        queue = broadcaster.subscribe()
        broadcaster.unsubscribe(queue)

        await broadcaster.poll_once()

        self.assertEqual(queue.qsize(), 0)


class LoopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_polls_while_subscribed_and_stops_after_last_leaves(self) -> None:
        calls = {"n": 0}

        async def fetch_models() -> dict:
            calls["n"] += 1
            return {"claude_code": {"connected": True}}

        broadcaster = EventsBroadcaster(
            sections={"models": fetch_models}, interval=0.01
        )

        queue = broadcaster.subscribe()
        await asyncio.sleep(0.06)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertTrue(broadcaster.loop_running)

        broadcaster.unsubscribe(queue)
        await asyncio.sleep(0.06)
        self.assertFalse(broadcaster.loop_running)
        settled = calls["n"]
        await asyncio.sleep(0.04)
        self.assertEqual(calls["n"], settled)


class GatherNamedTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failing_probe_does_not_affect_the_others(self) -> None:
        async def probe_ok() -> dict:
            return {"status": "connected"}

        async def probe_broken() -> dict:
            raise RuntimeError("network down")

        result = await gather_named(
            {"github": probe_broken, "vercel": probe_ok},
            fallback={"status": "error"},
        )

        self.assertEqual(
            result,
            {"github": {"status": "error"}, "vercel": {"status": "connected"}},
        )


class SectionMappingTests(unittest.TestCase):
    def test_flatten_providers_merges_oauth_and_api_keys(self) -> None:
        payload = {
            "agent": "claude_code",
            "oauth": {
                "claude_code": {"connected": True},
                "codex": {"connected": False},
            },
            "api_keys": {"anthropic": {"connected": True}},
        }

        self.assertEqual(
            flatten_providers(payload),
            {
                "claude_code": {"connected": True},
                "codex": {"connected": False},
                "anthropic": {"connected": True},
            },
        )

    def test_rclone_remote_status_maps_all_three_states(self) -> None:
        self.assertEqual(
            rclone_remote_status(True, [{"name": "gdrive"}]),
            {"status": "connected"},
        )
        self.assertEqual(rclone_remote_status(True, []), {"status": "needs_auth"})
        self.assertEqual(rclone_remote_status(False, []), {"status": "unavailable"})


class EventsRouteTests(unittest.IsolatedAsyncioTestCase):
    """Drives the SSE generator directly: Starlette's TestClient buffers the
    entire response body, so an endless stream can never be consumed through
    it (the request would hang forever)."""

    async def test_events_endpoint_streams_initial_snapshot(self) -> None:
        from routers.cowork_agent import events as events_module

        async def fetch_models() -> dict:
            return {"claude_code": {"connected": True}}

        fake = EventsBroadcaster(sections={"models": fetch_models})

        with mock.patch.object(events_module, "broadcaster", fake):
            response = await events_module.events()
            self.assertEqual(response.media_type, "text/event-stream")

            stream = response.body_iterator
            first = await asyncio.wait_for(stream.__anext__(), timeout=5)
            await stream.aclose()

        self.assertIn("event: snapshot", first)
        self.assertIn('"claude_code": {"connected": true}', first)
        # Closing the stream must unsubscribe the client and stop the loop.
        self.assertFalse(fake.loop_running)

    def test_route_is_registered_at_api_events(self) -> None:
        from routers.cowork_agent import all_routers, events as events_module

        self.assertEqual(
            [route.path for route in events_module.router.routes],
            ["/api/events"],
        )
        self.assertIn(events_module.router, all_routers)


if __name__ == "__main__":
    unittest.main()
