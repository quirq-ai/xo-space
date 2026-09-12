"""The connections poller: due checks, the per-toolkit lock, dedupe, error
isolation, the identity and scope failure paths, and the tick summary.

Hermetic: QUIRQ_STATE_ROOT points into a temp dir, every collaborator with
a network or cache behind it is patched by attribute on the poller module
(identity, workspace scope, the MCP entry builder, the MCP client), one
event loop per test, and the lock dict is reset in setUp. A guard asserts
``store.connections_dir()`` resolves under the temp root once the env is
patched, so nothing here can reach the real ``~/.quirq``."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.connections import collectors, mcp_client, poller, store
from services.cowork_agent.connections import service as connections_service
from services.cowork_agent.connections.mcp_client import McpError
from services.inbox import service as inbox_service

ENTRY = {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer t"}}


def envelope(messages: list[dict], *, successful: bool = True, error=None) -> dict:
    payload = {"successful": successful, "data": {"messages": messages}, "error": error}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def message(i: int) -> dict:
    """Message ``i`` arrived at ``i`` o'clock, so a higher number is newer."""
    return {"messageId": f"m{i}", "subject": f"Subject {i}", "snippet": f"snippet {i}",
            "messageTimestamp": f"2026-09-11T{i:02d}:00:00Z"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root / ".quirq"),
                                            "XO_PROJECTS_ROOT": str(self.root / "projects")})
        self._env.start()
        self.assertEqual(store.connections_dir(), self.root / ".quirq" / "connections",
                         "the store must resolve under the temp root, never the real ~/.quirq")
        poller.reset_for_tests()
        self.loop = asyncio.new_event_loop()
        self.known = patch.object(poller.state, "account_id_if_known", return_value="user_x")
        self.aaccount = patch.object(poller.state, "aaccount_id", new=AsyncMock(return_value="user_x"))
        self.scope = patch.object(poller.space_scope, "enabled_toolkits",
                                  return_value=["gmail", "googlecalendar", "notion"])
        self.entry = patch.object(poller.composio_service, "build_mcp_server_entry", return_value=ENTRY)
        self.call = patch.object(poller.mcp_client, "call_tool",
                                 new=AsyncMock(return_value=envelope([message(1), message(2)])))
        # The session lists the collector slugs directly, so the default path is a
        # plain tools/call; the routing tests swap this for the executor-only list.
        self.names = patch.object(poller.mcp_client, "list_tools", new=AsyncMock(
            return_value=["GMAIL_FETCH_EMAILS", "GOOGLECALENDAR_EVENTS_LIST", "NOTION_SEARCH_NOTION_PAGE"]))
        self.mocks = {}
        for name, p in (("known", self.known), ("aaccount", self.aaccount), ("scope", self.scope),
                        ("entry", self.entry), ("call", self.call), ("names", self.names)):
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        self.loop.close()
        poller.reset_for_tests()
        inbox_service._reset_throttle()
        self._env.stop()
        self._tmp.cleanup()

    def run_(self, coro):
        return self.loop.run_until_complete(coro)

    def folder(self, toolkit: str = "gmail") -> Path:
        return self.root / ".quirq" / "connections" / toolkit

    def events(self, toolkit: str = "gmail") -> list[dict]:
        return store.read_events(toolkit, limit=100)


class SkipPathsTests(_Base):
    def test_not_configured_creates_nothing(self) -> None:
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out, {"toolkit": "gmail", "polled": False, "new_events": 0, "error": None,
                               "skipped": "not_configured"})
        self.assertFalse(self.folder().exists())
        self.assertFalse((self.root / ".quirq" / "connections").exists())
        self.mocks["call"].assert_not_awaited()
        self.mocks["known"].assert_not_called()

    def test_disabled_unless_forced(self) -> None:
        store.write_config("gmail", enabled=False)
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["skipped"], "disabled")
        self.assertIsNone(store.read_state("gmail")["last_poll_at"], "a skip never stamps the state")
        out = self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual((out["polled"], out["new_events"], out["skipped"]), (True, 2, None))

    def test_not_due_unless_forced(self) -> None:
        store.write_config("gmail", interval_s=3600)
        store.update_state("gmail", last_poll_at=collectors.iso(datetime.now(timezone.utc)))
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["skipped"], "not_due")
        self.mocks["call"].assert_not_awaited()
        self.assertTrue(self.run_(poller.poll_connection("gmail", force=True))["polled"])

    def test_due_when_the_interval_elapsed_or_the_stamp_is_odd(self) -> None:
        store.write_config("gmail", interval_s=60)
        for stamp in (collectors.iso(datetime.now(timezone.utc) - timedelta(seconds=61)),
                      collectors.iso(datetime.now(timezone.utc) + timedelta(days=1)), "junk", None):
            with self.subTest(last_poll_at=stamp):
                store.update_state("gmail", last_poll_at=stamp, cursors={})
                self.assertTrue(self.run_(poller.poll_connection("gmail"))["polled"])

    def test_busy_when_the_lock_is_held(self) -> None:
        store.write_config("gmail")

        async def scenario():
            async with poller._lock("gmail"):
                return await poller.poll_connection("gmail", force=True)

        out = self.run_(scenario())
        self.assertEqual(out["skipped"], "busy")
        self.mocks["call"].assert_not_awaited()
        self.assertFalse(poller._lock("gmail").locked())
        self.assertTrue(self.run_(poller.poll_connection("gmail", force=True))["polled"])

    def test_corrupt_config_is_skipped(self) -> None:
        self.folder().mkdir(parents=True)
        (self.folder() / "config.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["skipped"], "not_configured")
        self.assertFalse((self.folder() / "state.json").exists())
        self.assertFalse((self.folder() / "events.jsonl").exists())


class SuccessfulPollTests(_Base):
    def test_events_state_and_summary(self) -> None:
        store.write_config("gmail")
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out, {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None})
        self.mocks["call"].assert_awaited_once()
        entry, tool, args = self.mocks["call"].await_args.args
        self.assertEqual((entry, tool, args), (ENTRY, "GMAIL_FETCH_EMAILS", {"query": "is:unread", "max_results": 20}))
        self.assertEqual(self.mocks["call"].await_args.kwargs, {"timeout_s": poller.CALL_TIMEOUT_S})
        self.mocks["entry"].assert_called_once_with("user_x")
        events = self.events()
        self.assertEqual([e["key"] for e in events], ["m2", "m1"], "newest-first")
        self.assertEqual(events[0]["toolkit"], "gmail")
        self.assertEqual(events[0]["type"], "unread")
        state_doc = store.read_state("gmail")
        self.assertEqual(state_doc["last_poll_at"], state_doc["last_ok_at"])
        self.assertIsNotNone(state_doc["last_poll_at"])
        self.assertIsNone(state_doc["last_error"])
        self.assertEqual(state_doc["events_total"], 2)
        self.assertEqual(sorted(state_doc["cursors"]["unread"]["seen"]), ["m1", "m2"])
        self.assertEqual(list(state_doc["cursors"]), ["unread"])

    def test_seen_keys_and_in_batch_duplicates_are_dropped(self) -> None:
        store.write_config("gmail")
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["new_events"], 2)
        self.mocks["call"].return_value = envelope([message(2), message(3), message(3), message(1)])
        out = self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(out["new_events"], 1)
        self.assertEqual([e["key"] for e in self.events()], ["m3", "m2", "m1"])
        state_doc = store.read_state("gmail")
        self.assertEqual((state_doc["events_total"], state_doc["cursors"]["unread"]["seen"][-1]), (3, "m3"))
        out = self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(out["new_events"], 0)
        self.assertEqual(store.read_state("gmail")["events_total"], 3)

    def test_cursors_are_per_collector(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["new_events"], 4)
        self.assertEqual(sorted(e["type"] for e in self.events()), ["inbox", "inbox", "unread", "unread"])
        self.assertEqual(self.mocks["call"].await_count, 2)

    def test_cached_identity_wins_and_a_fetch_is_the_fallback(self) -> None:
        store.write_config("gmail")
        self.assertTrue(self.run_(poller.poll_connection("gmail"))["polled"])
        self.mocks["aaccount"].assert_not_awaited()
        self.mocks["known"].return_value = None
        self.assertTrue(self.run_(poller.poll_connection("gmail", force=True))["polled"])
        self.mocks["aaccount"].assert_awaited_once()
        self.mocks["entry"].assert_called_with("user_x")


class CollectorFailureTests(_Base):
    def test_one_failing_collector_does_not_stop_the_others(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["call"].side_effect = [McpError("HTTP 500 boom"), envelope([message(7)])]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, "unread: HTTP 500 boom"))
        self.assertEqual([e["key"] for e in self.events()], ["m7"])
        state_doc = store.read_state("gmail")
        self.assertEqual(state_doc["last_error"], "unread: HTTP 500 boom")
        self.assertIsNotNone(state_doc["last_poll_at"])
        self.assertIsNone(state_doc["last_ok_at"], "last_ok_at moves only when nothing failed")
        self.assertEqual(state_doc["events_total"], 1)
        self.assertEqual(sorted(state_doc["cursors"]), ["inbox"])

    def test_composio_envelope_with_successful_false_is_an_error(self) -> None:
        store.write_config("gmail")
        self.mocks["call"].return_value = envelope([message(1)], successful=False, error="scope missing")
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["error"], "unread: scope missing")
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertEqual(store.read_state("gmail")["events_total"], 0)

    def test_mapping_exception_is_isolated_and_truncated(self) -> None:
        store.write_config("gmail")
        self.mocks["call"].side_effect = RuntimeError("x" * 900)
        out = self.run_(poller.poll_connection("gmail"))
        self.assertTrue(out["error"].startswith("unread: xxx"))
        self.assertLessEqual(len(out["error"]), 300)
        self.assertLessEqual(len(store.read_state("gmail")["last_error"]), 300)

    def test_timeout_is_recorded(self) -> None:
        store.write_config("gmail")

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        with patch.object(poller, "CALL_TIMEOUT_S", 0.01), patch.object(poller, "_GRACE_S", 0.01), \
             patch.object(poller.mcp_client, "call_tool", new=slow):
            out = self.run_(poller.poll_connection("gmail"))
        self.assertTrue(out["polled"])
        self.assertIn("unread: timed out", out["error"])

    def test_errors_are_joined_and_a_later_success_clears_them(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["call"].side_effect = [McpError("a"), McpError("b")]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["error"], "unread: a; inbox: b")
        self.mocks["call"].side_effect = None
        out = self.run_(poller.poll_connection("gmail", force=True))
        self.assertIsNone(out["error"])
        state_doc = store.read_state("gmail")
        self.assertIsNone(state_doc["last_error"])
        self.assertEqual(state_doc["last_ok_at"], state_doc["last_poll_at"])


class IdentityAndScopeTests(_Base):
    def assert_failed(self, out: dict, message: str) -> None:
        self.assertEqual(out, {"toolkit": "gmail", "polled": False, "new_events": 0, "error": message, "skipped": None})
        state_doc = store.read_state("gmail")
        self.assertEqual(state_doc["last_error"], message)
        self.assertIsNotNone(state_doc["last_poll_at"], "a failed attempt still stamps last_poll_at")
        self.assertIsNone(state_doc["last_ok_at"])
        self.assertEqual(state_doc["events_total"], 0)
        self.assertFalse((self.folder() / "events.jsonl").exists(), "no events on a failed attempt")
        self.mocks["call"].assert_not_awaited()

    def test_not_signed_in(self) -> None:
        store.write_config("gmail")
        self.mocks["known"].return_value = None
        self.mocks["aaccount"].side_effect = RuntimeError("no token")
        self.assert_failed(self.run_(poller.poll_connection("gmail")), poller.NOT_SIGNED_IN)
        self.mocks["entry"].assert_not_called()
        self.mocks["aaccount"].side_effect = None
        self.mocks["aaccount"].return_value = ""
        self.assert_failed(self.run_(poller.poll_connection("gmail", force=True)), poller.NOT_SIGNED_IN)
        self.assert_failed(self.run_(poller.poll_connection("gmail", force=True, user_id=None)), poller.NOT_SIGNED_IN)

    def test_toolkit_not_turned_on_here(self) -> None:
        store.write_config("gmail")
        self.mocks["scope"].return_value = ["notion"]
        self.assert_failed(self.run_(poller.poll_connection("gmail")), "gmail is not turned on in this workspace")
        self.mocks["entry"].assert_not_called()
        self.mocks["scope"].side_effect = OSError("unreadable")
        self.assert_failed(self.run_(poller.poll_connection("gmail", force=True)), "gmail is not turned on in this workspace")

    def test_no_toolkits_enabled_and_session_failures(self) -> None:
        store.write_config("gmail")
        self.mocks["entry"].side_effect = poller.composio_service.NoToolkitsEnabled("none")
        self.assert_failed(self.run_(poller.poll_connection("gmail")), poller.NO_TOOLKITS)
        self.mocks["entry"].side_effect = RuntimeError("composio: session for user=user_x exposed no MCP url. " + "y" * 400)
        out = self.run_(poller.poll_connection("gmail", force=True))
        self.assertTrue(out["error"].startswith("session unavailable: composio: session"))
        self.assertLessEqual(len(out["error"]), 300)
        self.assertEqual(store.read_state("gmail")["last_error"], out["error"])
        self.mocks["call"].assert_not_awaited()

    def test_a_failed_attempt_is_not_retried_before_its_interval(self) -> None:
        store.write_config("gmail", interval_s=3600)
        self.mocks["scope"].return_value = []
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["error"], "gmail is not turned on in this workspace")
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["skipped"], "not_due")


class TickTests(_Base):
    def test_poll_once_summary_and_single_identity_resolution(self) -> None:
        store.write_config("gmail")
        store.write_config("notion", enabled=False)
        store.write_config("googlecalendar")
        (self.root / ".quirq" / "connections" / "figma").mkdir()          # no config.json: never polled
        self.mocks["call"].return_value = envelope([message(1)])
        summary = self.run_(poller.poll_once())
        self.assertEqual(summary, {"configured": 3, "polled": 2, "skipped": 1, "errors": 0})
        self.assertEqual(self.mocks["known"].call_count, 1, "identity resolved once per tick")
        self.assertEqual(self.mocks["entry"].call_count, 2)
        self.assertFalse((self.root / ".quirq" / "connections" / "figma" / "state.json").exists())
        self.assertEqual(self.run_(poller.poll_once()), {"configured": 3, "polled": 0, "skipped": 3, "errors": 0})
        self.assertEqual(self.mocks["known"].call_count, 1, "nothing due: no identity round trip")

    def test_poll_once_counts_errors(self) -> None:
        store.write_config("gmail")
        store.write_config("notion")
        self.mocks["scope"].return_value = ["notion"]
        self.mocks["call"].side_effect = McpError("nope")
        summary = self.run_(poller.poll_once())
        self.assertEqual(summary, {"configured": 2, "polled": 1, "skipped": 0, "errors": 2})
        self.assertEqual(store.read_state("gmail")["last_error"], "gmail is not turned on in this workspace")
        self.assertEqual(store.read_state("notion")["last_error"], "recent_pages: nope")

    def test_poll_once_survives_listing_and_connection_failures(self) -> None:
        with patch.object(poller.store, "list_configured", side_effect=OSError("boom")):
            self.assertEqual(self.run_(poller.poll_once()), {"configured": 0, "polled": 0, "skipped": 0, "errors": 0})
        store.write_config("gmail")
        with patch.object(poller, "poll_connection", new=AsyncMock(side_effect=RuntimeError("unexpected"))), \
             self.assertLogs(poller.logger, level="WARNING"):
            self.assertEqual(self.run_(poller.poll_once()), {"configured": 1, "polled": 0, "skipped": 0, "errors": 1})

    def test_poll_once_with_no_identity_records_the_error_per_connection(self) -> None:
        store.write_config("gmail")
        self.mocks["known"].return_value = None
        self.mocks["aaccount"].side_effect = RuntimeError("no token")
        self.assertEqual(self.run_(poller.poll_once()), {"configured": 1, "polled": 0, "skipped": 0, "errors": 1})
        self.assertEqual(store.read_state("gmail")["last_error"], poller.NOT_SIGNED_IN)
        self.assertEqual(self.mocks["aaccount"].await_count, 1)

    def test_cancellation_propagates_and_releases_the_lock(self) -> None:
        store.write_config("gmail")

        async def slow(*args, **kwargs):
            await asyncio.sleep(30)

        with patch.object(poller.mcp_client, "call_tool", new=slow):
            task = self.loop.create_task(poller.poll_connection("gmail"))
            self.loop.call_later(0.05, task.cancel)
            with self.assertRaises(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        self.assertFalse(poller._lock("gmail").locked())
        self.assertFalse((self.folder() / "events.jsonl").exists())

    def test_loop_survives_a_failing_tick_and_stops_on_cancel(self) -> None:
        summary = {"configured": 0, "polled": 0, "skipped": 0, "errors": 0}
        once = AsyncMock(side_effect=[RuntimeError("tick boom"), summary, summary, summary, summary, summary])
        with patch.object(poller, "_STARTUP_DELAY_S", 0), patch.object(poller, "tick_seconds", return_value=0.01), \
             patch.object(poller, "poll_once", new=once):
            task = self.loop.create_task(poller.start_connections_poller())
            self.loop.call_later(0.2, task.cancel)
            with self.assertRaises(asyncio.CancelledError):
                self.loop.run_until_complete(task)
        self.assertGreaterEqual(once.await_count, 2, "the loop kept ticking after a failure")

    def test_loop_returns_at_once_when_disabled(self) -> None:
        with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": "false"}), \
             patch.object(poller, "poll_once", new=AsyncMock()) as once:
            self.assertIsNone(self.run_(poller.start_connections_poller()))
        once.assert_not_awaited()


class EnvTests(unittest.TestCase):
    def test_enabled_flag(self) -> None:
        with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": ""}):
            self.assertTrue(poller.poller_enabled())
        for raw in ("false", "0", "off", "no", "FALSE"):
            with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": raw}):
                self.assertFalse(poller.poller_enabled(), raw)
        for raw in ("true", "1", "on", "yes", "TRUE"):
            with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_ENABLED": raw}):
                self.assertTrue(poller.poller_enabled(), raw)

    def test_tick_seconds(self) -> None:
        for raw, expected in (("", 30.0), ("45", 45.0), ("1", 5.0), ("abc", 30.0), ("0.5", 5.0)):
            with patch.dict(os.environ, {"XO_CONNECTIONS_POLL_TICK_S": raw}):
                self.assertEqual(poller.tick_seconds(), expected, raw)

    def test_module_names_no_agent_and_uses_no_dashes(self) -> None:
        import re
        root = Path(__file__).resolve().parents[1] / "services" / "cowork_agent" / "connections"
        for name in ("__init__", "store", "collectors", "mcp_client", "poller", "service"):
            src = (root / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotRegex(src, r"openclaw|hermes|claude_code|codex|antigravity")
                self.assertNotIn("services.cowork_agent.adapters", src)
                self.assertIsNone(re.search("[\\u2013\\u2014]", src))


class ServicePollNowTests(_Base):
    """``service.poll_now`` nudges the inbox once a poll collected something:
    the Inbox tab reloads its rows right after the POST, and that read
    throttles its own ingest, so without the nudge the fresh events would
    wait for the next tick."""

    OUTCOME = {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None}

    def test_new_events_trigger_a_forced_inbox_refresh(self) -> None:
        with patch.object(poller, "poll_connection", new=AsyncMock(return_value=self.OUTCOME)) as pc, \
             patch.object(inbox_service, "refresh", return_value=True) as rf:
            self.assertEqual(self.run_(connections_service.poll_now("gmail")), self.OUTCOME)
        pc.assert_awaited_once_with("gmail", force=True)
        rf.assert_called_once_with(force=True)

    def test_nothing_new_or_a_skip_leaves_the_inbox_alone(self) -> None:
        for outcome in ({**self.OUTCOME, "new_events": 0},
                        {**self.OUTCOME, "polled": False, "new_events": 0, "skipped": "busy"},
                        {**self.OUTCOME, "new_events": 0, "error": "unread: boom"}):
            with self.subTest(outcome=outcome), \
                 patch.object(poller, "poll_connection", new=AsyncMock(return_value=outcome)), \
                 patch.object(inbox_service, "refresh") as rf:
                self.assertEqual(self.run_(connections_service.poll_now("gmail")), outcome)
                rf.assert_not_called()

    def test_a_failing_inbox_refresh_is_logged_and_the_outcome_still_returned(self) -> None:
        with patch.object(poller, "poll_connection", new=AsyncMock(return_value=self.OUTCOME)), \
             patch.object(inbox_service, "refresh", side_effect=OSError("disk")), \
             self.assertLogs(connections_service.logger, level="WARNING"):
            self.assertEqual(self.run_(connections_service.poll_now("gmail")), self.OUTCOME)

    def test_the_inbox_holds_what_poll_now_collected_despite_its_throttle(self) -> None:
        (self.root / "projects").mkdir(exist_ok=True)
        store.write_config("gmail")
        fresh = collectors.iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.mocks["call"].return_value = envelope([{**message(9), "messageTimestamp": fresh}])
        inbox_service._reset_throttle()
        self.assertFalse(inbox_service.refresh(force=True), "nothing to ingest yet")
        self.assertFalse(inbox_service.refresh(), "the throttle window is now armed")
        out = self.run_(connections_service.poll_now("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, None))
        inbox_path = self.root / ".quirq" / "inbox.json"
        self.assertTrue(inbox_path.is_file(), "the forced refresh wrote the inbox inside the throttle window")
        keys = {it["key"] for it in json.loads(inbox_path.read_text(encoding="utf-8"))["items"]}
        self.assertIn("connection:gmail:unread:m9", keys)


if __name__ == "__main__":
    unittest.main()


def router_result(*, results: list[dict], is_error: bool = False) -> dict:
    """What COMPOSIO_MULTI_EXECUTE_TOOL answers: one result per requested tool."""
    payload = {"data": {"results": results, "total_count": len(results),
                        "success_count": sum(1 for r in results if "error" not in r),
                        "error_count": sum(1 for r in results if "error" in r)},
               "successful": not is_error, "error": None}
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}


class SessionRoutingTests(_Base):
    """A Composio tool-router session lists meta tools only, so a collector's
    slug runs through the executor; a dead session is replaced once."""

    def test_slug_not_listed_runs_through_the_executor(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].return_value = [mcp_client.EXECUTOR_TOOL, "COMPOSIO_SEARCH_TOOLS"]
        self.mocks["call"].return_value = router_result(results=[
            {"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
             "response": {"successful": True, "data": {"messages": [message(1)]}}}])
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, None))
        self.assertEqual([e["key"] for e in self.events()], ["m1"])
        called = self.mocks["call"].call_args
        self.assertEqual(called.args[1], mcp_client.EXECUTOR_TOOL)
        self.assertEqual(called.args[2]["tools"][0]["tool_slug"], "GMAIL_FETCH_EMAILS")
        self.assertEqual(called.args[2]["tools"][0]["arguments"]["query"], "is:unread")
        self.assertIs(called.args[2]["sync_response_to_workbench"], False)
        self.assertIs(called.kwargs.get("raise_on_tool_error"), False)

    def test_executor_per_tool_error_is_recorded_for_that_collector(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["names"].return_value = [mcp_client.EXECUTOR_TOOL]
        self.mocks["call"].side_effect = [
            router_result(results=[{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                                    "error": "[Session Restriction] Toolkit 'gmail' is not allowed"}], is_error=True),
            router_result(results=[{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                                    "response": {"successful": True, "data": {"messages": [message(3)]}}}]),
        ]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["new_events"], 1)
        self.assertTrue(out["error"].startswith("unread: [Session Restriction]"), out["error"])
        self.assertEqual([e["key"] for e in self.events()], ["m3"])

    def test_tools_list_failure_is_a_poll_failure_without_events(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].side_effect = McpError("tools/list failed: HTTP 500 upstream")
        out = self.run_(poller.poll_connection("gmail"))
        self.assertTrue(out["error"].startswith("tools/list failed"), out["error"])
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertIsNone(store.read_state("gmail")["last_ok_at"])
        self.mocks["call"].assert_not_called()

    def test_dead_session_is_invalidated_and_retried_once(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].side_effect = [
            McpError('initialize failed: HTTP 404 {"error":{"message":"Tool router session with ID trs_x not found"}}'),
            ["GMAIL_FETCH_EMAILS"],
        ]
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertEqual(self.mocks["entry"].call_count, 2, "a fresh entry is built after the invalidation")
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 2, None))

    def test_dead_session_twice_is_a_recorded_failure(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].side_effect = McpError("initialize failed: HTTP 404 gone")
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertIn("initialize failed: HTTP 404", out["error"])
        self.mocks["call"].assert_not_called()

    def test_forced_poll_waits_for_a_busy_lock(self) -> None:
        store.write_config("gmail")

        async def scenario():
            lock = poller._lock("gmail")
            await lock.acquire()
            task = asyncio.ensure_future(poller.poll_connection("gmail", force=True))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "the forced poll waits instead of bouncing")
            lock.release()
            return await task

        out = self.run_(scenario())
        self.assertTrue(out["polled"])
        self.assertFalse(poller._lock("gmail").locked())

    def test_unforced_poll_bounces_on_a_busy_lock(self) -> None:
        store.write_config("gmail")

        async def scenario():
            lock = poller._lock("gmail")
            await lock.acquire()
            try:
                return await poller.poll_connection("gmail")
            finally:
                lock.release()

        self.assertEqual(self.run_(scenario())["skipped"], "busy")

    def test_forced_poll_gives_up_after_the_wait(self) -> None:
        store.write_config("gmail")

        async def scenario():
            lock = poller._lock("gmail")
            await lock.acquire()
            try:
                with patch.object(poller, "FORCE_WAIT_S", 0.01):
                    return await poller.poll_connection("gmail", force=True)
            finally:
                lock.release()

        self.assertEqual(self.run_(scenario())["skipped"], "busy")
