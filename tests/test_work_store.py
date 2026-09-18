"""``services/work/store.py``: the file behind the Work, its marks and its
retention. Hermetic: QUIRQ_STATE_ROOT points into a temp dir."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.work import store

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root / "projects"),
                                            "QUIRQ_STATE_ROOT": str(self.root / ".quirq")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def path(self, page: str = "inbox") -> Path:
        return self.root / ".quirq" / "work" / page / f"{page}.json"

    def write(self, doc, page: str = "inbox") -> None:
        self.path(page).parent.mkdir(parents=True, exist_ok=True)
        self.path(page).write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")

    def read(self, page: str = "inbox") -> dict:
        return json.loads(self.path(page).read_text(encoding="utf-8"))


class NormalizeTests(_Sandbox):
    def test_defaults_fill_and_unknown_keys_survive(self) -> None:
        doc = store.normalize_document({"custom": 1, "sources": {"connections": {"attention": ["gmail.unread", 3]}}})
        self.assertEqual(doc["schema"], store.SCHEMA)
        self.assertEqual(doc["custom"], 1)
        self.assertEqual(doc["sources"]["connections"], {"enabled": True, "attention": ["gmail.unread"]})
        self.assertEqual(doc["sources"]["jobs"], {"enabled": True})
        for key in ("dismissed", "acked", "promoted"):
            self.assertEqual(doc[key], {})
        self.assertEqual((doc["pinned"], doc["posts"], doc["watermark"]), ([], [], None))

    def test_bad_marks_and_posts_are_dropped_on_read(self) -> None:
        doc = store.normalize_document({
            "watermark": "yesterday",
            "dismissed": {"ok@2026-01-01T00:00:00Z": "2026-01-02T00:00:00Z", "bad key": "2026-01-02T00:00:00Z", "k@": "not a time"},
            "acked": {"k": "2026-01-02T00:00:00Z", "z": 5},
            "promoted": {"k": {"project_id": "p", "workitem_id": "w"}, "half": {"project_id": "p"}},
            "pinned": ["a", "a", "b c", 7],
            "posts": [{"id": "c0ffee01", "ts": "2026-01-01T00:00:00Z", "title": "t", "kind": "Bad Kind", "source": "ok"},
                      {"id": "nope", "ts": "2026-01-01T00:00:00Z", "title": "t"}, {"id": "c0ffee02", "ts": "x", "title": ""}, "junk"],
        })
        self.assertIsNone(doc["watermark"])
        self.assertEqual(list(doc["dismissed"]), ["ok@2026-01-01T00:00:00Z"])
        self.assertEqual(list(doc["acked"]), ["k"])
        self.assertEqual(list(doc["promoted"]), ["k"])
        self.assertEqual(doc["pinned"], ["a"])
        self.assertEqual([p["id"] for p in doc["posts"]], ["c0ffee01"])
        self.assertEqual(doc["posts"][0]["kind"], "note")

    def test_build_post_validates_and_shapes(self) -> None:
        post = store.build_post(title="  Need a decision  ", kind="question", source="sample_agent",
                                ref={"workitem_id": "w1", "issue": {"repo": "o/r", "number": 3}, "odd": 1},
                                link={"view": "projects", "project": "p", "junk": 1}, url="https://x.test/1")
        self.assertEqual(post["title"], "Need a decision")
        self.assertEqual(post["ref"], {"workitem_id": "w1", "issue": {"repo": "o/r", "number": 3}})
        self.assertEqual(post["link"], {"view": "projects", "project": "p"})
        self.assertIsNone(post["pid"])
        for kwargs, code in (({"title": ""}, "invalid_value"), ({"title": "t", "kind": "A B"}, "invalid_value"),
                             ({"title": "t", "url": "ftp://x"}, "invalid_value"),
                             ({"title": "t", "project_id": "../x"}, "invalid_project_id"),
                             ({"title": "t", "link": "no"}, "invalid_link"), ({"title": "t", "ref": []}, "invalid_value")):
            with self.subTest(code=code, kwargs=kwargs):
                with self.assertRaises(store.WorkError) as ctx:
                    store.build_post(**kwargs)
                self.assertEqual(ctx.exception.code, code)


class FileTests(_Sandbox):
    def test_a_read_never_creates_the_file_and_a_write_does(self) -> None:
        doc, ok = store.load_document()
        self.assertTrue(ok)
        self.assertFalse(self.path().exists())
        store.modify(lambda d: False)
        self.assertFalse(self.path().exists())
        store.modify(lambda d: store.ack(d, "job:runs:x:2026-09-18"))
        self.assertEqual(list(self.read()["acked"]), ["job:runs:x:2026-09-18"])
        for page in store.PAGES:
            self.assertEqual(self.read(page)["schema"], store.SCHEMA, page)
        self.assertEqual(self.read("history")["posts"], [])
        self.assertEqual(self.read("live")["stream"]["agents"], {"enabled": True})
        self.assertNotIn("dismissed", self.read("history"))
        self.assertNotIn("posts", self.read("inbox"))

    def test_a_malformed_file_is_served_empty_and_never_overwritten(self) -> None:
        self.write("{not json", "history")
        doc, ok = store.load_document()
        self.assertFalse(ok)
        self.assertEqual(doc["posts"], [])
        with self.assertRaises(store.WorkError) as ctx:
            store.modify(lambda d: store.ack(d, "k"))
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("scope_unavailable", 500))
        self.assertEqual(self.path("history").read_text(encoding="utf-8"), "{not json")
        self.assertFalse(self.path("inbox").exists(), "a broken sibling blocks every write")

    def test_hand_edits_survive_a_write_in_their_own_file(self) -> None:
        self.write({"schema": 3, "note": "mine"})
        self.write({"schema": 3, "posts": [{"id": "c0ffee01", "ts": iso(NOW), "title": "t", "extra": True}]}, "history")
        store.modify(lambda d: store.set_pin(d, "k", True))
        self.assertEqual(self.read()["note"], "mine")
        self.assertNotIn("note", self.read("history"))
        history = self.read("history")
        self.assertTrue(history["posts"][0]["extra"])
        self.assertEqual(history["pinned"], ["k"])

    def test_the_attention_kinds_live_in_inbox_and_the_reader_switches_in_history(self) -> None:
        self.write({"schema": 3, "attention": {"connections": ["gmail.unread"]}})
        self.write({"schema": 3, "sources": {"jobs": {"enabled": False}}}, "history")
        doc, ok = store.load_document()
        self.assertTrue(ok)
        self.assertEqual(doc["sources"]["connections"], {"enabled": True, "attention": ["gmail.unread"]})
        self.assertFalse(doc["sources"]["jobs"]["enabled"])
        store.modify(lambda d: store.ack(d, "k"))
        self.assertEqual(self.read()["attention"], {"connections": ["gmail.unread"]})
        self.assertNotIn("attention", self.read("history")["sources"]["connections"])
        self.assertFalse(self.read("history")["sources"]["jobs"]["enabled"])

    def test_the_first_cut_single_file_is_adopted_once(self) -> None:
        old = self.root / ".quirq" / "work" / "work.json"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({"schema": 2, "watermark": iso(NOW), "dismissed": {"a@b": iso(NOW)},
                                   "sources": {"connections": {"enabled": True, "attention": ["slack.mention"]}},
                                   "posts": [{"id": "c0ffee01", "ts": iso(NOW), "title": "t"}]}), encoding="utf-8")
        doc, ok = store.load_document()
        self.assertTrue(ok)
        self.assertFalse(old.exists())
        self.assertEqual(list(self.read()["dismissed"]), ["a@b"])
        self.assertEqual(self.read()["attention"], {"connections": ["slack.mention"]})
        self.assertEqual(self.read("history")["watermark"], iso(NOW))
        self.assertEqual([p["id"] for p in self.read("history")["posts"]], ["c0ffee01"])
        self.assertEqual(doc["watermark"], iso(NOW))


class MarkTests(_Sandbox):
    def test_dismissal_hides_the_pair_and_a_new_since_comes_back(self) -> None:
        doc = store.normalize_document({})
        self.assertTrue(store.dismiss(doc, "todo:p:1", "2026-09-17T00:00:00Z"))
        self.assertFalse(store.dismiss(doc, "todo:p:1", "2026-09-17T00:00:00Z"))
        self.assertTrue(store.is_dismissed(doc, "todo:p:1", "2026-09-17T00:00:00Z"))
        self.assertFalse(store.is_dismissed(doc, "todo:p:1", "2026-09-18T00:00:00Z"))
        self.assertTrue(store.dismiss(doc, "share:o/r", None))
        self.assertTrue(store.is_dismissed(doc, "share:o/r", ""))
        self.assertTrue(store.undismiss(doc, "todo:p:1"))
        self.assertFalse(store.undismiss(doc, "todo:p:1"))
        self.assertEqual(list(doc["dismissed"]), ["share:o/r@"])
        with self.assertRaises(store.WorkError):
            store.dismiss(doc, "has space", "")
        with self.assertRaises(store.WorkError):
            store.dismiss(doc, "k", "two words")

    def test_ack_pin_promote_and_watermark(self) -> None:
        doc = store.normalize_document({})
        self.assertTrue(store.ack(doc, "k"))
        self.assertFalse(store.ack(doc, "k"))
        self.assertTrue(store.unack(doc, "k"))
        self.assertFalse(store.unack(doc, "k"))
        self.assertTrue(store.set_pin(doc, "k", True))
        self.assertFalse(store.set_pin(doc, "k", True))
        self.assertTrue(store.set_pin(doc, "k", False))
        self.assertFalse(store.set_pin(doc, "k", False))
        self.assertTrue(store.promote(doc, "k", project_id="p", workitem_id="w"))
        self.assertFalse(store.promote(doc, "k", project_id="q", workitem_id="x"))
        self.assertEqual(doc["promoted"]["k"]["project_id"], "p")
        self.assertTrue(store.set_watermark(doc, "2026-09-18T10:00:00Z"))
        self.assertFalse(store.set_watermark(doc, "2026-09-18T09:00:00Z"), "only moves forward")
        self.assertFalse(store.set_watermark(doc, "2026-09-18T10:00:00Z"))
        self.assertEqual(doc["watermark"], "2026-09-18T10:00:00Z")
        with self.assertRaises(store.WorkError):
            store.set_watermark(doc, "soon")

    def test_add_post_allocates_an_id_and_puts_it_first(self) -> None:
        doc = store.normalize_document({"posts": [{"id": "c0ffee01", "ts": iso(NOW - timedelta(hours=1)), "title": "old"}]})
        stored = store.add_post(doc, store.build_post(title="new", ts=iso(NOW)))
        self.assertRegex(stored["id"], r"^[0-9a-f]{8}$")
        self.assertEqual([p["title"] for p in doc["posts"]], ["new", "old"])
        self.assertIs(store.find_post(doc, stored["id"]), doc["posts"][0])


class RetentionTests(_Sandbox):
    def test_old_posts_and_marks_are_pruned_on_write(self) -> None:
        old, fresh = iso(NOW - timedelta(days=31)), iso(NOW - timedelta(days=1))
        self.write({"schema": 3, "dismissed": {"a@x": old, "b@y": fresh}, "acked": {"c": old, "d": fresh}})
        self.write({"schema": 3, "posts": [{"id": f"{i:08x}", "ts": old if i % 2 else fresh, "title": f"p{i}"} for i in range(6)]}, "history")
        store.modify(lambda d: store.ack(d, "e"), now=NOW)
        self.assertEqual(sorted(p["title"] for p in self.read("history")["posts"]), ["p0", "p2", "p4"])
        self.assertEqual(list(self.read()["dismissed"]), ["b@y"])
        self.assertEqual(sorted(self.read()["acked"]), ["d", "e"])

    def test_posts_are_capped_newest_first(self) -> None:
        doc = store.normalize_document({"posts": [
            {"id": f"{i:08x}", "ts": iso(NOW - timedelta(minutes=i)), "title": f"p{i}"} for i in range(store.POSTS_MAX + 5)]})
        dropped = store.apply_retention(doc, NOW)
        self.assertEqual(dropped, 5)
        self.assertEqual(len(doc["posts"]), store.POSTS_MAX)
        self.assertEqual(doc["posts"][0]["title"], "p0")
        self.assertNotIn("p504", [p["title"] for p in doc["posts"]])


if __name__ == "__main__":
    unittest.main()
