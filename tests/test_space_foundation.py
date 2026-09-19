"""The Space foundation under ``services/inbox`` and ``services/connections``:
the file primitives in ``services/storage`` (and the aliases at their former
paths), ``services/timestamps``, ``services/errors`` with its BFF glue in
``routers/cowork_agent/bff/errors.py``, and the new-events listener registry
that replaced the connections -> inbox import."""

from __future__ import annotations

import asyncio
import importlib
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from routers.cowork_agent.bff import errors as bff_errors
from services import errors, timestamps
from services import signals
from modules.connections import collectors, poller
from modules.connections import service as connections_service
from modules.connections import store as connections_store
from services.inbox import service as inbox_service
from services.inbox import store as inbox_store
from services.storage import atomic_write, flock, paths, reader
from utils import runtime_env

ROOT = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\\u2013\\u2014]")
AGENT_NAMES = re.compile(r"openclaw|hermes|claude_code|codex|antigravity")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class StorageMoveTests(unittest.TestCase):
    OLD_TO_NEW = {
        "services.cowork_agent.visualizer.flock": flock,
        "services.cowork_agent.visualizer.atomic_write": atomic_write,
        "services.cowork_agent.visualizer.reader": reader,
        "services.cowork_agent.local_state": paths,
    }
    SHIMS = {
        "services/cowork_agent/visualizer/flock.py": "flock",
        "services/cowork_agent/visualizer/atomic_write.py": "atomic_write",
        "services/cowork_agent/visualizer/reader.py": "reader",
        "services/cowork_agent/local_state.py": "paths",
    }

    def test_former_paths_are_the_storage_modules_not_copies(self) -> None:
        for old, new in self.OLD_TO_NEW.items():
            with self.subTest(old=old):
                self.assertIs(importlib.import_module(old), new)
                self.assertIs(sys.modules[old], new)
        self.assertIs(paths.quirq_state_dir, runtime_env.quirq_state_dir)

    def test_a_patch_through_the_former_path_is_seen_through_the_new_one(self) -> None:
        sentinel = {"patched": True}
        with patch("services.cowork_agent.visualizer.reader.read_json", return_value=sentinel):
            self.assertIs(reader.read_json(Path("/nowhere")), sentinel)
        self.assertIsNone(reader.read_json(Path("/nowhere")))

    def test_former_files_are_shims_and_storage_holds_the_code(self) -> None:
        for rel, target in self.SHIMS.items():
            with self.subTest(file=rel):
                src = read(rel)
                self.assertIn(f"from services.storage import {target} as _moved", src)
                self.assertIn("sys.modules[__name__] = _moved", src)
                self.assertLessEqual(len(src.strip().splitlines()), 4)
        for name in ("__init__", "flock", "atomic_write", "reader", "paths"):
            with self.subTest(module=name):
                src = read(f"services/storage/{name}.py")
                self.assertIsNone(DASHES.search(src))
                self.assertNotRegex(src, AGENT_NAMES)
        self.assertIn("Space-level", read("services/storage/__init__.py"))
        for fn in (flock.locked, atomic_write.write_json_atomic, atomic_write.append_jsonl,
                   reader.read_json, reader.read_jsonl_tail_reverse, paths.legacy_state_dir):
            self.assertTrue(fn.__module__.startswith("services.storage."), fn)

    def test_inbox_and_connections_import_storage_not_the_former_paths(self) -> None:
        for rel in ("services/inbox/store.py", "services/inbox/feeders.py", "modules/connections/store.py"):
            with self.subTest(file=rel):
                src = read(rel)
                self.assertIn("from services.storage.", src)
                for former in ("cowork_agent.visualizer.reader", "cowork_agent.visualizer.flock",
                               "cowork_agent.visualizer.atomic_write", "cowork_agent.local_state"):
                    self.assertNotIn(former, src)


class TimestampsTests(unittest.TestCase):
    def test_parse_ts_accepts_z_offset_and_naive_as_utc(self) -> None:
        z = timestamps.parse_ts("2026-09-10T10:00:00Z")
        self.assertEqual(z, datetime(2026, 9, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(z, timestamps.parse_ts("2026-09-10T10:00:00+00:00"))
        self.assertEqual(z, timestamps.parse_ts("2026-09-10T12:00:00+02:00"))
        self.assertEqual(z, timestamps.parse_ts("2026-09-10T10:00:00"))
        self.assertEqual(z, timestamps.parse_ts("  2026-09-10T10:00:00z "))
        self.assertEqual(z.tzinfo, timezone.utc)
        for bad in (None, "", "   ", "garbage", 1726000000, "2026-13-40T00:00:00Z"):
            self.assertIsNone(timestamps.parse_ts(bad), bad)

    def test_iso_aware_and_now_iso_write_the_one_format(self) -> None:
        naive = datetime(2026, 9, 10, 10, 30, 5)
        self.assertEqual(timestamps.iso(naive), "2026-09-10T10:30:05Z")
        plus_two = datetime(2026, 9, 10, 12, 30, 5, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(timestamps.iso(plus_two), "2026-09-10T10:30:05Z")
        self.assertEqual(timestamps.aware(naive), datetime(2026, 9, 10, 10, 30, 5, tzinfo=timezone.utc))
        self.assertEqual(timestamps.aware(plus_two), timestamps.aware(naive))
        self.assertEqual(timestamps.parse_ts(timestamps.iso(naive)), timestamps.aware(naive))
        self.assertRegex(timestamps.now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(timestamps.EPOCH, datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(timestamps.TS_FORMAT, "%Y-%m-%dT%H:%M:%SZ")

    def test_the_packages_share_the_helpers_rather_than_copying_them(self) -> None:
        self.assertIs(inbox_store.parse_ts, timestamps.parse_ts)
        self.assertIs(inbox_store.now_iso, timestamps.now_iso)
        self.assertIs(inbox_store._EPOCH, timestamps.EPOCH)
        self.assertIs(connections_store.parse_ts, timestamps.parse_ts)
        self.assertIs(connections_store.now_iso, timestamps.now_iso)
        self.assertIs(connections_store._EPOCH, timestamps.EPOCH)
        self.assertIs(collectors.iso, timestamps.iso)
        self.assertIs(collectors.parse_ts, timestamps.parse_ts)
        self.assertEqual(collectors.TS_FORMAT, timestamps.TS_FORMAT)
        self.assertIs(poller.parse_ts, timestamps.parse_ts)
        src = read("services/timestamps.py")
        self.assertIsNone(DASHES.search(src))
        self.assertNotRegex(src, AGENT_NAMES)
        self.assertNotRegex(src, r"(?m)^(from|import) services", "a leaf module")


class ServiceErrorTests(unittest.TestCase):
    def test_shape_and_default_status(self) -> None:
        exc = errors.ServiceError("code_x", "what happened")
        self.assertEqual((exc.code, exc.message, exc.status, str(exc)),
                         ("code_x", "what happened", 400, "what happened"))
        self.assertEqual(errors.ServiceError("c", "m", 404).status, 404)

    def test_inbox_and_connections_errors_are_service_errors_at_their_old_homes(self) -> None:
        self.assertTrue(issubclass(inbox_store.InboxError, errors.ServiceError))
        self.assertTrue(issubclass(connections_store.ConnectionsError, errors.ServiceError))
        self.assertFalse(issubclass(connections_store.ConnectionsError, inbox_store.InboxError))
        self.assertIs(inbox_service.InboxError, inbox_store.InboxError)
        self.assertIs(connections_service.ConnectionsError, connections_store.ConnectionsError)
        self.assertEqual(inbox_store.InboxError.__module__, "services.inbox.store")
        self.assertEqual(connections_store.ConnectionsError.__module__, "modules.connections.store")
        exc = inbox_store.InboxError("item_not_found", "Inbox item not found.", 404)
        self.assertEqual((exc.code, exc.message, exc.status, str(exc)),
                         ("item_not_found", "Inbox item not found.", 404, "Inbox item not found."))
        self.assertEqual(connections_store.ConnectionsError("invalid_value", "bad").status, 400)

    def test_http_error_maps_status_and_detail(self) -> None:
        exc = bff_errors.http_error(connections_store.ConnectionsError("unknown_toolkit", "Unknown toolkit.", 404))
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual((exc.status_code, exc.detail), (404, {"code": "unknown_toolkit", "message": "Unknown toolkit."}))
        exc = bff_errors.http_error(inbox_store.InboxError("invalid_value", "bad"))
        self.assertEqual((exc.status_code, exc.detail), (400, {"code": "invalid_value", "message": "bad"}))

    def test_forbid_extra_rejects_unknown_keys(self) -> None:
        class Body(bff_errors.ForbidExtra):
            status: str

        self.assertEqual(Body(status="seen").status, "seen")
        with self.assertRaises(ValidationError):
            Body(status="seen", extra=1)

    def test_the_two_routers_use_the_shared_glue(self) -> None:
        for rel in ("routers/cowork_agent/bff/inbox.py", "modules/connections/routes.py"):
            with self.subTest(file=rel):
                src = read(rel)
                self.assertIn("from routers.errors import ForbidExtra", src)
                self.assertNotIn("http_error", src, "errors reach the wire through the app handler")
                self.assertNotIn("_ForbidExtra", src)
                self.assertNotIn("def _http(", src)
                self.assertNotIn("ConfigDict", src)
        glue = read("routers/errors.py")
        self.assertIsNone(DASHES.search(glue))
        self.assertNotRegex(glue, AGENT_NAMES)
        self.assertNotRegex(glue, r"^\s*(import os|from os |import pathlib|from pathlib)", "BFF rule P2")
        base = read("services/errors.py")
        self.assertIsNone(DASHES.search(base))
        self.assertNotRegex(base, r"(?m)^(from|import) (services|fastapi|pydantic)", "a leaf module")


class NewEventsListenerTests(unittest.TestCase):
    """``connections.service.poll_now`` raises the ``connections.new_events``
    signal when a poll collected something; the inbox registers a listener
    at import through ``register_new_events_listener`` (``services.signals``
    underneath). The runtime listeners are restored after every test."""

    OUTCOME = {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None}
    SIGNAL = "connections.new_events"

    def setUp(self) -> None:
        self._saved = {k: list(v) for k, v in signals._extra.items()}
        self.loop = asyncio.new_event_loop()

    def tearDown(self) -> None:
        signals._extra.clear()
        signals._extra.update({k: list(v) for k, v in self._saved.items()})
        self.loop.close()

    def run_(self, coro):
        return self.loop.run_until_complete(coro)

    def _only(self, *fns) -> None:
        signals._extra[self.SIGNAL] = [("test", fn) for fn in fns]

    def test_registration_is_by_identity_and_idempotent(self) -> None:
        async def fn(toolkit: str) -> None:
            pass

        signals._extra[self.SIGNAL] = []
        connections_service.register_new_events_listener(fn)
        connections_service.register_new_events_listener(fn)
        self.assertEqual([f for _m, f in signals._extra[self.SIGNAL]], [fn])
        self.assertIn("register_new_events_listener", connections_service.__all__)

    def test_the_inbox_registers_its_ingest_once_at_import(self) -> None:
        registered = [f for _m, f in self._saved.get(self.SIGNAL, [])]
        self.assertEqual(registered.count(inbox_service._ingest_after_poll), 1)
        importlib.import_module("services.inbox.service")
        importlib.import_module("services.inbox")
        registered = [f for _m, f in signals._extra.get(self.SIGNAL, [])]
        self.assertEqual(registered.count(inbox_service._ingest_after_poll), 1)

    def test_poll_now_awaits_every_listener_with_the_toolkit_only_when_something_arrived(self) -> None:
        first, second = AsyncMock(), AsyncMock()
        self._only(first, second)
        with patch.object(poller, "poll_connection", new=AsyncMock(return_value=self.OUTCOME)):
            self.assertEqual(self.run_(connections_service.poll_now("gmail")), self.OUTCOME)
        first.assert_awaited_once_with(toolkit="gmail")
        second.assert_awaited_once_with(toolkit="gmail")
        for outcome in ({**self.OUTCOME, "new_events": 0},
                        {**self.OUTCOME, "polled": False, "new_events": 0, "skipped": "busy"}):
            first.reset_mock()
            second.reset_mock()
            with patch.object(poller, "poll_connection", new=AsyncMock(return_value=outcome)):
                self.assertEqual(self.run_(connections_service.poll_now("gmail")), outcome)
            first.assert_not_awaited()
            second.assert_not_awaited()

    def test_a_failing_listener_is_logged_by_name_and_the_rest_still_run(self) -> None:
        async def broken(toolkit: str) -> None:
            raise OSError("disk")

        after = AsyncMock()
        self._only(broken, after)
        with patch.object(poller, "poll_connection", new=AsyncMock(return_value=self.OUTCOME)), \
             self.assertLogs(signals.logger, level="WARNING") as logs:
            self.assertEqual(self.run_(connections_service.poll_now("gmail")), self.OUTCOME)
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn("broken", message)
        self.assertIn(self.SIGNAL, message)
        self.assertIsNotNone(logs.records[0].exc_info)
        after.assert_awaited_once_with(toolkit="gmail")

    def test_cancellation_inside_a_listener_propagates(self) -> None:
        async def cancelled(toolkit: str) -> None:
            raise asyncio.CancelledError

        self._only(cancelled)
        with patch.object(poller, "poll_connection", new=AsyncMock(return_value=self.OUTCOME)):
            with self.assertRaises(asyncio.CancelledError):
                self.run_(connections_service.poll_now("gmail"))

    def test_connections_never_imports_the_inbox(self) -> None:
        for path in sorted((ROOT / "modules" / "connections").glob("*.py")):
            with self.subTest(file=path.name):
                self.assertNotIn("services.inbox", path.read_text(encoding="utf-8"))
        src = read("services/inbox/service.py")
        self.assertIn("connections_service.register_new_events_listener(_ingest_after_poll)", src)


if __name__ == "__main__":
    unittest.main()
