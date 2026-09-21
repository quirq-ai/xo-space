"""``services/work/items.py``: the connection folders, the policy, the
maker, the index, decisions and retention (design section 17). Hermetic,
over a copy of the sample state root."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.connections import store as connections_store
from services.cowork_agent.project_sharing import status as sharing_status
from services.work import attention, inbox, items, service, store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT = ROOT / "tests" / "fixtures" / "xo-project"
NOW = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Sample(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.state = base / ".quirq"
        self.projects = base / "projects"
        shutil.copytree(FIXTURE, self.state, ignore=shutil.ignore_patterns("README.md"))
        shutil.copytree(PROJECT, self.projects / "sample-project")
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects), "QUIRQ_STATE_ROOT": str(self.state),
                                            "XO_SCHEDULER_ENABLED": "false", "QUIRQ_COMMAND_LOG": "off"})
        self._env.start()
        sharing_status.reset()

    def tearDown(self) -> None:
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    def event(self, key: str, *, minutes_ago: int = 5, collector: str = "unread", title: str = "Hello") -> dict:
        return {"ts": iso(NOW - timedelta(minutes=minutes_ago)), "type": collector, "key": key, "title": title,
                "body": "some@example.com", "url": "https://mail.google.com/mail/u/0/#inbox/" + key, "toolkit": "gmail"}

    def space_lines(self) -> list[dict]:
        path = self.state / "projects" / "timeline.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class NamesAndPolicyTests(_Sample):
    def test_item_ids_are_safe_folder_names_and_unique(self) -> None:
        self.assertEqual(items.item_id_for("unread", "msg-example-1"), "unread-msg-example-1")
        odd = items.item_id_for("unread", "1725000000.000100")
        self.assertEqual(odd, "unread-1725000000.000100")
        spaced = items.item_id_for("mention", "C01/p 1")
        self.assertRegex(spaced, r"^mention-C01-p-1-[0-9a-f]{8}$", "a changed key carries a hash")
        long = items.item_id_for("unread", "x" * 200)
        self.assertRegex(long, r"^unread-x{48}-[0-9a-f]{8}$")
        self.assertNotEqual(items.item_id_for("unread", "a b"), items.item_id_for("unread", "a-b"))
        for bad in ("../x", "", ".hidden", "a b"):
            with self.assertRaises(store.WorkError):
                items.check_item_id(bad)
        self.assertEqual(items.parse_item_key("item:gmail:unread-msg-example-1"), ("gmail", "unread-msg-example-1"))
        self.assertIsNone(items.parse_item_key("item:Bad Toolkit:x"))
        self.assertIsNone(items.parse_item_key("todo:p:1"))

    def test_policy_defaults_validation_and_folder(self) -> None:
        policy = items.normalize_policy({"items": {"unread": 1, "": True}, "sessions": {"mode": "loud", "max_concurrent": 99, "act": "yes"}, "note": "mine"})
        self.assertEqual(policy["items"], {"unread": True})
        self.assertEqual(policy["sessions"]["mode"], "manual")
        self.assertEqual(policy["sessions"]["max_concurrent"], 10)
        self.assertFalse(policy["sessions"]["act"])
        self.assertEqual(policy["note"], "mine")
        for body, text in (({"items": {"unread": "yes"}}, "items"), ({"sessions": {"mode": "loud"}}, "mode"),
                           ({"sessions": {"timeout_s": 5}}, "timeout_s"), ({"sessions": {"act": 1}}, "act"),
                           ({"retention_days": 0}, "retention"), ({"colour": 1}, "unknown")):
            with self.subTest(body=body):
                with self.assertRaises(store.WorkError) as ctx:
                    items.validate_policy(body)
                self.assertIn(text, ctx.exception.message)
        self.assertIsNone(items.read_policy("slack"))
        written = items.write_policy("slack", {"items": {"mentions": True}, "sessions": {"mode": "auto", "kinds": ["mentions"]}})
        self.assertEqual(written["sessions"]["mode"], "auto")
        self.assertEqual(items.read_policy("slack")["sessions"]["kinds"], ["mentions"])
        self.assertEqual(items.list_connections(), ["gmail", "slack"])
        with self.assertRaises(store.WorkError):
            items.write_policy("Bad Toolkit", {})


class MakerTests(_Sample):
    def test_listed_collector_events_become_item_folders_once(self) -> None:
        connections_store.append_events("gmail", [self.event("m1"), self.event("m2", minutes_ago=3, collector="drafts")])
        made = items.make_items("gmail", now=NOW)
        self.assertEqual(made, ["unread-m1"], "only the listed collector")
        record = items.read_item("gmail", "unread-m1")
        self.assertEqual((record["status"], record["kind"], record["collector"], record["key"]), ("new", "gmail.unread", "unread", "m1"))
        self.assertEqual(record["url"], "https://mail.google.com/mail/u/0/#inbox/m1")
        idx = items.read_index("gmail")
        self.assertEqual(idx["cursors"]["unread"], iso(NOW - timedelta(minutes=5)))
        self.assertEqual(idx["items"]["unread-m1"]["status"], "new")
        self.assertEqual(items.make_items("gmail", now=NOW), [], "idempotent")
        created = [line for line in self.space_lines() if line["type"] == "inbox.item.created"]
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["item_id"], created[0]["kind"], created[0]["project_id"], list(created[0])[:2]),
                         ("unread-m1", "gmail", "inbox-gmail", ["ts", "type"]))
        self.assertNotIn("some@example.com", json.dumps(created[0]), "bodies never reach the timeline")

    def test_bootstrap_reads_only_the_last_day_then_the_cursor_rules(self) -> None:
        idx_path = items.index_path("gmail")
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        idx["cursors"] = {}
        idx["items"] = {}
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        shutil.rmtree(items.item_dir("gmail", "unread-msg-example-1"))
        connections_store.append_events("gmail", [self.event("old", minutes_ago=60 * 30), self.event("fresh", minutes_ago=10)])
        self.assertEqual(items.make_items("gmail", now=NOW), ["unread-fresh"], "the day-old one is not an item")
        self.assertEqual(items.read_index("gmail")["cursors"]["unread"], iso(NOW - timedelta(minutes=10)))
        connections_store.append_events("gmail", [self.event("later", minutes_ago=1)])
        self.assertEqual(items.make_items("gmail", now=NOW), ["unread-later"])
        self.assertEqual(sorted(items.read_index("gmail")["items"]), ["unread-fresh", "unread-later"])

    def test_no_policy_or_no_listed_collector_makes_nothing(self) -> None:
        connections_store.append_events("gmail", [self.event("m1")])
        items.write_policy("gmail", {"items": {}})
        self.assertEqual(items.make_items("gmail", now=NOW), [])
        self.assertEqual(items.make_items("slack", now=NOW), [], "no folder, no items")


class DecisionListingRetentionTests(_Sample):
    def test_decide_list_and_detail(self) -> None:
        connections_store.append_events("gmail", [self.event("m1")])
        items.make_items("gmail", now=NOW)
        rows = items.list_items("gmail")
        self.assertEqual([r["id"] for r in rows], ["unread-m1", "unread-msg-example-1"], "newest first")
        self.assertEqual(rows[0]["key"], "item:gmail:unread-m1")
        self.assertEqual([r["id"] for r in items.list_items(status="new")], ["unread-m1"])
        with self.assertRaises(store.WorkError):
            items.list_items(status="odd")
        record = items.mark_decided("gmail", "unread-m1", "dismissed")
        self.assertEqual(record["decided"]["action"], "dismissed")
        self.assertEqual(items.read_index("gmail")["items"]["unread-m1"]["decided"], "dismissed")
        self.assertEqual([line["status"] for line in self.space_lines() if line["type"] == "inbox.item.decided"], ["dismissed"])
        detail = items.item_detail("gmail", "unread-msg-example-1")
        self.assertEqual(detail["outcome"]["kind"], "reply_drafted")
        self.assertEqual(detail["session"]["native_session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(detail["workbench"], {"project_id": "inbox-gmail", "path": "items/unread-msg-example-1", "exists": False, "files": []})
        with self.assertRaises(store.WorkError) as ctx:
            items.item_detail("gmail", "unread-nope")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(store.WorkError):
            items.mark_decided("gmail", "unread-m1", "ignored")

    def test_sweep_removes_old_decided_and_failed_items_and_their_workbench(self) -> None:
        connections_store.append_events("gmail", [self.event("m1"), self.event("m2", minutes_ago=4)])
        items.make_items("gmail", now=NOW)
        items.mark_decided("gmail", "unread-m1", "accepted")
        items.update_item("gmail", "unread-m2", status="failed")
        bench = items.workbench_dir("gmail", "unread-m1")
        bench.mkdir(parents=True)
        (bench / "reply.md").write_text("hi", encoding="utf-8")
        self.assertEqual(items.sweep("gmail", now=NOW), 1, "the sample's item was accepted months ago; m1 and m2 are fresh")
        self.assertEqual(sorted(items.read_index("gmail")["items"]), ["unread-m1", "unread-m2"])
        self.assertEqual(items.sweep("gmail", now=NOW + timedelta(days=31)), 2, "m1 (accepted) and m2 (failed)")
        self.assertEqual(items.read_index("gmail")["items"], {})
        self.assertFalse(bench.exists())
        self.assertFalse(items.item_dir("gmail", "unread-m2").exists())

    def test_item_entry_and_the_inbox_composition(self) -> None:
        connections_store.append_events("gmail", [self.event("m1", title="Invoice question")])
        items.make_items("gmail", now=NOW)
        entry = items.item_entry("gmail", items.read_item("gmail", "unread-m1"))
        self.assertEqual((entry["key"], entry["source"], entry["kind"], entry["title"]), ("item:gmail:unread-m1", "inbox", "gmail.unread", "Invoice question"))
        self.assertEqual(entry["ref"]["workbench"], "items/unread-m1")
        doc = store.load_document()[0]
        [new] = [it for it in attention.derive(doc, me=["local"]) if it["reason"] == "item_new"]
        self.assertEqual(new["key"], "item:gmail:unread-m1")
        self.assertEqual(new["primary"], "start")
        self.assertIn("Start a session", new["detail"])
        # the same collector's raw event is not a second decision when its kind is listed
        doc["sources"]["connections"]["attention"] = ["gmail.unread"]
        reasons = [it["reason"] for it in attention.derive(doc, me=["local"]) if it["key"].endswith("unread-m1") or it["key"].endswith(":m1")]
        self.assertEqual(reasons, ["item_new"])
        page = inbox.compose(doc, me=["local"], now=NOW)
        self.assertEqual(page["counts"]["running"], 0)
        [conn] = page["connections"]
        self.assertEqual((conn["toolkit"], conn["mode"], conn["waiting"], conn["counts"]["new"]), ("gmail", "manual", 1, 1))


class ServiceTests(_Sample):
    def test_connections_policy_and_items_through_the_service(self) -> None:
        rows = service.inbox_connections()
        self.assertEqual([r["toolkit"] for r in rows["connections"]], ["gmail"])
        self.assertEqual(rows["connections"][0]["policy"]["items"], {"unread": True})
        self.assertEqual(rows["available"], [], "gmail is the only configured connection in the sample")
        with self.assertRaises(store.WorkError) as ctx:
            service.set_connection_policy("slack", {"items": {}})
        self.assertEqual(ctx.exception.code, "connection_not_configured")
        self.assertEqual(service.set_connection_policy("gmail", {"sessions": {"mode": "off"}})["sessions"]["mode"], "off")
        self.assertEqual(service.list_inbox_items(connection="gmail")["count"], 1)
        self.assertEqual(service.inbox_item("gmail", "unread-msg-example-1")["item"]["status"], "done")

    def test_decide_track_promotes_into_a_project_and_marks_the_item(self) -> None:
        connections_store.append_events("gmail", [self.event("m1", title="Reissue the invoice")])
        items.make_items("gmail", now=NOW)
        items.write_outcome("gmail", "unread-m1", {"kind": "task_proposed", "summary": "Finance must reissue it.",
                                                    "task": {"title": "Reissue June invoice at 10 seats", "assignee": None}, "acted": []})
        items.update_item("gmail", "unread-m1", status="done", outcome="task_proposed")
        with self.assertRaises(store.WorkError) as ctx:
            service.decide_inbox_item("gmail", "unread-m1", "track")
        self.assertEqual(ctx.exception.code, "invalid_project_id", "the connection project does not exist until a session ran")
        with patch.object(service.coder_identity, "resolve_user_id", return_value="local"):
            record = service.decide_inbox_item("gmail", "unread-m1", "track", project_id="sample-project")
        self.assertEqual(record["decided"]["action"], "tracked")
        wid = record["decided"]["workitem_id"]
        stored = json.loads((self.projects / "sample-project" / ".xo" / "workitems.json").read_text(encoding="utf-8"))["items"][wid]
        self.assertEqual(stored["title"], "Reissue June invoice at 10 seats")
        self.assertEqual(stored["labels"], ["work", "inbox"])
        self.assertEqual(store.load_document()[0]["promoted"]["item:gmail:unread-m1"]["workitem_id"], wid)
        self.assertEqual(service.decide_inbox_item("gmail", "unread-m1", "dismiss")["decided"]["action"], "tracked", "a decided item stays decided")
        self.assertEqual([it for it in attention.derive(store.load_document()[0], me=["local"]) if it["key"] == "item:gmail:unread-m1"], [])


if __name__ == "__main__":
    unittest.main()
