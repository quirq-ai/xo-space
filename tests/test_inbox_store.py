from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from services.cowork_agent import project_layout

from services.inbox import feeders, service, store
from services.cowork_agent.project_sharing import status as sharing_status

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def item(i: int, *, status: str = "new", ts: str | None = None, **extra) -> dict:
    return {"id": f"{i:08x}", "ts": ts or iso(NOW), "source": "api", "kind": "note",
            "title": f"item {i}", "status": status, **extra}


class InboxStoreTests(unittest.TestCase):
    """Hermetic: XO_PROJECTS_ROOT and QUIRQ_STATE_ROOT point into a temp dir
    (the flock sentinel resolves through QUIRQ_STATE_ROOT at call time), the
    in-memory sharing status is reset, and the per-process throttle is cleared."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root),
                                            "QUIRQ_STATE_ROOT": str(self.root / ".quirq")})
        self._env.start()
        service._reset_throttle()
        sharing_status.reset()

    def tearDown(self) -> None:
        service._reset_throttle()
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    # helpers
    def path(self) -> Path:
        return self.root / ".quirq" / "inbox.json"

    def write(self, doc) -> None:
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text(json.dumps(doc), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.path().read_text(encoding="utf-8"))

    def write_timeline(self, events: list[dict], *, append: bool = False) -> None:
        # the workspace timeline is a runtime-tier file: ~/.quirq/workspace/, not <XO root>/.xo/
        p = project_layout.workspace_runtime_dir() / "timeline.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as fp:
            fp.write("".join(json.dumps(e) + "\n" for e in events))

    def write_todo_sessions(self, pid: str, sessions: dict[str, list[dict]]) -> None:
        d = self.root / pid / ".xo"
        d.mkdir(parents=True, exist_ok=True)
        (d / "todos.json").write_text(json.dumps({"schema": 1, "sessions": {
            sid: {"runtime": "r", "todos": todos} for sid, todos in sessions.items()}}), encoding="utf-8")

    def write_todos(self, pid: str, todos: list[dict]) -> None:
        self.write_todo_sessions(pid, {"s1": todos})

    def by_key(self, key: str) -> dict:
        matches = [it for it in self.read()["items"] if it.get("key") == key]
        self.assertEqual(len(matches), 1, f"expected exactly one item for {key}")
        return matches[0]

    # ── normalisation ───────────────────────────────────────────────────────

    def test_hand_edited_file_is_normalised_and_unknown_keys_survive(self) -> None:
        self.write({"schema": 1, "notes": "keep me", "items": [
            {**item(1), "status": None, "extra": "kept"},        # missing status -> new
            {**item(2), "status": "weird", "link": "nope"},       # bad status -> new, link -> None
            {"id": "cccccccc", "ts": iso(NOW)},                   # no title -> dropped
            {"id": "zzz", "ts": iso(NOW), "title": "bad id"},     # dropped
            {"id": 12345678, "ts": iso(NOW), "title": "int id"},  # dropped
            "junk",                                               # dropped
            {**item(1), "title": "duplicate id"},                 # dropped, first wins
            {**item(3), "ts": "garbage"},                         # ts -> now
        ]})
        del_status = {it["id"]: it["status"] for it in service.list_items(status="all")["items"]}
        self.assertEqual(del_status, {"00000001": "new", "00000002": "new", "00000003": "new"})
        service.create_item("trigger a write")
        doc = self.read()
        self.assertEqual(doc["notes"], "keep me")
        one = next(it for it in doc["items"] if it["id"] == "00000001")
        self.assertEqual(one["extra"], "kept")
        two = next(it for it in doc["items"] if it["id"] == "00000002")
        self.assertIsNone(two["link"])
        three = next(it for it in doc["items"] if it["id"] == "00000003")
        self.assertIsNotNone(store.parse_ts(three["ts"]))
        self.assertEqual(doc["sources"]["todos"]["statuses"], ["blocked"])

    def test_parse_ts_and_newest_first_ordering_across_formats(self) -> None:
        self.assertIsNone(store.parse_ts("garbage"))
        self.assertIsNone(store.parse_ts(None))
        self.assertEqual(store.parse_ts("2026-09-10T10:00:00Z"), store.parse_ts("2026-09-10T10:00:00+00:00"))
        self.assertEqual(store.parse_ts("2026-09-10T10:00:00"), store.parse_ts("2026-09-10T12:00:00+02:00"))
        self.write({"items": [
            item(1, ts="2026-09-10T10:00:00Z"),
            item(2, ts="2026-09-10T11:00:00+00:00"),
            item(3, ts="2026-09-10T09:30:00"),
            item(4, ts="2026-09-10T12:00:00.123456+00:00"),
            item(5, ts="2026-09-10T13:30:00+02:00"),
        ]})
        ids = [it["id"] for it in service.list_items(status="all")["items"]]
        self.assertEqual(ids, ["00000004", "00000005", "00000002", "00000001", "00000003"])

    # ── API operations ──────────────────────────────────────────────────────

    def test_create_update_delete_roundtrip(self) -> None:
        self.assertFalse(self.path().exists())
        self.assertEqual(service.list_items()["counts"], {"new": 0, "seen": 0, "done": 0})
        self.assertFalse(self.path().exists(), "a read-only GET must not create the file")
        created = service.create_item("  hello  ", body="b", link={"view": "projects", "junk": 1})
        self.assertRegex(created["id"], r"^[0-9a-f]{8}$")
        self.assertEqual((created["title"], created["status"], created["link"]), ("hello", "new", {"view": "projects"}))
        self.assertIsNone(service.create_item("empty link", link={})["link"])
        self.assertEqual(service.update_item(created["id"], "seen")["status"], "seen")
        self.assertEqual(next(it for it in self.read()["items"] if it["id"] == created["id"])["status"], "seen")
        with self.assertRaises(store.InboxError) as cm:
            service.update_item("ffffffff", "seen")
        self.assertEqual((cm.exception.code, cm.exception.status), ("item_not_found", 404))
        with self.assertRaises(store.InboxError) as cm:
            service.update_item(created["id"], "archived")
        self.assertEqual((cm.exception.code, cm.exception.status), ("invalid_status", 400))
        self.assertTrue(service.delete_item(created["id"]))
        self.assertFalse(service.delete_item(created["id"]))
        listing = service.list_items(status="all", limit=1)
        self.assertEqual(listing["counts"], {"new": 1, "seen": 0, "done": 0})
        self.assertEqual(len(listing["items"]), 1)

    def test_create_validation_codes(self) -> None:
        cases = [
            (dict(title=""), "invalid_value"),
            (dict(title="x" * 301), "invalid_value"),
            (dict(title=7), "invalid_value"),
            (dict(title="t", body="b" * 4001), "invalid_value"),
            (dict(title="t", kind="Bad Kind"), "invalid_value"),
            (dict(title="t", source="UPPER"), "invalid_value"),
            (dict(title="t", project_id="a/b"), "invalid_project_id"),
            (dict(title="t", link="nope"), "invalid_link"),
            (dict(title="t", link={"path": "../etc"}), "invalid_link"),
            (dict(title="t", link={"path": "/abs"}), "invalid_link"),
            (dict(title="t", link={"view": "Bad View"}), "invalid_link"),
        ]
        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(store.InboxError) as cm:
                    service.create_item(**kwargs)
                self.assertEqual((cm.exception.code, cm.exception.status), (code, 400))
        self.assertFalse(self.path().exists())

    def test_ids_are_unique_and_collisions_retry(self) -> None:
        self.write({"items": [item(0xDEADBEEF)]})
        taken = uuid.UUID("deadbeef" + "0" * 24)
        fresh = uuid.UUID("cafebabe" + "0" * 24)
        with patch.object(uuid, "uuid4", side_effect=[taken, fresh]):
            self.assertEqual(service.create_item("x")["id"], "cafebabe")
        doc = store.normalize_document({})
        with patch.object(uuid, "uuid4", side_effect=[taken, taken, fresh]):
            store.upsert_many(doc, [store.build_item(title="a", key="k:a"), store.build_item(title="b", key="k:b")])
        self.assertEqual(sorted(it["id"] for it in doc["items"]), ["cafebabe", "deadbeef"])
        with patch.object(uuid, "uuid4", return_value=taken):
            with self.assertRaises(store.InboxError) as cm:
                store.upsert_many(doc, [store.build_item(title="c", key="k:c")])
        self.assertEqual(cm.exception.status, 500)

    # ── retention ───────────────────────────────────────────────────────────

    def test_retention_prunes_ttl_first_then_caps(self) -> None:
        boundary = NOW - timedelta(days=store.DONE_TTL_DAYS)
        items = [
            item(1, status="done", ts=iso(boundary)),                        # exactly at the TTL: kept
            item(2, status="done", ts=iso(boundary - timedelta(seconds=1))),  # older: pruned
            item(3, status="done", ts=iso(NOW)),
            item(4, status="done", ts=iso(NOW)),
        ] + [item(100 + i, ts=iso(NOW - timedelta(seconds=i))) for i in range(store.MAX_ITEMS + 1)]
        kept, pruned = store.apply_retention(items, NOW)
        self.assertEqual(len(kept), store.MAX_ITEMS)
        self.assertEqual(pruned, 5)
        ids = {it["id"] for it in kept}
        self.assertFalse(any(it["status"] == "done" for it in kept))
        self.assertNotIn(f"{100 + store.MAX_ITEMS:08x}", ids)   # the oldest open item went last
        self.assertIn(f"{100 + store.MAX_ITEMS - 1:08x}", ids)
        # untouched when within the cap
        kept, pruned = store.apply_retention([item(1, status="done", ts=iso(boundary))], NOW)
        self.assertEqual((len(kept), pruned), (1, 0))

    def test_write_applies_retention_and_logs_it(self) -> None:
        self.write({"items": [item(1, status="done", ts=iso(NOW - timedelta(days=40)))]})
        with self.assertLogs(store.logger, level="INFO") as logs:
            service.create_item("fresh")
        self.assertTrue(any("pruned 1" in line for line in logs.output))
        self.assertEqual([it["title"] for it in self.read()["items"]], ["fresh"])

    # ── locking and malformed files ─────────────────────────────────────────

    def test_writes_go_through_locked(self) -> None:
        entered: list[Path] = []

        @contextmanager
        def fake_locked(path):
            entered.append(path)
            yield

        with patch.object(store, "locked", fake_locked):
            created = service.create_item("a")
            self.assertEqual(len(entered), 1)
            service.update_item(created["id"], "done")
            self.assertEqual(len(entered), 2)
            service.delete_item(created["id"])
            self.assertEqual(len(entered), 3)
            self.write_todos("proj", [{"id": "t1", "content": "c", "status": "blocked"}])
            service.refresh(force=True)
            self.assertEqual(len(entered), 4)
            service.update_many([created["id"], "ffffffff", "00000001"], "done")
            self.assertEqual(len(entered), 5, "a batch is one locked read-modify-write")
        self.assertTrue(all(Path(p).resolve() == self.path().resolve() for p in entered))

    def test_malformed_file_is_never_overwritten(self) -> None:
        self.path().parent.mkdir(parents=True)
        self.path().write_text("{not json", encoding="utf-8")
        self.assertEqual(service.list_items()["items"], [])
        with self.assertRaises(store.InboxError) as cm:
            service.create_item("x")
        self.assertEqual((cm.exception.code, cm.exception.status), ("scope_unavailable", 500))
        # the router forwards the message to the browser: name the file, never its path
        self.assertIn("inbox.json", cm.exception.message)
        self.assertNotIn(str(self.path()), cm.exception.message)
        self.assertNotIn(str(self.root), cm.exception.message)
        self.assertEqual(self.path().read_text(encoding="utf-8"), "{not json")

    # ── feeders ─────────────────────────────────────────────────────────────

    def test_timeline_bootstrap_window_then_cursor(self) -> None:
        now = datetime.now(timezone.utc)
        old = {"ts": iso(now - timedelta(hours=30)), "type": "session.started", "session_id": "s-old",
               "runtime": "r", "project_id": "alpha"}
        recent = {"ts": iso(now - timedelta(hours=2)), "type": "session.started", "session_id": "s-new",
                  "runtime": "r", "project_id": "alpha"}
        todo = {"ts": (now - timedelta(hours=1)).isoformat(), "type": "todo.added", "session_id": "s-new",
                "runtime": "r", "project_id": "alpha", "todo": {"id": "t1", "content": "line one\nline two"}}
        untagged = {"ts": iso(now - timedelta(minutes=30)), "type": "session.started", "session_id": "s-x"}
        edited = {"ts": iso(now - timedelta(minutes=10)), "type": "file.edited", "path": "a.md",
                  "session_id": "s-new", "project_id": "alpha"}
        self.write_timeline([old, recent, todo, untagged, edited])
        self.assertTrue(service.refresh(force=True))
        doc = self.read()
        self.assertEqual({it["key"] for it in doc["items"]},
                         {"timeline:session.started:s-new", "timeline:todo.added:s-new:t1"})
        # cursor: newest fetched event of an enabled type, even one skipped for lacking project_id
        self.assertEqual(doc["cursors"]["timeline"], untagged["ts"])
        started = self.by_key("timeline:session.started:s-new")
        self.assertEqual((started["title"], started["link"], started["project_id"]),
                         ("Session started in alpha (r)", {"view": "sessions"}, "alpha"))
        self.assertEqual(self.by_key("timeline:todo.added:s-new:t1")["title"], "Todo added in alpha: line one line two")
        # cursor rule: an event older than the cursor is ignored, a newer one lands
        self.write_timeline([
            {"ts": iso(now - timedelta(hours=5)), "type": "session.started", "session_id": "s-late",
             "runtime": "r", "project_id": "alpha"},
            {"ts": iso(now), "type": "session.started", "session_id": "s-now", "runtime": "r", "project_id": "beta"},
        ], append=True)
        self.assertTrue(service.refresh(force=True))
        keys = {it["key"] for it in self.read()["items"]}
        self.assertIn("timeline:session.started:s-now", keys)
        self.assertNotIn("timeline:session.started:s-late", keys)
        self.assertEqual(self.read()["cursors"]["timeline"], iso(now))
        self.assertFalse(service.refresh(force=True))

    def test_todo_items_dedupe_by_key_keep_status_and_auto_close(self) -> None:
        self.write_todos("proj", [{"id": "t1", "content": "fix it", "status": "blocked", "description": "why"}])
        service.refresh(force=True)
        it = self.by_key("todo.blocked:proj:t1")
        self.assertEqual((it["title"], it["body"], it["kind"], it["link"]),
                         ("Todo blocked in proj: fix it", "why", "todo.blocked", {"view": "projects", "project": "proj"}))
        service.update_item(it["id"], "seen")
        self.write_todos("proj", [{"id": "t1", "content": "fix it now", "status": "blocked"}])
        service.refresh(force=True)
        again = self.by_key("todo.blocked:proj:t1")
        self.assertEqual((again["id"], again["title"], again["status"], again["ts"]), (it["id"], "Todo blocked in proj: fix it now", "seen", it["ts"]))
        self.write_todos("proj", [{"id": "t1", "content": "fix it now", "status": "pending"}])
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:proj:t1")["status"], "done")
        # a project that vanishes closes its items too; other sources are untouched
        self.write_todos("gone", [{"id": "t9", "content": "x", "status": "blocked"}])
        note = service.create_item("api note")
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:gone:t9")["status"], "new")
        shutil.rmtree(self.root / "gone")
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:gone:t9")["status"], "done")
        self.assertEqual(next(i for i in self.read()["items"] if i["id"] == note["id"])["status"], "new")

    def test_todo_ids_colliding_across_sessions_keep_the_watched_one_visible(self) -> None:
        # Runtimes number todos per session ("1", "2", ...), so one project can
        # hold the same id in several sessions while the key carries only
        # <pid>:<todo_id>. A completed "1" in a later session must not hide a
        # blocked "1" in an earlier one, whatever the order in the file.
        blocked = {"id": "1", "content": "waiting on review", "status": "blocked"}
        completed = {"id": "1", "content": "unrelated", "status": "completed"}
        self.write_todo_sessions("proj", {"a": [blocked], "b": [completed]})
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:proj:1")["status"], "new")
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:proj:1")["status"], "new",
                         "an unwatched twin in a later session must not close the item")
        self.write_todo_sessions("proj", {"a": [completed], "b": [blocked]})
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:proj:1")["status"], "new")
        self.write_todo_sessions("proj", {"a": [completed], "b": [dict(completed)]})
        service.refresh(force=True)
        self.assertEqual(self.by_key("todo.blocked:proj:1")["status"], "done")

    def test_auto_close_tracks_todo_keys_never_keyless_items(self) -> None:
        # an agent may label its own POST with source "todos"; without a
        # "todo." key it is not the feeder's to close (spec 2b)
        posted = service.create_item("agent asks something", source="todos")
        self.assertIsNone(posted.get("key"))
        self.write_todos("proj", [{"id": "t1", "content": "c", "status": "blocked"}])
        service.refresh(force=True)
        doc = self.read()
        doc["items"].append({**item(7, status="seen"), "source": "api", "key": "todo.blocked:proj:gone"})
        self.write(doc)
        service.refresh(force=True)
        status_of = {it["id"]: it["status"] for it in self.read()["items"]}
        self.assertEqual(status_of[posted["id"]], "new", "keyless items are never auto-closed")
        self.assertEqual(self.by_key("todo.blocked:proj:t1")["status"], "new")
        # a "todo." key is tracked whatever its source field says
        self.assertEqual(self.by_key("todo.blocked:proj:gone")["status"], "done")
        with self.assertRaises(ValueError):
            store.close_missing(store.normalize_document({}), "", frozenset())

    def test_cursors_only_move_forward(self) -> None:
        # two refreshes can pass the throttle together; the one that read the
        # older timeline must not drag the cursor back and re-ingest what the
        # user deleted meanwhile
        self.write({"cursors": {"timeline": "2026-09-10T12:00:00Z"}, "items": []})
        older = feeders.FeedResult([], "2026-09-10T11:00:00Z", None)
        with patch.object(feeders, "timeline", Mock(return_value=older)):
            self.assertFalse(service.refresh(force=True))
        self.assertEqual(self.read()["cursors"]["timeline"], "2026-09-10T12:00:00Z")
        same_instant = feeders.FeedResult([], "2026-09-10T12:00:00+00:00", None)   # other spelling: no churn
        with patch.object(feeders, "timeline", Mock(return_value=same_instant)):
            self.assertFalse(service.refresh(force=True))
        self.assertEqual(self.read()["cursors"]["timeline"], "2026-09-10T12:00:00Z")
        newer = feeders.FeedResult([], "2026-09-10T13:00:00Z", None)
        with patch.object(feeders, "timeline", Mock(return_value=newer)):
            self.assertTrue(service.refresh(force=True))
        self.assertEqual(self.read()["cursors"]["timeline"], "2026-09-10T13:00:00Z")
        # a hand-edited, unparsable cursor is replaced rather than kept forever
        doc = self.read()
        doc["cursors"]["timeline"] = "garbage"
        self.write(doc)
        with patch.object(feeders, "timeline", Mock(return_value=older)):
            self.assertTrue(service.refresh(force=True))
        self.assertEqual(self.read()["cursors"]["timeline"], "2026-09-10T11:00:00Z")

    def test_sharing_cursor_compares_parsed_timestamps(self) -> None:
        repo = "github.com/acme/tp"
        snap = {"repos": {repo: {"project": "tp"}}, "recent": [
            {"at": "2026-09-10T10:00:00.123456+00:00", "repo": repo, "kind": "fetched", "detail": "2 commit(s)"},
            {"at": "2026-09-10T12:00:00.000001+00:00", "repo": repo, "kind": "cloned", "detail": "cloned into tp"},
        ]}
        self.write({"cursors": {"sharing": "2026-09-10T11:00:00Z"}, "items": []})
        with patch.object(sharing_status, "snapshot", return_value=snap):
            service.refresh(force=True)
            doc = self.read()
            self.assertEqual([it["key"] for it in doc["items"]], [f"sharing:cloned:{repo}:2026-09-10T12:00:00.000001+00:00"])
            self.assertEqual(doc["items"][0]["title"], f"Sharing: cloned for {repo}")
            self.assertEqual((doc["items"][0]["project_id"], doc["items"][0]["kind"], doc["items"][0]["body"]),
                             ("tp", "sharing.cloned", "cloned into tp"))
            self.assertEqual(doc["cursors"]["sharing"], "2026-09-10T12:00:00.000001+00:00")
            del doc["cursors"]["sharing"]   # an operator reset re-adds older entries once, no duplicates
            self.write(doc)
            service.refresh(force=True)
            keys = [it["key"] for it in self.read()["items"]]
            self.assertEqual(len(keys), 2)
            self.assertEqual(self.by_key(f"sharing:fetched:{repo}:2026-09-10T10:00:00.123456+00:00")["title"],
                             f"New commits fetched: {repo}")

    def test_disabled_sources_are_not_read(self) -> None:
        self.write({"sources": {"timeline": {"enabled": False}, "todos": {"enabled": False}}, "items": []})
        scope = Mock()
        with patch.object(feeders, "resolve_scope", return_value=scope) as rs, \
             patch.object(feeders, "list_project_ids", return_value=["p"]) as lp, \
             patch.object(sharing_status, "snapshot", return_value={"repos": {}, "recent": []}) as sn:
            service.refresh(force=True)
        rs.assert_not_called()
        scope.read_timeline.assert_not_called()
        lp.assert_not_called()
        sn.assert_called_once()

    def test_raising_feeder_does_not_stop_the_others(self) -> None:
        self.write_todos("proj", [{"id": "t1", "content": "c", "status": "blocked"}])
        snap = {"repos": {}, "recent": [{"at": "2026-09-10T10:00:00+00:00", "repo": "r", "kind": "error", "detail": "boom"}]}
        with patch.object(feeders, "timeline", side_effect=RuntimeError("timeline exploded")), \
             patch.object(sharing_status, "snapshot", return_value=snap), \
             self.assertLogs(service.logger, level="WARNING") as logs:
            self.assertTrue(service.refresh(force=True))
        self.assertTrue(any("inbox feeder timeline failed" in line for line in logs.output))
        doc = self.read()
        self.assertEqual({it["key"] for it in doc["items"]}, {"todo.blocked:proj:t1", "sharing:error:r:2026-09-10T10:00:00+00:00"})
        self.assertNotIn("timeline", doc["cursors"])
        self.assertIsNotNone(service._last_refresh_monotonic, "a failed run still arms the throttle")

    def test_a_failing_feeder_still_arms_the_throttle(self) -> None:
        # the badge polls every 60 s and the tab every 30 s; a feeder that
        # keeps raising must not turn each of those reads into a full ingest
        boom = Mock(side_effect=RuntimeError("timeline exploded"))
        quiet = Mock(return_value=feeders.FeedResult([], None, None))
        with patch.object(feeders, "timeline", boom), patch.object(feeders, "sharing", quiet), \
             self.assertLogs(service.logger, level="WARNING"):
            self.assertFalse(service.refresh(force=True))
            self.assertFalse(service.refresh())
            self.assertEqual(service.list_items()["items"], [])
        self.assertEqual((boom.call_count, quiet.call_count), (1, 1),
                         "calls within INGEST_MIN_INTERVAL_S must not re-run the feeders")
        self.assertIsNotNone(service._last_refresh_monotonic)

    def test_refresh_is_throttled_and_list_swallows_refresh_errors(self) -> None:
        calls = Mock(return_value=feeders.FeedResult([], None, None))
        with patch.object(feeders, "sharing", calls):
            service.refresh(force=True)
            self.assertFalse(service.refresh())
            self.assertEqual(calls.call_count, 1)
            with patch.object(service, "INGEST_MIN_INTERVAL_S", 0):
                service.refresh()
            self.assertEqual(calls.call_count, 2)
        with patch.object(store, "modify", side_effect=OSError("disk")), \
             patch.object(feeders, "sharing", Mock(return_value=feeders.FeedResult([store.build_item(title="x", key="k")], None, None))):
            service._reset_throttle()
            self.assertEqual(service.list_items()["items"], [])
            self.assertIsNotNone(service._last_refresh_monotonic, "a run that reached the feeders is stamped even when the write fails")

    # ── auto_closed: a feeder's close reopens, a person's done sticks ───────

    @staticmethod
    def _blocked(title: str, ts: str | None = None) -> feeders.FeedResult:
        it = store.build_item(title=title, key="todo.blocked:proj:t1", source="todos", kind="todo.blocked", ts=ts)
        return feeders.FeedResult([it], None, feeders.Watched("todo.", frozenset({"todo.blocked:proj:t1"})))

    _UNBLOCKED = feeders.FeedResult([], None, feeders.Watched("todo.", frozenset()))

    def test_close_missing_flags_the_close_and_upsert_reopens_it(self) -> None:
        doc = store.normalize_document({})
        reported = store.build_item(title="Issue #1 in p: t", key="issue:p:1", source="issues", kind="issue.open")
        self.assertTrue(store.upsert_many(doc, [reported]))
        self.assertTrue(store.close_missing(doc, "issue:", frozenset()))
        it = doc["items"][0]
        self.assertEqual((it["status"], it["auto_closed"]), ("done", True))
        self.assertFalse(store.close_missing(doc, "issue:", frozenset()), "already done: nothing to close again")
        # the feeder reports the key again (reopened on GitHub): back to new, flag gone
        self.assertTrue(store.upsert_many(doc, [dict(reported)]))
        self.assertEqual(it["status"], "new")
        self.assertNotIn("auto_closed", it)
        self.assertFalse(store.upsert_many(doc, [dict(reported)]), "a second report changes nothing")

    def test_a_reopened_item_takes_the_reported_ts_and_sorts_first(self) -> None:
        # reopened with its first ts, an old item would sort back behind
        # everything newer and fall outside the slice the UI shows
        doc = store.normalize_document({"items": [item(1, ts=iso(NOW))]})
        old = store.build_item(title="Issue #1 in p: t", key="issue:p:1", source="issues", kind="issue.open",
                               ts=iso(NOW - timedelta(days=40)))
        store.upsert_many(doc, [old])
        self.assertTrue(store.close_missing(doc, "issue:", frozenset()))
        again = dict(old, ts=iso(NOW + timedelta(hours=1)))
        self.assertTrue(store.upsert_many(doc, [again]))
        it = next(i for i in doc["items"] if i.get("key") == "issue:p:1")
        self.assertEqual((it["status"], it["ts"]), ("new", iso(NOW + timedelta(hours=1))))
        self.assertEqual(store.sort_newest_first(doc["items"])[0]["key"], "issue:p:1")
        # a still-open item is never re-dated: a refreshed title keeps its place
        self.assertTrue(store.upsert_many(doc, [dict(again, title="renamed", ts=iso(NOW + timedelta(days=2)))]))
        self.assertEqual(it["ts"], iso(NOW + timedelta(hours=1)))
        # a report without a ts dates the reopen now
        self.assertTrue(store.close_missing(doc, "issue:", frozenset()))
        self.assertTrue(store.upsert_many(doc, [{k: v for k, v in again.items() if k != "ts"}]))
        self.assertEqual(it["status"], "new")
        self.assertGreater(store.parse_ts(it["ts"]), NOW + timedelta(hours=1))

    def test_a_persons_done_survives_the_feeder_reporting_the_key_again(self) -> None:
        reported = store.build_item(title="Issue #1 in p: t", key="issue:p:1", source="issues", kind="issue.open")
        open_issue = feeders.FeedResult([reported], None, feeders.Watched("issue:", frozenset({"issue:p:1"})))
        with patch.object(feeders, "issues", Mock(return_value=open_issue)):
            service.refresh(force=True)
            it = self.by_key("issue:p:1")
            self.assertEqual(service.update_item(it["id"], "done")["status"], "done")
            self.assertFalse(service.refresh(force=True))
        again = self.by_key("issue:p:1")
        self.assertEqual((again["id"], again["status"]), (it["id"], "done"))
        self.assertNotIn("auto_closed", again)

    def test_a_todo_blocked_again_resurfaces_after_its_auto_close(self) -> None:
        with patch.object(feeders, "todos", Mock(side_effect=[
                self._blocked("Todo blocked in proj: a", ts=iso(NOW)), self._UNBLOCKED,
                self._blocked("Todo blocked in proj: b", ts=iso(NOW + timedelta(days=1)))])):
            service.refresh(force=True)
            first = self.by_key("todo.blocked:proj:t1")
            self.assertEqual((first["status"], first["ts"]), ("new", iso(NOW)))
            service.refresh(force=True)
            closed = self.by_key("todo.blocked:proj:t1")
            self.assertEqual((closed["status"], closed["auto_closed"]), ("done", True))
            self.assertTrue(service.refresh(force=True))
        back = self.by_key("todo.blocked:proj:t1")
        self.assertEqual((back["id"], back["status"], back["title"]), (first["id"], "new", "Todo blocked in proj: b"))
        self.assertNotIn("auto_closed", back)
        # re-dated to the report that reopened it, so it surfaces at the top
        self.assertEqual(back["ts"], iso(NOW + timedelta(days=1)))

    def test_update_item_and_update_many_clear_auto_closed(self) -> None:
        with patch.object(feeders, "todos", Mock(side_effect=[self._blocked("x"), self._UNBLOCKED])):
            service.refresh(force=True)
            service.refresh(force=True)
        it = self.by_key("todo.blocked:proj:t1")
        self.assertTrue(it["auto_closed"])
        # confirming "done" changes no status but drops the flag, and that is written
        updated = service.update_item(it["id"], "done")
        self.assertEqual(updated["status"], "done")
        self.assertNotIn("auto_closed", updated)
        self.assertNotIn("auto_closed", self.by_key("todo.blocked:proj:t1"))
        # the same through the batch route
        doc = self.read()
        doc["items"][0]["auto_closed"] = True
        self.write(doc)
        self.assertTrue(self.by_key("todo.blocked:proj:t1")["auto_closed"])
        self.assertEqual(service.update_many([it["id"]], "done"), {"updated": 0, "missing": []})
        self.assertNotIn("auto_closed", self.by_key("todo.blocked:proj:t1"))
        # and a reopen by hand leaves nothing behind for the feeder to undo
        doc = self.read()
        doc["items"][0]["auto_closed"] = True
        self.write(doc)
        self.assertEqual(service.update_item(it["id"], "new")["status"], "new")
        self.assertNotIn("auto_closed", self.by_key("todo.blocked:proj:t1"))

    def test_auto_closed_survives_only_as_the_literal_true(self) -> None:
        self.write({"items": [{**item(1, status="done"), "auto_closed": True},
                              {**item(2, status="done"), "auto_closed": "yes"},
                              {**item(3, status="done"), "auto_closed": 1},
                              {**item(4), "auto_closed": False}]})
        flags = {it["id"]: it.get("auto_closed", "absent") for it in service.list_items(status="all")["items"]}
        self.assertEqual(flags, {"00000001": True, "00000002": "absent", "00000003": "absent", "00000004": "absent"})

    # ── update_many ─────────────────────────────────────────────────────────

    def test_update_many_counts_changes_reports_missing_in_order_and_is_idempotent(self) -> None:
        a, b, c = (service.create_item(t) for t in ("a", "b", "c"))
        service.update_item(c["id"], "seen")
        res = service.update_many([a["id"], "ffffffff", b["id"], "nope", c["id"], a["id"], None], "seen")
        self.assertEqual(res, {"updated": 2, "missing": ["ffffffff", "nope", None]})
        self.assertEqual({it["id"]: it["status"] for it in self.read()["items"]},
                         {a["id"]: "seen", b["id"]: "seen", c["id"]: "seen"})
        with patch.object(store, "write_json_atomic") as write:
            self.assertEqual(service.update_many([a["id"], "ffffffff", b["id"]], "seen"),
                             {"updated": 0, "missing": ["ffffffff"]})
            write.assert_not_called()
        self.assertEqual(service.update_many([a["id"], b["id"], c["id"]], "done")["updated"], 3)
        self.assertEqual(service.list_items()["counts"], {"new": 0, "seen": 0, "done": 3})

    def test_update_many_validates_before_touching_the_file(self) -> None:
        for ids in ([], ["deadbeef"] * (service.UPDATE_MANY_MAX + 1), "deadbeef", None):
            with self.subTest(ids=ids):
                with self.assertRaises(store.InboxError) as cm:
                    service.update_many(ids, "seen")
                self.assertEqual((cm.exception.code, cm.exception.status), ("invalid_value", 400))
        with self.assertRaises(store.InboxError) as cm:
            service.update_many(["deadbeef"], "archived")
        self.assertEqual((cm.exception.code, cm.exception.status), ("invalid_status", 400))
        self.assertFalse(self.path().exists())
        # exactly the cap, every id unknown: one call, no write
        res = service.update_many([f"{i:08x}" for i in range(service.UPDATE_MANY_MAX)], "done")
        self.assertEqual((res["updated"], len(res["missing"])), (0, service.UPDATE_MANY_MAX))
        self.assertFalse(self.path().exists(), "unknown ids change nothing, so the file is never created")


if __name__ == "__main__":
    unittest.main()


class InboxLocationTests(unittest.TestCase):
    """The file is machine-local state under the Quirq root, and a file left
    at the earlier ``<XO root>/.xo/inbox.json`` location is moved once."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root),
                                            "QUIRQ_STATE_ROOT": str(self.root / ".quirq")})
        self._env.start()
        service._reset_throttle()

    def tearDown(self) -> None:
        service._reset_throttle()
        self._env.stop()
        self._tmp.cleanup()

    def test_path_is_under_the_quirq_root_not_the_xo_root(self) -> None:
        # the XO root is resolved (macOS keeps /var as a symlink to /private/var), the Quirq root is not
        self.assertEqual(store.inbox_path(), self.root / ".quirq" / "inbox.json")
        self.assertEqual(store._legacy_path().resolve(), (self.root / ".xo" / "inbox.json").resolve())

    def test_a_legacy_file_is_moved_once_on_first_use(self) -> None:
        legacy = self.root / ".xo" / "inbox.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"schema": 1, "items": [
            {"id": "aaaaaaaa", "ts": "2026-09-10T00:00:00Z", "title": "carried over", "status": "seen"}]}))
        doc, ok = store.load_document()
        self.assertTrue(ok)
        self.assertEqual([it["id"] for it in doc["items"]], ["aaaaaaaa"])
        self.assertTrue(store.inbox_path().is_file(), "moved to the Quirq root")
        self.assertFalse(legacy.exists(), "nothing is left behind in .xo")

    def test_a_legacy_file_never_overwrites_a_newer_one(self) -> None:
        new = store.inbox_path(); new.parent.mkdir(parents=True)
        new.write_text(json.dumps({"schema": 1, "items": [
            {"id": "bbbbbbbb", "ts": "2026-09-11T00:00:00Z", "title": "current", "status": "new"}]}))
        legacy = self.root / ".xo" / "inbox.json"; legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"schema": 1, "items": []}))
        doc, _ = store.load_document()
        self.assertEqual([it["id"] for it in doc["items"]], ["bbbbbbbb"])
        self.assertTrue(legacy.exists(), "left alone: the new file wins and the old one is not touched")

    def test_a_read_adopts_the_legacy_file_under_the_lock_then_reads_lock_free(self) -> None:
        # shutil.move copies then unlinks across filesystems, so an unlocked
        # reader could copy beside a locked writer and hand it a half-copied file
        legacy = self.root / ".xo" / "inbox.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"schema": 1, "items": []}))
        entered: list[Path] = []
        held: list[bool] = []
        moved_under_lock: list[bool] = []

        @contextmanager
        def fake_locked(path):
            entered.append(path)
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        real_move = shutil.move

        def fake_move(src, dst):
            moved_under_lock.append(bool(held))
            return real_move(src, dst)

        with patch.object(store, "locked", fake_locked), patch.object(shutil, "move", fake_move):
            _doc, ok = store.load_document()
            self.assertTrue(ok)
            self.assertEqual(moved_under_lock, [True], "the move happens inside locked(), like a write's")
            self.assertEqual([Path(p) for p in entered], [store.inbox_path()])
            store.load_document()
            self.assertEqual(len(entered), 1, "once adopted, a read takes no lock")
        self.assertFalse(legacy.exists())
        self.assertTrue(store.inbox_path().is_file())

    def test_a_write_that_comes_first_after_the_upgrade_adopts_the_legacy_file(self) -> None:
        legacy = self.root / ".xo" / "inbox.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"schema": 1, "items": [
            {"id": "aaaaaaaa", "ts": "2026-09-10T00:00:00Z", "title": "carried over", "status": "seen"}]}))
        created = service.create_item("the first call is a POST")
        ids = {it["id"] for it in service.list_items(status="all")["items"]}
        self.assertEqual(ids, {"aaaaaaaa", created["id"]}, "the old items and the new one share one file")
        self.assertFalse(legacy.exists(), "moved, not copied: nothing is left behind in .xo")
        self.assertTrue(store.inbox_path().is_file())
