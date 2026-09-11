"""The two disk-reading inbox feeders (issues, connections) and the item
``url`` field.

Hermetic: XO_PROJECTS_ROOT and QUIRQ_STATE_ROOT point into a temp dir, so
the issue mirrors live under <tmp>/.quirq/projects/<pid>/github/ and the
connection folders under <tmp>/.quirq/connections/<toolkit>/; the flock
sentinel resolves through QUIRQ_STATE_ROOT at call time. Nothing here
touches the network."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.connections import store as connections_store
from services.inbox import feeders, service, store
from services.cowork_agent.project_sharing import status as sharing_status
from services.cowork_agent.visualizer import github_mirror

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta) -> str:
    return iso(NOW - timedelta(**delta))


def row(number: int, title: str = "t", *, state: str = "open", updated_at: str | None = None, **extra) -> dict:
    return {"node_id": f"I_{number}", "number": number, "title": title, "state": state, "state_reason": None,
            "url": f"https://github.com/o/r/issues/{number}", "updated_at": updated_at or ago(hours=1), **extra}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects),
                                            "QUIRQ_STATE_ROOT": str(self.root / ".quirq")})
        self._env.start()
        service._reset_throttle()
        sharing_status.reset()

    def tearDown(self) -> None:
        service._reset_throttle()
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    def doc(self, **top) -> dict:
        return store.normalize_document({"schema": 1, "items": [], **top})

    # issues fixtures
    def project(self, pid: str) -> None:
        (self.projects / pid).mkdir(parents=True, exist_ok=True)

    def mirror(self, pid: str, rows: list[dict] | None = None, *, raw: str | None = None) -> Path:
        self.project(pid)
        path = github_mirror.mirror_path(pid, create=True)
        assert path is not None
        if raw is None:
            raw = json.dumps({"$schema": "xo/github-issues.schema.json", "schema": 1, "repo": "o/r",
                              "fetched_at": iso(NOW),
                              "issues": {(r["node_id"] if isinstance(r, dict) else f"junk{i}"): r
                                         for i, r in enumerate(rows or [])}})
        path.write_text(raw, encoding="utf-8")
        return path

    # connections fixtures
    def connection(self, toolkit: str, events: list[dict], *, configured: bool = True) -> Path:
        folder = self.root / ".quirq" / "connections" / toolkit
        folder.mkdir(parents=True, exist_ok=True)
        if configured:
            (folder / "config.json").write_text(json.dumps({
                "schema": 1, "toolkit": toolkit, "enabled": True, "interval_s": 900, "collectors": ["unread"]}),
                encoding="utf-8")
        # the poller appends chronologically (oldest first); the reader returns newest-first
        (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return folder

    def event(self, key: str, ts: str, *, toolkit: str = "gmail", type_: str = "unread", **extra) -> dict:
        return {"ts": ts, "type": type_, "key": key, "title": f"mail {key}", "body": "snippet",
                "url": f"https://mail.google.com/mail/u/0/#all/{key}", "toolkit": toolkit, **extra}

    def inbox(self) -> dict:
        return json.loads((self.root / ".quirq" / "inbox.json").read_text(encoding="utf-8"))

    def write_inbox(self, doc: dict) -> None:
        path = self.root / ".quirq" / "inbox.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")


class IssuesFeederTests(_Base):
    def test_bootstrap_window_is_seven_days_and_items_carry_the_row(self) -> None:
        self.mirror("proj", [
            row(1, "Login  breaks\non mobile", labels=["bug", "ui"],
                assignees=[{"login": "alice", "avatar_url": None}, {"login": ""}, "junk"]),
            row(2, "old but open", updated_at=ago(days=10)),
            row(3, "closed recently", state="closed", updated_at=ago(minutes=5)),
        ])
        res = feeders.issues(self.doc())
        self.assertEqual([it["key"] for it in res.items], ["issue:proj:1"])
        it = res.items[0]
        self.assertEqual(it["kind"], "issue.open")
        self.assertEqual(it["source"], "issues")
        self.assertEqual(it["title"], "Issue #1 in proj: Login breaks on mobile")
        self.assertEqual(it["body"], "labels: bug, ui\nassignees: alice")
        self.assertEqual(it["url"], "https://github.com/o/r/issues/1")
        self.assertEqual(it["project_id"], "proj")
        self.assertEqual(it["link"], {"view": "projects", "project": "proj"})
        self.assertEqual(it["ts"], ago(hours=1))
        # the cursor is the newest updated_at across every readable row, kept or not
        self.assertEqual(res.cursor, ago(minutes=5))
        # every row in a watched state is watched, inside the window or not
        self.assertEqual(res.watched, feeders.Watched("issue:", frozenset({"issue:proj:1", "issue:proj:2"})))

    def test_cursor_advances_and_states_config_is_honoured(self) -> None:
        self.mirror("proj", [row(1, updated_at=ago(hours=1)), row(4, updated_at=ago(minutes=10)),
                             row(5, "done", state="closed", updated_at=ago(minutes=1))])
        res = feeders.issues(self.doc(cursors={"issues": ago(minutes=30)}))
        self.assertEqual([it["key"] for it in res.items], ["issue:proj:4"])
        self.assertEqual(res.cursor, ago(minutes=1))
        res = feeders.issues(self.doc(sources={"issues": {"states": ["closed"]}}))
        self.assertEqual([(it["key"], it["kind"]) for it in res.items], [("issue:proj:5", "issue.closed")])
        self.assertEqual(res.watched.keys, frozenset({"issue:proj:5"}))

    def test_title_is_truncated_and_bad_rows_or_urls_are_skipped(self) -> None:
        self.mirror("proj", [
            row(1, "x" * 500),
            row(2, url="javascript:alert(1)"),
            {**row(3), "number": "3"},                 # number must be an int
            {**row(4), "number": True},                # bool is not a number
            {**row(5), "title": None},
            row(6, updated_at="not a date"),
            "junk",
        ])
        res = feeders.issues(self.doc())
        by_key = {it["key"]: it for it in res.items}
        self.assertEqual(set(by_key), {"issue:proj:1", "issue:proj:2"})
        self.assertEqual(len(by_key["issue:proj:1"]["title"]), len("Issue #1 in proj: ") + 120)
        self.assertIsNone(by_key["issue:proj:2"]["url"])
        # row 6 is open: watched even though its updated_at cannot be emitted or pin the cursor
        self.assertEqual(res.watched.keys, frozenset({"issue:proj:1", "issue:proj:2", "issue:proj:6"}))

    def test_unparsable_updated_at_keeps_an_open_issue_watched(self) -> None:
        self.mirror("proj", [row(6, updated_at=ago(hours=1))])
        service.refresh(force=True)
        self.assertEqual({it["key"]: it["status"] for it in self.inbox()["items"]}, {"issue:proj:6": "new"})
        # the row is still open but its timestamp broke: no item, no cursor, still watched
        self.mirror("proj", [row(6, updated_at="not a date")])
        res = feeders.issues(self.doc())
        self.assertEqual((res.items, res.cursor), ([], None))
        self.assertEqual(res.watched, feeders.Watched("issue:", frozenset({"issue:proj:6"})))
        service.refresh(force=True)
        self.assertEqual({it["key"]: it["status"] for it in self.inbox()["items"]}, {"issue:proj:6": "new"},
                         "an open issue with a broken updated_at must not be closed")

    def test_future_updated_at_never_pins_the_cursor(self) -> None:
        self.mirror("proj", [row(1, updated_at=ago(hours=1)), row(2, updated_at=iso(NOW + timedelta(days=30)))])
        res = feeders.issues(self.doc())
        self.assertEqual({it["key"] for it in res.items}, {"issue:proj:1", "issue:proj:2"})
        self.assertEqual(res.cursor, ago(hours=1))

    def test_auto_close_only_when_every_mirror_is_readable(self) -> None:
        self.project("nofile")                                  # no mirror file at all: readable-empty
        self.mirror("good", [row(1)])
        bad = self.mirror("bad", raw="not json")
        res = feeders.issues(self.doc())
        self.assertEqual([it["key"] for it in res.items], ["issue:good:1"])
        self.assertIsNone(res.watched, "a mirror that exists but cannot be read must not close real issues")
        bad.unlink()
        res = feeders.issues(self.doc())
        self.assertEqual(res.watched, feeders.Watched("issue:", frozenset({"issue:good:1"})))
        # the mirror's own schema check counts as unreadable too
        self.mirror("bad", raw=json.dumps({"schema": 2, "issues": {}}))
        self.assertIsNone(feeders.issues(self.doc()).watched)

    def test_refresh_closes_issues_that_left_the_watched_states(self) -> None:
        self.mirror("proj", [row(1), row(2)])
        service.refresh(force=True)
        keys = {it["key"]: it["status"] for it in self.inbox()["items"]}
        self.assertEqual(keys, {"issue:proj:1": "new", "issue:proj:2": "new"})
        self.assertEqual(self.inbox()["cursors"]["issues"], ago(hours=1))
        # #2 closes, and at the same time another project's mirror is broken: nothing closes this run
        self.mirror("proj", [row(1), row(2, state="closed", updated_at=ago(minutes=1))])
        broken = self.mirror("other", raw="{")
        service.refresh(force=True)
        self.assertEqual({it["key"]: it["status"] for it in self.inbox()["items"]},
                         {"issue:proj:1": "new", "issue:proj:2": "new"})
        broken.unlink()
        service.refresh(force=True)
        self.assertEqual({it["key"]: it["status"] for it in self.inbox()["items"]},
                         {"issue:proj:1": "new", "issue:proj:2": "done"})

    def test_disabled_source_reads_no_mirror(self) -> None:
        self.mirror("proj", [row(1)])
        with patch.object(github_mirror, "mirror_path") as mp, patch.object(github_mirror, "read_mirror") as rm:
            service.refresh(force=True)
            mp.assert_called()
            rm.assert_called()
            mp.reset_mock()
            rm.reset_mock()
            self.write_inbox({"schema": 1, "items": [], "sources": {"issues": {"enabled": False}}})
            service.refresh(force=True)
            mp.assert_not_called()
            rm.assert_not_called()


class ConnectionsFeederTests(_Base):
    def test_bootstrap_24h_then_key_kind_and_chronological_order(self) -> None:
        self.connection("gmail", [self.event("old", ago(days=3)), self.event("m1", ago(hours=1))])
        self.connection("notion", [self.event("p1", ago(hours=2), toolkit="notion", type_="recent_pages",
                                              url="https://www.notion.so/p1")])
        self.connection("figma", [self.event("f1", ago(minutes=1), toolkit="figma")], configured=False)
        res = feeders.connections(self.doc())
        self.assertEqual([it["key"] for it in res.items],
                         ["connection:notion:recent_pages:p1", "connection:gmail:unread:m1"])
        gm = res.items[1]
        self.assertEqual((gm["kind"], gm["source"], gm["link"]), ("gmail.unread", "connections", {"view": "connectors"}))
        self.assertEqual((gm["title"], gm["body"], gm["ts"]), ("mail m1", "snippet", ago(hours=1)))
        self.assertEqual(gm["url"], "https://mail.google.com/mail/u/0/#all/m1")
        self.assertIsNone(gm["project_id"])
        self.assertEqual(res.items[0]["kind"], "notion.recent_pages")
        self.assertEqual(res.cursor, ago(hours=1), "the newest ts across every configured toolkit")
        self.assertIsNone(res.watched)

    def test_one_cursor_covers_every_toolkit(self) -> None:
        self.connection("gmail", [self.event("m1", ago(hours=1)), self.event("m2", ago(minutes=5))])
        self.connection("notion", [self.event("p1", ago(minutes=30), toolkit="notion", type_="recent_pages")])
        res = feeders.connections(self.doc(cursors={"connections": ago(minutes=45)}))
        self.assertEqual([it["key"] for it in res.items],
                         ["connection:notion:recent_pages:p1", "connection:gmail:unread:m2"])
        self.assertEqual(res.cursor, ago(minutes=5))

    def test_a_future_ts_is_emitted_but_never_pins_the_cursor(self) -> None:
        # a calendar collector stamps upcoming events with their start, days ahead
        self.connection("gmail", [self.event("m1", ago(hours=1))])
        self.connection("googlecalendar", [self.event("ev1", iso(NOW + timedelta(days=3)),
                                                      toolkit="googlecalendar", type_="upcoming")])
        res = feeders.connections(self.doc())
        self.assertEqual({it["key"] for it in res.items},
                         {"connection:gmail:unread:m1", "connection:googlecalendar:upcoming:ev1"})
        self.assertEqual(res.cursor, ago(hours=1), "an event three days ahead must not become the floor")
        # mail arriving after that poll still surfaces on the next run
        self.connection("gmail", [self.event("m1", ago(hours=1)), self.event("m2", ago(minutes=5))])
        res = feeders.connections(self.doc(cursors={"connections": res.cursor}))
        self.assertIn("connection:gmail:unread:m2", [it["key"] for it in res.items])
        self.assertEqual(res.cursor, ago(minutes=5))
        # clock skew within the slack still pins the cursor
        skew = iso(NOW + timedelta(minutes=2))
        self.connection("gmail", [self.event("m3", skew)])
        self.assertEqual(feeders.connections(self.doc()).cursor, skew)

    def test_hand_edited_lines_are_tolerated(self) -> None:
        self.connection("gmail", [
            {"ts": ago(hours=1), "type": "unread"},                                   # no key: skipped
            {"ts": "garbage", "type": "unread", "key": "k0"},                         # bad ts: skipped
            {"ts": ago(hours=1), "type": "", "key": "k1"},                            # empty type: skipped
            {"ts": ago(minutes=50), "type": "unread", "key": "k2", "title": "", "body": 7, "url": "ftp://x"},
            {"ts": ago(minutes=40), "type": "Unread Mail", "key": "k3", "title": ["not", "a", "string"]},
            {"ts": ago(minutes=30), "type": "unread", "key": "k4", "title": "y" * 400, "body": "b" * 5000},
            "junk",
        ])
        res = feeders.connections(self.doc())
        by_key = {it["key"]: it for it in res.items}
        self.assertEqual(set(by_key), {"connection:gmail:unread:k2", "connection:gmail:Unread Mail:k3",
                                       "connection:gmail:unread:k4"})
        k2 = by_key["connection:gmail:unread:k2"]
        self.assertEqual((k2["title"], k2["body"], k2["url"]), ("gmail unread: k2", "", None))
        k3 = by_key["connection:gmail:Unread Mail:k3"]
        self.assertEqual((k3["title"], k3["kind"]), ("gmail Unread Mail: k3", "gmail.unread-mail"))
        k4 = by_key["connection:gmail:unread:k4"]
        self.assertEqual((len(k4["title"]), len(k4["body"])), (store.TITLE_MAX, store.BODY_MAX))

    def test_refresh_ingests_and_a_disabled_source_reads_nothing(self) -> None:
        self.connection("gmail", [self.event("m1", ago(hours=1))])
        service.refresh(force=True)
        items = {it["key"]: it for it in self.inbox()["items"]}
        self.assertEqual(set(items), {"connection:gmail:unread:m1"})
        self.assertEqual(items["connection:gmail:unread:m1"]["url"], "https://mail.google.com/mail/u/0/#all/m1")
        self.assertEqual(self.inbox()["cursors"]["connections"], ago(hours=1))
        self.write_inbox({"schema": 1, "items": [], "sources": {"connections": {"enabled": False}}})
        with patch.object(connections_store, "list_configured") as lc:
            service.refresh(force=True)
        lc.assert_not_called()

    def test_a_failing_toolkit_read_does_not_stop_the_others(self) -> None:
        self.connection("gmail", [self.event("m1", ago(hours=1))])
        self.connection("notion", [self.event("p1", ago(hours=2), toolkit="notion", type_="recent_pages")])
        real = connections_store.read_events

        def flaky(toolkit, **kw):
            if toolkit == "gmail":
                raise OSError("disk")
            return real(toolkit, **kw)

        with patch.object(connections_store, "read_events", side_effect=flaky), \
             self.assertLogs(feeders.logger, level="WARNING"):
            res = feeders.connections(self.doc())
        self.assertEqual([it["key"] for it in res.items], ["connection:notion:recent_pages:p1"])


class InboxUrlFieldTests(_Base):
    def test_build_item_validates_url_strictly(self) -> None:
        self.assertEqual(store.build_item(title="t", url="https://example.com/x")["url"], "https://example.com/x")
        self.assertEqual(store.build_item(title="t", url="http://example.com")["url"], "http://example.com")
        self.assertIsNone(store.build_item(title="t")["url"])
        for bad in ("ftp://x", "javascript:alert(1)", "example.com", "", 7, "https://" + "x" * 2000):
            with self.subTest(url=bad):
                with self.assertRaises(store.InboxError) as cm:
                    store.build_item(title="t", url=bad)
                self.assertEqual(cm.exception.code, "invalid_value")

    def test_create_item_takes_an_optional_url_and_defaults_it_to_none(self) -> None:
        created = service.create_item("hello")
        self.assertIsNone(created["url"])
        created = service.create_item("hello", url="https://example.test/issues/1")
        self.assertEqual(created["url"], "https://example.test/issues/1")
        with self.assertRaises(service.InboxError) as cm:
            service.create_item("hello", url="javascript:alert(1)")
        self.assertEqual(cm.exception.code, "invalid_value")

    def test_normalize_is_lenient_and_upsert_refreshes_url(self) -> None:
        doc = store.normalize_document({"items": [
            {"id": "00000001", "ts": iso(NOW), "title": "a", "url": "javascript:x"},
            {"id": "00000002", "ts": iso(NOW), "title": "b", "url": "https://ok/1"},
            {"id": "00000003", "ts": iso(NOW), "title": "c"},
        ]})
        urls = {it["id"]: it["url"] for it in doc["items"]}
        self.assertEqual(urls, {"00000001": None, "00000002": "https://ok/1", "00000003": None})
        first = store.build_item(title="a", key="k", url="https://ok/1")
        doc = store.normalize_document({"items": []})
        self.assertTrue(store.upsert_many(doc, [first]))
        self.assertFalse(store.upsert_many(doc, [store.build_item(title="a", key="k", url="https://ok/1")]))
        self.assertTrue(store.upsert_many(doc, [store.build_item(title="a", key="k", url="https://ok/2")]))
        self.assertEqual(doc["items"][0]["url"], "https://ok/2")

    def test_defaults_feeder_names_and_schema(self) -> None:
        self.assertEqual(feeders.FEEDER_NAMES, ("timeline", "todos", "sharing", "issues", "connections"))
        self.assertEqual(store.DEFAULT_SOURCES["issues"], {"enabled": True, "states": ["open"]})
        self.assertEqual(store.DEFAULT_SOURCES["connections"], {"enabled": True})
        self.assertEqual(store.source_config({}, "issues")["states"], ["open"])
        schema = json.loads((ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "inbox.schema.json")
                            .read_text(encoding="utf-8"))
        self.assertIn("issues", schema["properties"]["sources"]["properties"])
        self.assertIn("connections", schema["properties"]["sources"]["properties"])
        self.assertIn("issues", schema["properties"]["cursors"]["properties"])
        self.assertIn("connections", schema["properties"]["cursors"]["properties"])
        url = schema["definitions"]["item"]["properties"]["url"]
        self.assertEqual((url["type"], url["maxLength"], url["pattern"]), (["string", "null"], 2000, "^https?://"))

    def test_no_dashes_or_agent_names_or_router_imports_in_the_feeders(self) -> None:
        for rel in ("services/inbox/feeders.py", "services/inbox/store.py",
                    "services/cowork_agent/visualizer/schema/inbox.schema.json"):
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIsNone(re.search("[\\u2013\\u2014]", text))
                self.assertNotRegex(text, r"openclaw|hermes|claude_code|codex|antigravity")
                self.assertNotIn("from routers", text)
                self.assertNotIn("import routers", text)


if __name__ == "__main__":
    unittest.main()
