"""``services/inbox``: the ledger, a fact and the work item it becomes, and
the service's ingest (docs/work-and-workitems.md section 18). Hermetic, over
a copy of the sample state root."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.scopes import VisualizerScope
from services.inbox import facts, feeders, ledger, service
from services.work import items

from tests.inbox_harness import ROOT, SampleRoot, ago, connection_fact


class LedgerTests(SampleRoot):
    def test_normalise_fills_every_key_and_keeps_unknown_ones(self) -> None:
        doc = ledger.normalize_document({"schema": 1, "cursors": {"connections": "2026-01-01T09:14:00Z", "issues": "bogus"},
                                         "sources": {"issues": {"enabled": False, "states": ["open", 3]}}, "note": "kept"})
        self.assertEqual(doc["cursors"], {"sharing": None, "issues": None, "connections": "2026-01-01T09:14:00Z"})
        self.assertEqual(doc["sources"]["issues"], {"enabled": False, "states": ["open"]})
        self.assertEqual(doc["sources"]["sharing"], {"enabled": True})
        self.assertEqual(doc["note"], "kept")
        self.assertEqual(ledger.source_config(doc, "connections"), {"enabled": True})

    def test_the_sample_reads_and_a_missing_or_torn_file_is_safe(self) -> None:
        doc, ok = ledger.load_document()
        self.assertTrue(ok)
        self.assertEqual(doc["cursors"]["connections"], "2026-01-01T09:14:00Z")
        ledger.ledger_path().unlink()
        doc, ok = ledger.load_document()
        self.assertTrue(ok, "no file is the empty ledger")
        self.assertIsNone(doc["cursors"]["connections"])
        ledger.ledger_path().write_text("[1, 2", encoding="utf-8")
        doc, ok = ledger.load_document()
        self.assertFalse(ok)
        with self.assertRaises(ledger.InboxError) as ctx:
            ledger.modify(lambda d: True)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ledger.ledger_path().read_text(encoding="utf-8"), "[1, 2", "a torn file is never overwritten")

    def test_modify_writes_only_on_change(self) -> None:
        before = ledger.ledger_path().read_text(encoding="utf-8")
        ledger.modify(lambda d: False)
        self.assertEqual(ledger.ledger_path().read_text(encoding="utf-8"), before)

        def advance(d: dict) -> bool:
            d["cursors"]["sharing"] = "2026-01-02T00:00:00Z"
            return True
        doc = ledger.modify(advance)
        self.assertIsNotNone(doc["updated_at"])
        self.assertEqual(json.loads(ledger.ledger_path().read_text(encoding="utf-8"))["cursors"]["sharing"], "2026-01-02T00:00:00Z")


class FactTests(SampleRoot):
    def test_build_fact_validates_and_shapes(self) -> None:
        fact = connection_fact("m1")
        self.assertEqual(fact["section"], "connections")
        self.assertEqual(fact["entity"], "gmail")
        self.assertEqual(fact["source"]["key"], "connection:gmail:unread:m1")
        self.assertEqual(fact["link"], {"view": "connectors"})
        for bad in (dict(title="  "), dict(title="x" * 301), dict(kind="Bad Kind"), dict(url="ftp://x"),
                    dict(section="mail"), dict(project_id="../x"), dict(ts="yesterday"),
                    dict(source={"kind": "mail"}), dict(link={"view": "Nope"})):
            with self.subTest(bad=bad):
                fields = dict(title="t", body="", kind="note", section="agents", source={"kind": "post", "post": {"agent": "a", "kind": "note"}})
                fields.update(bad)
                with self.assertRaises(ledger.InboxError):
                    facts.build_fact(**fields)
        self.assertEqual(facts.validate_link({"view": "projects", "project": "p", "path": "a/b.md"}), {"view": "projects", "project": "p", "path": "a/b.md"})
        self.assertIsNone(facts.validate_link({"path": "a.md"}), "a path needs a project")
        with self.assertRaises(ledger.InboxError):
            facts.validate_link({"project": "p", "path": "../x"}, strict=True)

    def test_sections_follow_the_source_kind(self) -> None:
        self.assertEqual([facts.section_of_kind(k) for k in ("connection", "sharing", "github", "post", "local", None)],
                         ["connections", "projects", "issues", "agents", "agents", "agents"])
        self.assertEqual(facts.project_id_for("issues"), "inbox-issues")
        with self.assertRaises(ledger.InboxError):
            facts.check_section("mail")

    def test_target_project_is_the_named_project_when_it_exists(self) -> None:
        self.assertEqual(facts.target_project(connection_fact(project_id="sample-project")), "sample-project")
        self.assertEqual(facts.target_project(connection_fact(project_id="no-such-project")), "inbox-connections")
        self.assertEqual(facts.target_project(connection_fact()), "inbox-connections")
        with self.assertRaises(ledger.InboxError):
            facts.ensure_project("no-such-project")

    def test_ingest_makes_the_work_item_once_and_writes_the_fact_beside_the_claims(self) -> None:
        record, created, project_id = facts.ingest(connection_fact("m1"))
        self.assertTrue(created)
        self.assertEqual(project_id, "inbox-connections")
        self.assertTrue((self.projects / "inbox-connections" / ".xo" / "workitems.json").is_file(), "the section project was scaffolded")
        self.assertEqual(record["title"], "Invoice question")
        self.assertIsNone(record["body"], "the body never enters workitems.json")
        self.assertEqual(record["labels"], ["inbox", "connections"])
        self.assertEqual(record["source"]["kind"], "connection")
        self.assertEqual(record["source"]["key"], "connection:gmail:unread:m1")
        self.assertEqual(record["created_by"], "inbox")
        fact_file = facts.fact_path(project_id, record["id"])
        self.assertTrue(fact_file.is_file())
        self.assertEqual(fact_file.parent.parent.name, "workitems")
        stored = json.loads(fact_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["body"], "Hi, the June invoice shows 12 seats.")
        self.assertEqual(stored["workitem_id"], record["id"])
        self.assertEqual(stored["entity"], "gmail")
        first_seen = stored["ingested_at"]
        # the same key again: no second work item, the fact is refreshed, the first sighting kept
        again, created, _ = facts.ingest(connection_fact("m1", title="Invoice question (edited)"))
        self.assertFalse(created)
        self.assertEqual(again["id"], record["id"])
        self.assertEqual(len(VisualizerScope("inbox-connections").list_workitems()), 1)
        self.assertEqual(facts.read_fact(project_id, record["id"])["ingested_at"], first_seen)
        self.assertEqual(facts.read_fact(project_id, record["id"])["title"], "Invoice question (edited)")
        # a fact with a project of its own lands there
        rec2, created, project_id = facts.ingest(connection_fact("m2", project_id="sample-project"))
        self.assertTrue(created)
        self.assertEqual(project_id, "sample-project")
        self.assertEqual(len(VisualizerScope("sample-project").list_workitems()), 1)

    def test_an_issue_is_adopted_in_its_project(self) -> None:
        fact = facts.build_fact(title="Issue #12 in sample-project: Flaky test", body="labels: bug", kind="issue.open",
                                section="issues", entity="o/r", project_id="sample-project", ts=ago(60),
                                url="https://github.com/o/r/issues/12",
                                source={"kind": "github", "github": {"repo": "o/r", "number": 12, "node_id": "I_12", "url": "https://github.com/o/r/issues/12"}})
        record, created, project_id = facts.ingest(fact)
        self.assertTrue(created)
        self.assertEqual(project_id, "sample-project")
        self.assertEqual(record["source"]["github"]["node_id"], "I_12")
        same, created, _ = facts.ingest(fact)
        self.assertFalse(created)
        self.assertEqual(same["id"], record["id"])
        self.assertEqual(items.section_of(record, facts.read_fact(project_id, record["id"])), "issues")


class ServiceTests(SampleRoot):
    def test_refresh_ingests_every_feeder_and_advances_the_cursors(self) -> None:
        fact = connection_fact("m9", minutes_ago=3)
        with patch.object(feeders, "connections", return_value=feeders.FeedResult([fact], fact["ts"])), \
             patch.object(feeders, "issues", side_effect=RuntimeError("mirror exploded")), \
             patch.object(feeders, "sharing", return_value=feeders.FeedResult([], None)):
            self.assertEqual(service.refresh(force=True), 1)
            self.assertEqual(service.refresh(), 0, "throttled")
            self.assertEqual(service.refresh(force=True), 0, "the same key makes no second work item")
        doc, _ok = ledger.load_document()
        self.assertEqual(doc["cursors"]["connections"], fact["ts"])
        self.assertIsNone(doc["cursors"]["issues"], "a failing feeder leaves its cursor alone")
        rows = VisualizerScope("inbox-connections").list_workitems()
        self.assertEqual([r["title"] for r in rows], ["Invoice question"])

    def test_the_cursor_only_moves_forward(self) -> None:
        self.assertTrue(service._cursor_advances(None, "2026-01-01T00:00:00Z"))
        self.assertTrue(service._cursor_advances("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"))
        self.assertFalse(service._cursor_advances("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"))
        self.assertFalse(service._cursor_advances("2026-01-02T00:00:00Z", "junk"))
        self.assertTrue(service._cursor_advances("junk", "2026-01-01T00:00:00Z"))

    def test_create_post_lands_in_the_agents_section_and_answers_the_row(self) -> None:
        row = service.create_post(title="Which license?", body="MIT or Apache", kind="question", source="claude_code")
        self.assertEqual(row["kind"], "workitem")
        self.assertEqual((row["project_id"], row["section"], row["entity"], row["state"]), ("inbox-agents", "agents", "claude_code", "new"))
        self.assertEqual(row["source"], {"kind": "post", "post": {"agent": "claude_code", "kind": "question"}})
        self.assertEqual(row["fact"]["kind"], "question")
        again = service.create_post(title="Which license?", source="claude_code")
        self.assertNotEqual(again["id"], row["id"], "a post carries no key, so every post is a new work item")
        in_project = service.create_post(title="Ship it", source="api", project_id="sample-project")
        self.assertEqual(in_project["project_id"], "sample-project")
        with self.assertRaises(ledger.InboxError) as ctx:
            service.create_post(title="t", project_id="no-such-project")
        self.assertEqual(ctx.exception.code, "invalid_project_id")
        with self.assertRaises(ledger.InboxError):
            service.create_post(title="t", source="Bad Source")

    def test_the_package_names_no_agent_and_keeps_the_dependency_direction(self) -> None:
        for name in ("ledger", "facts", "feeders", "service", "__init__"):
            src = (ROOT / "services" / "inbox" / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotRegex(src, r"\b(openclaw|hermes|claude_code)\b")
                self.assertNotIn("from routers", src)
                self.assertNotIn("\u2014", src)
                self.assertNotIn("\u2013", src)
        src = (ROOT / "services" / "inbox" / "service.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"^from services\.work", "the work package is imported at call time (the runner imports this module)")
        self.assertIn("register_new_events_listener(_ingest_after_poll)", src)
        for name in ("store.py",):
            self.assertFalse((ROOT / "services" / "connections" / name).read_text(encoding="utf-8").count("services.inbox"),
                             "connections never imports the inbox")


if __name__ == "__main__":
    unittest.main()
