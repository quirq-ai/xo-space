"""The connections poller: due checks, the per-toolkit lock, dedupe, error
isolation, the identity and scope failure paths, and the tick summary.

Hermetic: QUIRQ_STATE_ROOT points into a temp dir, every collaborator with
a network or cache behind it is patched by attribute on the poller module
(identity, workspace scope and its pins, the MCP entry builder, and the
MCP session class, replaced by :class:`FakeSession`;
:class:`RealSessionTests` keeps the real one over an
``httpx.MockTransport``), one event loop per test, and the lock dict is
reset in setUp. A guard asserts ``store.connections_dir()`` resolves under
the temp root once the env is patched, so nothing here can reach the real
``~/.quirq``.

A poll resolves the connected account before its collectors when the
cache is cold, so the default session lists the two identity tools beside
the collector slugs and the default answer carries ``emailAddress`` (one
blob serves the identity call and the collectors): the first poll of a
gmail or calendar connection makes one ``tools/call`` more than its
collectors, and a scripted ``side_effect`` list starts with the identity
answer (:func:`profile`)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from services import signals
from modules.connections import collectors, mcp_client, poller, store
from modules.connections import service as connections_service
from modules.connections.mcp_client import McpError
from services.inbox import service as inbox_service

ENTRY = {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer t"}}
EMAIL = "ana@example.com"
#: What the default FakeSession lists: the collector slugs and the two identity tools.
DEFAULT_TOOLS = ["GMAIL_FETCH_EMAILS", "GMAIL_GET_PROFILE", "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
                 "GOOGLECALENDAR_GET_CALENDAR", "NOTION_SEARCH_NOTION_PAGE"]


def envelope(messages: list[dict], *, successful: bool = True, error=None, email: str | None = None) -> dict:
    """A Gmail fetch answer; with ``email`` the data also carries the profile's
    ``emailAddress``, so one answer serves the identity call too."""
    data = {"messages": messages}
    if email:
        data["emailAddress"] = email
    payload = {"successful": successful, "data": data, "error": error}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def profile(email: str = EMAIL) -> dict:
    """What GMAIL_GET_PROFILE answers (the live shape)."""
    payload = {"successful": True, "error": None,
               "data": {"emailAddress": email, "historyId": "12345", "messagesTotal": 10, "threadsTotal": 8,
                        "display_url": "https://mail.google.com/"}}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def calendar_profile(email: str = EMAIL) -> dict:
    """What GOOGLECALENDAR_GET_CALENDAR answers for ``primary`` (the live shape)."""
    payload = {"successful": True, "error": None,
               "data": {"calendar_data": {"id": email, "summary": email, "timeZone": "Europe/Lisbon"},
                        "display_url": "https://calendar.google.com/"}}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def message(i: int) -> dict:
    """Message ``i`` arrived at ``i`` o'clock, so a higher number is newer."""
    return {"messageId": f"m{i}", "subject": f"Subject {i}", "snippet": f"snippet {i}",
            "messageTimestamp": f"2026-09-11T{i:02d}:00:00Z"}


def dead_session() -> McpError:
    """What ``McpSession.open`` raises once Composio has dropped the session."""
    return McpError('initialize failed: HTTP 404 {"error":{"message":"Tool router session with ID trs_x not found"}}',
                    stage="initialize", status=404)


class FakeSession(mcp_client.McpSession):
    """The real session's routing (``execute_tool``) over mocked ``open``,
    ``list_tools`` and ``call_tool``: no client, no socket. The mocks are
    class attributes (reset in ``_Base.setUp``) reachable as
    ``self.mocks["open"]``, ``self.mocks["names"]`` and ``self.mocks["call"]``;
    ``opened`` collects every session whose handshake succeeded and
    ``closed`` counts the closes."""

    open_mock: AsyncMock
    list_mock: AsyncMock
    call_mock: AsyncMock
    opened: list = []
    closed = 0

    @classmethod
    def reset(cls, names: list, call_result: dict) -> None:
        cls.open_mock = AsyncMock(return_value=None)
        cls.list_mock = AsyncMock(return_value=names)
        cls.call_mock = AsyncMock(return_value=call_result)
        cls.opened, cls.closed = [], 0

    async def open(self):
        await self.open_mock(self.url)
        type(self).opened.append(self)
        return self

    async def close(self) -> None:
        type(self).closed += 1

    async def list_tools(self):
        return await self.list_mock()

    async def call_tool(self, name, arguments, *, raise_on_tool_error=True):
        return await self.call_mock(name, arguments, raise_on_tool_error=raise_on_tool_error)


class _Base(unittest.TestCase):
    #: RealSessionTests keeps mcp_client.McpSession and scripts the upstream instead.
    REAL_SESSION = False

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
        self.configured = patch.object(poller.byo_key, "configured", return_value=True)
        self.userid = patch.object(poller.byo_key, "user_id", return_value="user_x")
        self.scope = patch.object(poller.space_scope, "enabled_toolkits",
                                  return_value=["gmail", "googlecalendar", "notion"])
        # the pins: space_scope.load() reads a path fixed at import time, so it is
        # replaced here (nothing pinned by default; a test sets connected_account_ids)
        self.pins = patch.object(poller.space_scope, "load", return_value={})
        self.entry = patch.object(poller.composio_service, "build_mcp_server_entry", return_value=ENTRY)
        # The session lists the collector and identity slugs directly, so the
        # default path is a plain tools/call; the routing tests swap this for the
        # executor-only list. The one default answer carries emailAddress, so the
        # identity call a cold cache adds to the first poll succeeds on it too.
        FakeSession.reset(list(DEFAULT_TOOLS), envelope([message(1), message(2)], email=EMAIL))
        self.mocks = {"open": FakeSession.open_mock, "names": FakeSession.list_mock, "call": FakeSession.call_mock}
        for name, p in (("configured", self.configured), ("userid", self.userid), ("scope", self.scope),
                        ("pins", self.pins), ("entry", self.entry)):
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        if not self.REAL_SESSION:
            self.session = patch.object(poller.mcp_client, "McpSession", FakeSession)
            self.session.start()
            self.addCleanup(self.session.stop)

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

    def accounts(self) -> dict:
        path = self.root / ".quirq" / "connections" / "accounts.json"
        return json.loads(path.read_text(encoding="utf-8"))["accounts"] if path.is_file() else {}

    def called_tools(self) -> list[str]:
        return [c.args[0] for c in self.mocks["call"].await_args_list]


class SkipPathsTests(_Base):
    def test_not_configured_creates_nothing(self) -> None:
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out, {"toolkit": "gmail", "polled": False, "new_events": 0, "error": None,
                               "skipped": "not_configured"})
        self.assertFalse(self.folder().exists())
        self.assertFalse((self.root / ".quirq" / "connections").exists())
        self.mocks["call"].assert_not_awaited()
        self.mocks["userid"].assert_not_called()

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
        # the cold account cache adds the identity call, ahead of the collector
        self.assertEqual(self.mocks["call"].await_count, 2)
        self.assertEqual(self.mocks["call"].await_args_list[0].args, ("GMAIL_GET_PROFILE", {"user_id": "me"}))
        tool, args = self.mocks["call"].await_args.args
        self.assertEqual((tool, args), ("GMAIL_FETCH_EMAILS", {"query": "is:unread", "max_results": 20, "verbose": False, "include_payload": False}))
        self.assertEqual(self.mocks["call"].await_args.kwargs, {"raise_on_tool_error": True})
        self.assertEqual(self.accounts()["gmail"]["label"], EMAIL)
        self.mocks["entry"].assert_called_once_with("user_x")
        self.assertEqual([s.url for s in FakeSession.opened], [ENTRY["url"]], "one session, on the minted entry")
        self.assertEqual(FakeSession.opened[0].timeout_s, poller.CALL_TIMEOUT_S)
        self.assertEqual(FakeSession.closed, 1, "closed when the poll ends")
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
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS", "GMAIL_FETCH_EMAILS"],
                         "the identity call, then both collectors")
        self.assertEqual((len(FakeSession.opened), self.mocks["names"].await_count, FakeSession.closed), (1, 1, 1),
                         "the identity call and both collectors ran through one session and one listing")

    def test_identity_is_the_local_user_id(self) -> None:
        store.write_config("gmail")
        self.assertTrue(self.run_(poller.poll_connection("gmail"))["polled"])
        self.assertTrue(self.run_(poller.poll_connection("gmail", force=True))["polled"])
        self.mocks["entry"].assert_called_with("user_x")


class CollectorFailureTests(_Base):
    def test_one_failing_collector_does_not_stop_the_others(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["call"].side_effect = [profile(), McpError("HTTP 500 boom"), envelope([message(7)])]
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

    def test_composio_envelope_failure_is_an_execute_stage_error(self) -> None:
        """The poller's own McpError for ``successful: false`` carries stage
        ``execute`` and no status, like every other raise site, so a caller
        classifies it by attributes rather than by its text."""
        store.write_config("gmail")
        self.mocks["call"].return_value = envelope([message(1)], successful=False, error="scope missing")
        spec = collectors.collector("gmail", "unread")

        async def scenario():
            with self.assertRaises(McpError) as ctx:
                await poller._run_collector("gmail", spec, FakeSession(ENTRY), store.read_state("gmail"),
                                            datetime.now(timezone.utc), ["GMAIL_FETCH_EMAILS"])
            return ctx.exception

        exc = self.run_(scenario())
        self.assertEqual((exc.stage, exc.status, str(exc)), ("execute", None, "scope missing"))

    def test_no_active_connection_is_reworded_for_a_person(self) -> None:
        """Composio's tool router answers a toolkit whose connected account
        expired or was revoked with an instruction meant for a model; the
        person reads a reconnect hint instead."""
        store.write_config("gmail")
        self.mocks["call"].side_effect = McpError(
            "No active connection found for toolkit(s) 'gmail' in this session. To fix this, call "
            "COMPOSIO_MANAGE_CONNECTIONS with toolkits=['gmail'] to establish a connection, then retry this tool call.",
            stage="execute")
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["error"], "unread: gmail is no longer connected on Composio (the sign-in expired or "
                                       "was revoked): reconnect it from the Connectors tab")
        self.assertNotIn("COMPOSIO_MANAGE_CONNECTIONS", store.read_state("gmail")["last_error"])

    def test_an_error_object_in_the_envelope_reads_as_its_message(self) -> None:
        store.write_config("gmail")
        self.mocks["call"].return_value = envelope([], successful=False, error={"message": "boom"})
        self.assertEqual(self.run_(poller.poll_connection("gmail"))["error"], "unread: boom")

    def test_a_provider_quota_error_is_reworded(self) -> None:
        store.write_config("gmail")
        self.mocks["call"].return_value = envelope(
            [], successful=False,
            error={"error": {"code": 403, "message": "Quota exceeded for quota metric 'Queries' and limit "
                                                       "'Queries per minute' of service 'calendar-json.googleapis.com'"}})
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["error"], "unread: the provider's rate limit was hit (queries per minute); the next poll retries")

    def test_calendar_warnings_reach_last_error_while_its_events_land(self) -> None:
        """The all-calendars tool answers successful true with one calendar's
        failure inside errors_by_calendar: the event that did arrive is
        appended, the failure is in last_error, and last_ok_at stays unset."""
        store.write_config("googlecalendar")
        payload = {"successful": True, "error": None, "data": {
            "events": [{"event": {"id": "ev1", "summary": "Standup", "start": {"dateTime": "2026-09-12T09:00:00Z"},
                                  "htmlLink": "https://calendar.google.com/event?eid=1"},
                        "source_calendar_id": "team@group.calendar.google.com", "source_calendar_summary": "Team"}],
            "summary_view": [], "calendars_queried": [{"id": "primary"}, {"id": "team@group.calendar.google.com"}],
            "errors_by_calendar": {"primary": "403 Quota exceeded for quota metric 'Queries'"}}}
        self.mocks["call"].side_effect = [calendar_profile(), {"content": [{"type": "text", "text": json.dumps(payload)}]}]
        out = self.run_(poller.poll_connection("googlecalendar"))
        self.assertEqual((out["polled"], out["new_events"]), (True, 1))
        self.assertEqual(self.accounts()["googlecalendar"]["label"], EMAIL, "the primary calendar's id")
        self.assertTrue(out["error"].startswith("upcoming: 1 calendar(s) failed: primary: 403 Quota exceeded"), out["error"])
        state_doc = store.read_state("googlecalendar")
        self.assertEqual((state_doc["events_total"], state_doc["last_ok_at"]), (1, None))
        lines = (store.connection_dir("googlecalendar") / "events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["title"] for line in lines], ["Standup"])

    def test_a_truncated_answer_is_read_and_reported(self) -> None:
        store.write_config("gmail")
        payload = {"successful": True, "data": {"messages": [message(1)]}, "error": None, "truncated": True}
        self.mocks["call"].return_value = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["new_events"], 1)
        self.assertIn("only a preview", out["error"])

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

        self.mocks["call"].side_effect = slow
        with patch.object(poller, "CALL_TIMEOUT_S", 0.01), patch.object(poller, "_GRACE_S", 0.01):
            out = self.run_(poller.poll_connection("gmail"))
        self.assertTrue(out["polled"])
        self.assertIn("unread: timed out", out["error"])
        self.assertEqual(FakeSession.closed, 1, "the session is closed after a timed-out collector")

    def test_errors_are_joined_and_a_later_success_clears_them(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["call"].side_effect = [profile(), McpError("a"), McpError("b")]
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
        self.mocks["configured"].return_value = False
        self.assert_failed(self.run_(poller.poll_connection("gmail")), poller.NOT_SIGNED_IN)
        self.mocks["entry"].assert_not_called()
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


class AccountLookupTests(_Base):
    """A poll resolves the connected account on its own session before the
    collectors whenever the cache is cold, stale, or bound to another pinned
    id; the lookup is best-effort and never touches the poll's outcome."""

    def pin(self, toolkit: str, *ids: str) -> None:
        self.mocks["pins"].return_value = {toolkit: {"enabled": True, "connected_account_ids": list(ids)}}

    def test_cold_cache_looks_the_account_up_once_and_writes_accounts_json(self) -> None:
        store.write_config("gmail")
        self.pin("gmail", "ca_1", "ca_2")
        self.mocks["call"].side_effect = [profile(), envelope([message(1)])]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, None))
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS"], "identity first")
        self.assertEqual(self.mocks["call"].await_args_list[0].args[1], {"user_id": "me"})
        entry = self.accounts()["gmail"]
        self.assertEqual((entry["label"], entry["connected_account_id"]), (EMAIL, "ca_1"), "the first pinned id")
        self.assertEqual(entry["checked_at"], store.read_state("gmail")["last_poll_at"], "stamped with the poll's now")
        self.assertFalse((self.root / ".quirq" / "connections" / "gmail" / "accounts.json").exists(),
                         "one file beside the folders, not inside one")

    def test_a_second_poll_within_the_ttl_makes_no_identity_call(self) -> None:
        store.write_config("gmail")
        self.run_(poller.poll_connection("gmail"))
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS"])
        self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS", "GMAIL_FETCH_EMAILS"])
        self.assertTrue(poller.account_is_fresh("gmail", datetime.now(timezone.utc)))

    def test_a_changed_pinned_id_or_an_expired_check_triggers_one_lookup(self) -> None:
        store.write_config("gmail")
        self.pin("gmail", "ca_1")
        self.run_(poller.poll_connection("gmail"))
        self.assertEqual(self.accounts()["gmail"]["connected_account_id"], "ca_1")
        self.pin("gmail", "ca_2")                                   # another account pinned here
        self.mocks["call"].side_effect = [profile("other@example.com"), envelope([])]
        self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(self.called_tools()[-2:], ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS"])
        entry = self.accounts()["gmail"]
        self.assertEqual((entry["label"], entry["connected_account_id"]), ("other@example.com", "ca_2"))
        self.mocks["call"].side_effect = None
        self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(self.called_tools()[-1], "GMAIL_FETCH_EMAILS", "the new pin is cached now")
        # older than the TTL: looked up again, same pin
        stale = datetime.now(timezone.utc) - timedelta(seconds=poller.ACCOUNT_TTL_S + 1)
        store.remember_account("gmail", "other@example.com", "ca_2", now=stale)
        self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(self.called_tools()[-2:], ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS"])
        self.assertNotEqual(self.accounts()["gmail"]["checked_at"], collectors.iso(stale))
        # unpinned then and now is the same account
        self.mocks["pins"].return_value = {}
        store.remember_account("gmail", EMAIL, None)
        self.mocks["call"].reset_mock()
        self.run_(poller.poll_connection("gmail", force=True))
        self.assertEqual(self.called_tools(), ["GMAIL_FETCH_EMAILS"])

    def test_an_identity_failure_never_reaches_the_poll_outcome(self) -> None:
        store.write_config("gmail")
        self.mocks["call"].side_effect = [McpError("HTTP 500 profile down"), envelope([message(1), message(2)])]
        with self.assertLogs(poller.logger, level="WARNING") as logs:
            out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out, {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None})
        state_doc = store.read_state("gmail")
        self.assertIsNone(state_doc["last_error"])
        self.assertEqual(state_doc["last_ok_at"], state_doc["last_poll_at"])
        self.assertEqual([e["key"] for e in self.events()], ["m2", "m1"])
        self.assertEqual(self.accounts(), {}, "nothing remembered")
        self.assertTrue(any("gmail: account lookup failed: HTTP 500 profile down" in line for line in logs.output),
                        logs.output)
        # an answer without a label, a successful:false envelope and a timeout are failures of the same kind
        for answer in (envelope([message(1)]), envelope([], successful=False, error="scope missing")):
            with self.subTest(answer=answer):
                self.mocks["call"].side_effect = [answer, envelope([message(1)])]
                with self.assertLogs(poller.logger, level="WARNING"):
                    out = self.run_(poller.poll_connection("gmail", force=True))
                self.assertIsNone(out["error"])
                self.assertEqual(self.accounts(), {})

    def test_a_toolkit_without_an_identity_spec_makes_no_identity_call(self) -> None:
        store.write_config("notion")
        self.mocks["call"].return_value = {"content": [{"type": "text", "text": json.dumps(
            {"successful": True, "data": {"results": []}, "error": None})}]}
        out = self.run_(poller.poll_connection("notion"))
        self.assertEqual((out["polled"], out["error"]), (True, None))
        self.assertEqual(self.called_tools(), ["NOTION_SEARCH_NOTION_PAGE"])
        self.assertEqual(self.accounts(), {})
        self.assertIsNone(self.run_(poller.resolve_account("notion", FakeSession(ENTRY), DEFAULT_TOOLS,
                                                           datetime.now(timezone.utc))))

    def test_a_session_that_lists_neither_the_tool_nor_the_executor_is_refused_before_any_call(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].return_value = ["GMAIL_FETCH_EMAILS"]
        with self.assertLogs(poller.logger, level="WARNING") as logs:
            out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["new_events"], out["error"]), (2, None))
        self.assertEqual(self.called_tools(), ["GMAIL_FETCH_EMAILS"])
        self.assertTrue(any("GMAIL_GET_PROFILE is not exposed" in line for line in logs.output), logs.output)

    def test_freshness_and_the_pinned_id(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertFalse(poller.account_is_fresh("gmail", now), "nothing cached")
        store.remember_account("gmail", EMAIL, None, now=now - timedelta(hours=1))
        self.assertTrue(poller.account_is_fresh("gmail", now))
        self.assertTrue(poller.account_is_fresh("gmail", now.replace(tzinfo=None)), "a naive now is UTC")
        self.assertFalse(poller.account_is_fresh("gmail", now + timedelta(seconds=poller.ACCOUNT_TTL_S)))
        self.assertFalse(poller.account_is_fresh("gmail", now - timedelta(hours=2)), "checked in the future: stale")
        self.pin("gmail", "ca_1")
        self.assertFalse(poller.account_is_fresh("gmail", now), "cached unpinned, pinned now")
        store.remember_account("gmail", EMAIL, "ca_1", now=now)
        self.assertTrue(poller.account_is_fresh("gmail", now))
        self.assertEqual(poller.pinned_account_id("gmail"), "ca_1")
        self.assertIsNone(poller.pinned_account_id("notion"))
        self.mocks["pins"].return_value = {"gmail": {"enabled": True, "connected_account_ids": []}}
        self.assertIsNone(poller.pinned_account_id("gmail"))
        self.mocks["pins"].side_effect = OSError("unreadable")
        self.assertIsNone(poller.pinned_account_id("gmail"), "an unreadable scope reads as nothing pinned")
        self.assertEqual((poller.ACCOUNT_TTL_S, poller.ACCOUNT_MIN_REFRESH_S), (86400, 60))


class RefreshAccountTests(_Base):
    """``poller.refresh_account``: the route's "resolve it now" on a session
    of its own, answered in the route's shape, never raising for a provider
    or session failure."""

    def refresh(self, toolkit: str = "gmail", **kwargs) -> dict:
        return self.run_(poller.refresh_account(toolkit, **kwargs))

    def test_gmail_resolves_on_its_own_session_and_caches(self) -> None:
        self.mocks["pins"].return_value = {"gmail": {"enabled": True, "connected_account_ids": ["ca_1"]}}
        self.mocks["call"].return_value = profile()
        out = self.refresh()
        self.assertEqual({k: v for k, v in out.items() if k != "account_checked_at"},
                         {"toolkit": "gmail", "account_label": EMAIL, "error": None, "cached": False})
        self.assertRegex(out["account_checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE"])
        self.assertEqual((len(FakeSession.opened), FakeSession.closed, self.mocks["names"].await_count), (1, 1, 1))
        self.assertEqual(FakeSession.opened[0].timeout_s, poller.CALL_TIMEOUT_S)
        entry = self.accounts()["gmail"]
        self.assertEqual((entry["label"], entry["connected_account_id"], entry["checked_at"]),
                         (EMAIL, "ca_1", out["account_checked_at"]))
        self.assertFalse((self.folder() / "config.json").exists(), "no polling config is created")
        self.assertFalse((self.folder()).exists())

    def test_a_toolkit_without_a_lookup_answers_without_a_session(self) -> None:
        self.assertEqual(self.refresh("notion"), {"toolkit": "notion", "account_label": None, "account_checked_at": None,
                                                  "error": "no account lookup for notion yet", "cached": False})
        self.assertEqual(self.refresh("figma")["error"], "no account lookup for figma yet")
        self.assertEqual(FakeSession.opened, [])
        self.mocks["call"].assert_not_awaited()
        self.mocks["userid"].assert_not_called()

    def test_twice_within_a_minute_answers_from_the_cache(self) -> None:
        self.mocks["call"].return_value = profile()
        first = self.refresh()
        second = self.refresh()
        self.assertEqual(second, {**first, "cached": True})
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE"], "one provider call")
        self.assertEqual(len(FakeSession.opened), 1)
        self.assertEqual(self.refresh(force=True)["cached"], False)
        self.assertEqual(len(self.called_tools()), 2, "force asks the provider again")
        # a check older than the minute is not answered from the cache
        store.remember_account("gmail", EMAIL, None,
                               now=datetime.now(timezone.utc) - timedelta(seconds=poller.ACCOUNT_MIN_REFRESH_S + 1))
        self.assertEqual(self.refresh()["cached"], False)
        self.assertEqual(len(self.called_tools()), 3)

    def test_a_provider_failure_keeps_the_cached_label_and_rewords_the_error(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(minutes=2)
        store.remember_account("gmail", EMAIL, None, now=old)
        self.mocks["call"].side_effect = McpError(
            "No active connection found for toolkit(s) 'gmail' in this session. To fix this, call "
            "COMPOSIO_MANAGE_CONNECTIONS with toolkits=['gmail'] to establish a connection, then retry this tool call.",
            stage="execute")
        with self.assertLogs(poller.logger, level="WARNING"):
            out = self.refresh()
        self.assertEqual(out, {"toolkit": "gmail", "account_label": EMAIL, "account_checked_at": collectors.iso(old),
                               "error": "gmail is no longer connected on Composio (the sign-in expired or was revoked): "
                                        "reconnect it from the Connectors tab", "cached": False})
        self.assertEqual(FakeSession.closed, 1, "the session is closed after the failed call")
        self.assertEqual(self.accounts()["gmail"]["checked_at"], collectors.iso(old), "the cache is untouched")
        # without a cache the label is null
        store.forget_account("gmail")
        self.mocks["call"].side_effect = McpError("[Session Restriction] Toolkit 'gmail' is not allowed", stage="execute")
        with self.assertLogs(poller.logger, level="WARNING"):
            out = self.refresh()
        self.assertEqual((out["account_label"], out["account_checked_at"], out["cached"]), (None, None, False))
        self.assertEqual(out["error"], "gmail is turned off for this workspace's Composio session: "
                                       "turn it on from the Connectors tab")
        for answer in (envelope([message(1)]), envelope([], successful=False, error={"message": "boom"})):
            with self.subTest(answer=answer):
                self.mocks["call"].side_effect = None
                self.mocks["call"].return_value = answer
                with self.assertLogs(poller.logger, level="WARNING"):
                    out = self.refresh()
                self.assertIsNotNone(out["error"])
                self.assertIsNone(out["account_label"])

    def test_session_failures_answer_in_the_same_shape(self) -> None:
        store.remember_account("gmail", EMAIL, None,
                               now=datetime.now(timezone.utc) - timedelta(minutes=2))
        self.mocks["configured"].return_value = False
        out = self.refresh()
        self.assertEqual((out["error"], out["account_label"], out["cached"]), (poller.NOT_SIGNED_IN, EMAIL, False))
        self.mocks["entry"].assert_not_called()
        self.mocks["configured"].return_value = True
        self.mocks["scope"].return_value = ["notion"]
        self.assertEqual(self.refresh()["error"], "gmail is not turned on in this workspace")
        self.mocks["scope"].return_value = ["gmail"]
        self.mocks["entry"].side_effect = poller.composio_service.NoToolkitsEnabled("none")
        self.assertEqual(self.refresh()["error"], poller.NO_TOOLKITS)
        self.mocks["entry"].side_effect = None
        self.mocks["names"].side_effect = McpError("tools/list failed: HTTP 500 upstream")
        out = self.refresh()
        self.assertTrue(out["error"].startswith("tools/list failed"), out["error"])
        self.assertEqual(FakeSession.closed, 1, "a session whose listing failed is closed")
        self.mocks["call"].assert_not_awaited()
        self.assertEqual(self.accounts()["gmail"]["label"], EMAIL, "the cache is untouched by any of it")

    def test_a_dead_session_is_replaced_once_like_a_poll(self) -> None:
        fresh = dict(ENTRY, url="https://mcp.example.test/mcp-fresh")
        self.mocks["entry"].side_effect = [ENTRY, fresh]
        self.mocks["open"].side_effect = [dead_session(), None]
        self.mocks["call"].return_value = profile()
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.refresh()
        invalidate.assert_called_once()
        self.assertEqual((out["account_label"], out["error"]), (EMAIL, None))
        self.assertEqual([s.url for s in FakeSession.opened], [fresh["url"]])
        self.assertEqual(FakeSession.closed, 2)

    def test_the_service_checks_the_catalog_then_delegates(self) -> None:
        for bad in ("nope_toolkit", "Gmail", "a/b", "", None):
            with self.subTest(toolkit=bad):
                with self.assertRaises(connections_service.ConnectionsError) as ctx:
                    self.run_(connections_service.refresh_account(bad))
                self.assertEqual((ctx.exception.code, ctx.exception.status), ("unknown_toolkit", 404))
        answer = {"toolkit": "gmail", "account_label": EMAIL, "account_checked_at": "2026-09-11T10:00:00Z",
                  "error": None, "cached": True}
        with patch.object(poller, "refresh_account", new=AsyncMock(return_value=answer)) as ra:
            self.assertEqual(self.run_(connections_service.refresh_account("gmail")), answer)
        ra.assert_awaited_once_with("gmail")


class TickTests(_Base):
    def test_poll_once_summary_and_single_identity_resolution(self) -> None:
        store.write_config("gmail")
        store.write_config("notion", enabled=False)
        store.write_config("googlecalendar")
        (self.root / ".quirq" / "connections" / "figma").mkdir()          # no config.json: never polled
        self.mocks["call"].return_value = envelope([message(1)], email=EMAIL)
        summary = self.run_(poller.poll_once())
        self.assertEqual(summary, {"configured": 3, "polled": 2, "skipped": 1, "errors": 0})
        self.assertEqual(self.mocks["userid"].call_count, 1, "identity resolved once per tick")
        self.assertEqual(self.mocks["entry"].call_count, 2)
        self.assertFalse((self.root / ".quirq" / "connections" / "figma" / "state.json").exists())
        self.assertEqual(self.run_(poller.poll_once()), {"configured": 3, "polled": 0, "skipped": 3, "errors": 0})
        self.assertEqual(self.mocks["userid"].call_count, 1, "nothing due: no identity resolution")

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
        self.mocks["configured"].return_value = False
        self.assertEqual(self.run_(poller.poll_once()), {"configured": 1, "polled": 0, "skipped": 0, "errors": 1})
        self.assertEqual(store.read_state("gmail")["last_error"], poller.NOT_SIGNED_IN)

    def test_cancellation_propagates_and_releases_the_lock(self) -> None:
        store.write_config("gmail")

        async def slow(*args, **kwargs):
            await asyncio.sleep(30)

        self.mocks["call"].side_effect = slow
        task = self.loop.create_task(poller.poll_connection("gmail"))
        self.loop.call_later(0.05, task.cancel)
        with self.assertRaises(asyncio.CancelledError):
            self.loop.run_until_complete(task)
        self.assertFalse(poller._lock("gmail").locked())
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertEqual(FakeSession.closed, 1, "the session is closed on cancel too")

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
        # a top-level services package: what a person uses as much as the agent does is Space code
        root = Path(__file__).resolve().parents[1] / "modules" / "connections"
        self.assertFalse((root.parents[1] / "services" / "cowork_agent" / "connections").exists())
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
             self.assertLogs(signals.logger, level="WARNING"):
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
        inbox_path = self.root / ".quirq" / "inbox" / "inbox.json"
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
        # the identity call (first, cold cache) and the collector both route through the executor
        self.mocks["call"].side_effect = [
            router_result(results=[{"tool_slug": "GMAIL_GET_PROFILE", "index": 0,
                                    "response": {"successful": True, "data": {"emailAddress": EMAIL}}}]),
            router_result(results=[{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                                    "response": {"successful": True, "data": {"messages": [message(1)]}}}]),
        ]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, None))
        self.assertEqual([e["key"] for e in self.events()], ["m1"])
        self.assertEqual(self.accounts()["gmail"]["label"], EMAIL)
        first, called = self.mocks["call"].call_args_list
        self.assertEqual(first.args[0], mcp_client.EXECUTOR_TOOL)
        self.assertEqual(first.args[1]["tools"], [{"tool_slug": "GMAIL_GET_PROFILE", "arguments": {"user_id": "me"}}])
        self.assertEqual(called.args[0], mcp_client.EXECUTOR_TOOL)
        self.assertEqual(called.args[1]["tools"][0]["tool_slug"], "GMAIL_FETCH_EMAILS")
        self.assertEqual(called.args[1]["tools"][0]["arguments"]["query"], "is:unread")
        self.assertIs(called.args[1]["sync_response_to_workbench"], False)
        self.assertIs(called.kwargs.get("raise_on_tool_error"), False)

    def test_executor_per_tool_error_is_recorded_for_that_collector(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        self.mocks["names"].return_value = [mcp_client.EXECUTOR_TOOL]
        self.mocks["call"].side_effect = [
            router_result(results=[{"tool_slug": "GMAIL_GET_PROFILE", "index": 0,
                                    "response": {"successful": True, "data": {"emailAddress": EMAIL}}}]),
            router_result(results=[{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                                    "error": "[Session Restriction] Toolkit 'gmail' is not allowed"}], is_error=True),
            router_result(results=[{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                                    "response": {"successful": True, "data": {"messages": [message(3)]}}}]),
        ]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["new_events"], 1)
        # Composio's "[Session Restriction]" is reworded for a person (humanize_error)
        self.assertEqual(out["error"], "unread: gmail is turned off for this workspace's Composio session: "
                                       "turn it on from the Connectors tab")
        self.assertEqual([e["key"] for e in self.events()], ["m3"])

    def test_tools_list_failure_is_a_poll_failure_without_events(self) -> None:
        store.write_config("gmail")
        self.mocks["names"].side_effect = McpError("tools/list failed: HTTP 500 upstream")
        out = self.run_(poller.poll_connection("gmail"))
        self.assertTrue(out["error"].startswith("tools/list failed"), out["error"])
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertIsNone(store.read_state("gmail")["last_ok_at"])
        self.mocks["call"].assert_not_called()
        self.assertEqual(FakeSession.closed, 1, "a session whose listing failed is closed")

    def test_dead_session_is_invalidated_and_retried_once(self) -> None:
        store.write_config("gmail")
        fresh = dict(ENTRY, url="https://mcp.example.test/mcp-fresh")
        self.mocks["entry"].side_effect = [ENTRY, fresh]
        self.mocks["open"].side_effect = [dead_session(), None]
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertEqual(self.mocks["entry"].call_count, 2, "a fresh entry is built after the invalidation")
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 2, None))
        self.assertEqual([c.args for c in self.mocks["open"].await_args_list], [(ENTRY["url"],), (fresh["url"],)])
        self.assertEqual([s.url for s in FakeSession.opened], [fresh["url"]], "the collectors ran on the fresh session")
        self.assertEqual(FakeSession.closed, 2, "the dead session and the fresh one are both closed")

    def test_dead_session_twice_is_a_recorded_failure(self) -> None:
        store.write_config("gmail")
        self.mocks["open"].side_effect = [dead_session(), dead_session(), dead_session()]
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertIn("initialize failed: HTTP 404", out["error"])
        self.mocks["call"].assert_not_called()
        self.assertEqual(self.mocks["open"].await_count, 2, "exactly one retry")
        self.assertEqual(FakeSession.closed, 2, "both dead sessions are closed")

    def test_a_404_in_the_text_alone_is_not_a_dead_session(self) -> None:
        """The decision reads the error's stage and status; a message that
        merely looks like one (no stage, or another stage) heals nothing."""
        store.write_config("gmail")
        for exc in (McpError("initialize failed: HTTP 404 gone"),
                    McpError("tools/list failed: HTTP 404 nope", stage="tools/list", status=404),
                    McpError("initialize failed: HTTP 500 down", stage="initialize", status=500)):
            with self.subTest(exc=exc):
                self.mocks["open"].reset_mock()
                self.mocks["entry"].reset_mock()
                self.mocks["open"].side_effect = exc
                with patch.object(poller.composio_service, "invalidate_session") as invalidate:
                    out = self.run_(poller.poll_connection("gmail", force=True))
                invalidate.assert_not_called()
                self.assertEqual(self.mocks["entry"].call_count, 1, "no re-mint")
                self.assertEqual(self.mocks["open"].await_count, 1, "no retry")
                self.assertTrue(out["error"].startswith("tools/list failed: " + str(exc)), out["error"])

    def test_a_failing_close_does_not_mask_the_listing_error_or_block_healing(self) -> None:
        """``_open_and_list`` closes the session it could not list on; a close
        that raises is logged at debug, and the handshake or listing error is
        what the poll records (and what the dead-session check reads)."""
        store.write_config("gmail")
        listing = McpError("tools/list failed: HTTP 500 upstream", stage="tools/list", status=500)
        self.mocks["names"].side_effect = listing
        with patch.object(FakeSession, "close", new=AsyncMock(side_effect=[RuntimeError("aclose boom")])), \
             self.assertLogs(poller.logger, level="DEBUG") as logs:
            out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(out["error"], "tools/list failed: " + str(listing))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.mocks["names"].side_effect = None
        self.mocks["open"].side_effect = [dead_session(), None]
        with patch.object(FakeSession, "close", new=AsyncMock(side_effect=[RuntimeError("aclose boom"), None])), \
             patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail", force=True))
        invalidate.assert_called_once()
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 2, None))

    def test_a_session_that_dies_mid_poll_ends_the_collector_loop(self) -> None:
        """A collector's ``tools/call`` answering HTTP 404 means the session
        is gone: the collectors after it are recorded as not attempted, not
        run on the dead session, and the next poll's fresh session runs all
        of them again."""
        store.write_config("gmail", collectors=["unread", "inbox"])
        lost = McpError("tools/call GMAIL_FETCH_EMAILS failed: HTTP 404 session not found",
                        stage="tools/call", status=404)
        self.mocks["call"].side_effect = [profile(), lost, envelope([message(3)])]
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"]), (True, 0))
        self.assertEqual(out["error"], f"unread: {lost}; inbox: {poller.SESSION_LOST}")
        self.assertEqual(self.called_tools(), ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS"],
                         "the second collector never ran on the dead session")
        self.assertEqual(FakeSession.closed, 1)
        self.assertFalse((self.folder() / "events.jsonl").exists())
        state_doc = store.read_state("gmail")
        self.assertEqual(state_doc["last_error"], out["error"])
        self.assertIsNone(state_doc["last_ok_at"])
        self.mocks["call"].side_effect = None
        out = self.run_(poller.poll_connection("gmail", force=True))
        # the account is cached now, so the second poll is its two collectors only
        self.assertEqual((out["new_events"], out["error"], self.mocks["call"].await_count), (4, None, 4))
        self.assertEqual(FakeSession.closed, 2)

    def test_other_collector_failures_do_not_end_the_loop(self) -> None:
        """Only ``tools/call`` + 404 stops the loop: another status, another
        stage, or a 404 in the text alone leaves the other collectors running."""
        store.write_config("gmail", collectors=["unread", "inbox"])
        store.remember_account("gmail", EMAIL, None)     # cached: every poll below is its two collectors only
        for exc in (McpError("HTTP 500", stage="tools/call", status=500),
                    McpError("restricted", stage="execute"),
                    McpError("gone", stage="initialize", status=404),
                    McpError("tools/call X failed: HTTP 404 nope")):
            with self.subTest(exc=exc):
                self.mocks["call"].reset_mock()
                self.mocks["call"].side_effect = [exc, envelope([message(7)])]
                out = self.run_(poller.poll_connection("gmail", force=True))
                self.assertEqual(self.mocks["call"].await_count, 2, "both collectors ran")
                self.assertEqual(out["error"], f"unread: {exc}")

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


class SessionGoneTests(unittest.TestCase):
    """A dead Composio session is ``initialize`` + HTTP 404 on the error's
    attributes; the message text is never consulted."""

    def test_stage_and_status_decide(self) -> None:
        text = "initialize failed: HTTP 404 gone"
        self.assertTrue(poller._session_gone(McpError(text, stage="initialize", status=404)))
        self.assertTrue(poller._session_gone(McpError("anything", stage="initialize", status=404)))
        for exc in (McpError(text), McpError(text, stage="initialize"), McpError(text, status=404),
                    McpError(text, stage="tools/list", status=404), McpError(text, stage="initialize", status=500),
                    RuntimeError(text)):
            with self.subTest(exc=exc):
                self.assertFalse(poller._session_gone(exc))

    def test_session_lost_reads_tools_call_and_404(self) -> None:
        text = "tools/call X failed: HTTP 404 session not found"
        self.assertTrue(poller._session_lost(McpError(text, stage="tools/call", status=404)))
        self.assertTrue(poller._session_lost(McpError("anything", stage="tools/call", status=404)))
        for exc in (McpError(text), McpError(text, stage="tools/call"), McpError(text, status=404),
                    McpError(text, stage="initialize", status=404), McpError(text, stage="tools/call", status=500),
                    McpError(text, stage="execute"), RuntimeError(text)):
            with self.subTest(exc=exc):
                self.assertFalse(poller._session_lost(exc))


class ScriptedUpstream:
    """An MCP server behind ``httpx.MockTransport``: the handshake,
    ``tools/list`` and ``tools/call`` (every answer carries the request's
    id), ``DELETE``, and HTTP 404 on ``initialize`` for the first ``dead``
    sessions, the way Composio answers for a tool-router session that is
    gone; ``call_status`` other than 200 makes every ``tools/call`` answer
    that status with the same not-found body. Records every request."""

    def __init__(self, *, tools: list, call_result: dict, dead: int = 0, call_status: int = 200) -> None:
        self.tools, self.call_result, self.dead, self.call_status = tools, call_result, dead, call_status
        self.requests: list = []
        self.sessions = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            self.sessions += 1
            if self.sessions <= self.dead:
                return httpx.Response(404, json={"error": {"message": "Tool router session with ID trs_x not found"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26"}},
                                  headers={"Mcp-Session-Id": f"sess-{self.sessions}"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                             "result": {"tools": [{"name": n} for n in self.tools]}})
        if method == "tools/call":
            if self.call_status != 200:
                return httpx.Response(self.call_status,
                                      json={"error": {"message": "Tool router session with ID trs_x not found"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": self.call_result})
        return httpx.Response(400, json={"error": "unexpected"})

    def methods(self) -> list:
        return [r.method if r.method == "DELETE" else json.loads(r.content).get("method") for r in self.requests]

    def ids(self) -> list:
        return [json.loads(r.content).get("id") for r in self.requests
                if r.method == "POST" and "id" in json.loads(r.content)]


class RealSessionTests(_Base):
    """The poller over the real ``McpSession`` and a scripted upstream: one
    handshake per poll, every collector through it, one DELETE."""

    REAL_SESSION = True

    def use(self, up: ScriptedUpstream) -> ScriptedUpstream:
        p = patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler))
        p.start()
        self.addCleanup(p.stop)
        return up

    def test_one_session_serves_every_collector(self) -> None:
        store.write_config("gmail", collectors=["unread", "inbox"])
        up = self.use(ScriptedUpstream(tools=["GMAIL_FETCH_EMAILS"], call_result=envelope([message(1), message(2)])))
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 4, None))
        self.assertEqual(up.methods(), ["initialize", "notifications/initialized", "tools/list",
                                        "tools/call", "tools/call", "DELETE"])
        self.assertEqual(up.ids(), [1, 2, 3, 4], "one id per request, counted up within the session")
        self.assertNotIn("mcp-session-id", up.requests[0].headers)
        for req in up.requests[1:]:
            self.assertEqual(req.headers["mcp-session-id"], "sess-1")
            self.assertEqual(req.headers["Authorization"], "Bearer t")
        self.assertEqual(sorted(e["type"] for e in self.events()), ["inbox", "inbox", "unread", "unread"])

    def test_dead_session_is_replaced_once_and_the_fresh_one_is_used(self) -> None:
        store.write_config("gmail")
        fresh = dict(ENTRY, url="https://mcp.example.test/mcp-fresh")
        self.mocks["entry"].side_effect = [ENTRY, fresh]
        up = self.use(ScriptedUpstream(tools=["GMAIL_FETCH_EMAILS"], call_result=envelope([message(1)]), dead=1))
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertEqual(self.mocks["entry"].call_count, 2)
        self.assertEqual((out["polled"], out["new_events"], out["error"]), (True, 1, None))
        self.assertEqual(up.methods(), ["initialize", "initialize", "notifications/initialized", "tools/list",
                                        "tools/call", "DELETE"])
        self.assertEqual(str(up.requests[0].url), ENTRY["url"], "the first handshake went to the cached entry")
        for req in up.requests[1:]:
            self.assertEqual(str(req.url), fresh["url"], "everything after ran on the fresh entry")
        for req in up.requests[2:]:
            self.assertEqual(req.headers["mcp-session-id"], "sess-2")

    def test_dead_session_twice_records_the_failure_and_calls_nothing(self) -> None:
        store.write_config("gmail")
        up = self.use(ScriptedUpstream(tools=["GMAIL_FETCH_EMAILS"], call_result=envelope([]), dead=2))
        with patch.object(poller.composio_service, "invalidate_session") as invalidate:
            out = self.run_(poller.poll_connection("gmail"))
        invalidate.assert_called_once()
        self.assertTrue(out["error"].startswith("tools/list failed: initialize failed: HTTP 404"), out["error"])
        self.assertEqual(up.methods(), ["initialize", "initialize"])
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertIsNone(store.read_state("gmail")["last_ok_at"])

    def test_a_session_that_dies_mid_poll_is_left_after_the_first_404(self) -> None:
        """One ``tools/call`` answering HTTP 404 ends the poll's collector
        loop: the second collector is not attempted on the dead session, and
        the session still gets its one DELETE."""
        store.write_config("gmail", collectors=["unread", "inbox"])
        up = self.use(ScriptedUpstream(tools=["GMAIL_FETCH_EMAILS"], call_result=envelope([]), call_status=404))
        out = self.run_(poller.poll_connection("gmail"))
        self.assertEqual(up.methods(), ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"])
        self.assertTrue(out["error"].startswith("unread: tools/call GMAIL_FETCH_EMAILS failed: HTTP 404"), out["error"])
        self.assertTrue(out["error"].endswith("; inbox: " + poller.SESSION_LOST), out["error"])
        self.assertFalse((self.folder() / "events.jsonl").exists())
        self.assertIsNone(store.read_state("gmail")["last_ok_at"])
