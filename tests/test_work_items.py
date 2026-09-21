"""``services/work/items.py`` and ``services/work/inbox_view.py``: the
policies, the sidecars, the timeline lines, retention, and the join that is
the Inbox (docs/work-and-workitems.md section 18). Hermetic, over a copy of
the sample state root."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.cowork_agent.scopes import VisualizerScope
from services.inbox import facts
from services.work import inbox_view, items, store

from tests.inbox_harness import SampleRoot, ago, connection_fact

NOW = datetime(2026, 9, 21, 9, 0, 0, tzinfo=timezone.utc)


class NamesAndPolicyTests(SampleRoot):
    def test_sections_states_and_keys(self) -> None:
        self.assertEqual(items.SECTIONS, ("connections", "projects", "issues", "agents"))
        self.assertEqual(items.STATES, ("new", "running", "waiting", "failed", "closed"))
        self.assertEqual(items.item_key("p", "w"), "workitem:p:w")
        self.assertEqual(items.section_of({"source": {"kind": "github"}}), "issues")
        self.assertEqual(items.section_of({"source": {"kind": "local"}}, {"section": "connections"}), "connections", "the fact's section wins")
        self.assertEqual(items.section_of({}), "agents")

    def test_policy_defaults_per_section_validation_and_file(self) -> None:
        self.assertEqual(items.read_policy("connections")["sessions"]["mode"], "manual", "the sample pins manual")
        for section in ("projects", "issues", "agents"):
            self.assertEqual(items.read_policy(section)["sessions"]["mode"], "manual", section)
        items.policy_path("connections").unlink()
        connections = items.read_policy("connections")
        self.assertEqual(connections["sessions"]["mode"], "auto", "connections start by themselves by default")
        self.assertEqual((connections["sessions"]["max_concurrent"], connections["sessions"]["max_per_hour"],
                          connections["sessions"]["timeout_s"], connections["sessions"]["act"], connections["retention_days"]),
                         (2, 20, 300, False, 30))
        self.assertEqual(items.normalize_policy({"sessions": {"mode": "bogus", "max_concurrent": 99, "act": "yes"}}, "issues")["sessions"]["mode"], "manual")
        self.assertEqual(items.normalize_policy({"sessions": {"max_concurrent": 99}}, "issues")["sessions"]["max_concurrent"], 10)
        for bad in ({"sessions": {"mode": "bogus"}}, {"sessions": {"kinds": "unread"}}, {"sessions": {"act": 1}},
                    {"sessions": {"timeout_s": 5}}, {"retention_days": 0}, {"odd": 1}, {"sessions": {"runtime": ""}}):
            with self.subTest(bad=bad):
                with self.assertRaises(store.WorkError):
                    items.validate_policy(bad, "issues")
        written = items.write_policy("issues", {"sessions": {"mode": "auto", "kinds": ["issue.open"]}, "retention_days": 7})
        self.assertEqual(items.policy_path("issues"), self.state / "inbox" / "policy" / "issues.json")
        self.assertEqual(json.loads(items.policy_path("issues").read_text(encoding="utf-8"))["sessions"]["kinds"], ["issue.open"])
        self.assertEqual(items.read_policy("issues"), written)
        with self.assertRaises(store.WorkError):
            items.read_policy("mail")


class SidecarTests(SampleRoot):
    def test_sidecars_live_beside_the_claims_and_the_workbench_in_the_project(self) -> None:
        project_id, wid = self.ingest()
        folder = items.sidecar_dir(project_id, wid)
        self.assertEqual(folder.parent.name, "workitems")
        self.assertEqual(folder.parent.parent.parent.name, "projects")
        self.assertTrue((folder / "fact.json").is_file())
        self.assertIsNone(items.read_session(project_id, wid))
        items.write_session(project_id, wid, {"session_id": "s1", "runtime": "r", "project_id": project_id, "started_at": ago()})
        self.assertEqual(items.read_session(project_id, wid)["schema"], 1)
        items.write_outcome(project_id, wid, {"kind": "fyi", "summary": "nothing to do", "at": ago()})
        self.assertEqual(items.read_outcome(project_id, wid)["kind"], "fyi")
        bench = items.workbench_view(project_id, wid)
        self.assertEqual((bench["project_id"], bench["path"], bench["exists"]), (project_id, f"items/{wid}", False))
        items.workbench_dir(project_id, wid).mkdir(parents=True)
        (items.workbench_dir(project_id, wid) / "reply.md").write_text("hi", encoding="utf-8")
        self.assertEqual(items.workbench_view(project_id, wid)["files"], ["reply.md"])
        self.assertEqual(items.workbench_dir(project_id, wid), self.projects / project_id / "items" / wid)

    def test_records_are_required_updated_and_linked(self) -> None:
        project_id, wid = self.ingest()
        with self.assertRaises(store.WorkError) as ctx:
            items.require_record(project_id, "00000000-0000-4000-8000-000000000009")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(store.WorkError) as ctx:
            items.require_record("no-such-project", wid)
        self.assertEqual(ctx.exception.code, "project_not_found")
        with self.assertRaises(store.WorkError):
            items.require_record(project_id, "not-a-uuid")
        record = items.link_session(project_id, wid, "s1")
        self.assertEqual(record["links"]["session_ids"], ["s1"])
        self.assertEqual(items.link_session(project_id, wid, "s1")["links"]["session_ids"], ["s1"], "idempotent")
        self.assertEqual(items.link_session(project_id, wid, "s2")["links"]["session_ids"], ["s1", "s2"])
        closed = items.update_record(project_id, wid, status="closed", state_reason="completed")
        self.assertEqual((closed["status"], closed["state_reason"]), ("closed", "completed"))

    def test_events_land_on_the_project_and_the_space_timelines(self) -> None:
        project_id, wid = self.ingest()
        items.record_event(project_id, "inbox.item.started", workitem_id=wid, title="Invoice question", section="connections",
                           session_id="s1", runtime="sample_agent", status="running")
        items.record_event(project_id, "inbox.item.bogus", workitem_id=wid, title="x", section="connections")
        lines = [json.loads(l) for l in (self.state / "projects" / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        [line] = [l for l in lines if l["type"] == "inbox.item.started"]
        self.assertEqual((line["workitem_id"], line["kind"], line["status"], line["session_id"], line["project_id"]),
                         (wid, "connections", "running", "s1", project_id))
        self.assertNotIn("inbox.item.bogus", [l["type"] for l in lines])
        runtime_lines = items.project_layout.runtime_dir_for_project(project_id) / "timeline.jsonl"
        self.assertIn("inbox.item.started", runtime_lines.read_text(encoding="utf-8"))


class SweepTests(SampleRoot):
    def test_sweep_removes_the_sidecars_of_old_closed_items_and_keeps_the_record(self) -> None:
        project_id, old = self.ingest(connection_fact("old"))
        _, fresh = self.ingest(connection_fact("fresh"))
        _, still_open = self.ingest(connection_fact("open"))
        for wid in (old, fresh, still_open):
            items.workbench_dir(project_id, wid).mkdir(parents=True)
        for wid in (old, fresh):
            items.update_record(project_id, wid, status="closed", state_reason="completed")
        # backdate the old one's closing
        path = VisualizerScope(project_id)._workitems_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["items"][old]["updated_at"] = "2026-01-01T00:00:00Z"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(items.sweep(now=datetime(2026, 9, 21, tzinfo=timezone.utc)), 2, "the old item and the sample's recordless sidecar")
        self.assertIsNone(items.sidecar_dir(project_id, old), None) if items.sidecar_dir(project_id, old) is None else \
            self.assertFalse(items.sidecar_dir(project_id, old).exists())
        self.assertFalse(items.workbench_dir(project_id, old).exists())
        self.assertTrue(items.sidecar_dir(project_id, fresh).exists())
        self.assertTrue(items.sidecar_dir(project_id, still_open).exists())
        self.assertIsNotNone(VisualizerScope(project_id).get_workitem(old), "the work item itself stays")
        self.assertEqual(items.sweep(now=datetime(2026, 9, 21, tzinfo=timezone.utc)), 0)


class JoinTests(SampleRoot):
    def test_rows_states_and_sections(self) -> None:
        project_id, new = self.ingest(connection_fact("new"))
        _, waiting = self.ingest(connection_fact("waiting"))
        _, failed = self.ingest(connection_fact("failed"))
        _, closed = self.ingest(connection_fact("closed"))
        items.write_session(project_id, waiting, {"session_id": "s-w", "runtime": "r", "project_id": project_id, "started_at": ago(9), "ended_at": ago(8), "exit": {"status": "ok", "message": None}})
        items.write_outcome(project_id, waiting, {"kind": "needs_you", "summary": "which seats?", "question": "10 or 12?", "at": ago(8)})
        items.write_session(project_id, failed, {"session_id": "s-f", "runtime": "r", "project_id": project_id, "started_at": ago(7), "ended_at": ago(6), "exit": {"status": "timeout", "message": "timed out"}})
        items.write_session(project_id, closed, {"session_id": "s-c", "runtime": "r", "project_id": project_id, "started_at": ago(5), "ended_at": ago(4), "exit": {"status": "ok", "message": None}})
        items.write_outcome(project_id, closed, {"kind": "handled", "summary": "filed", "at": ago(4)})
        items.update_record(project_id, closed, status="closed", state_reason="completed")
        rows = {r["id"]: r for r in inbox_view.workitem_rows()}
        self.assertEqual({rows[new]["state"], rows[waiting]["state"], rows[failed]["state"], rows[closed]["state"]},
                         {"new", "waiting", "failed", "closed"})
        self.assertEqual((rows[new]["section"], rows[new]["entity"], rows[new]["entities"]["projects"]), ("connections", "gmail", project_id))
        self.assertEqual(rows[waiting]["outcome"]["question"], "10 or 12?")
        self.assertEqual(rows[failed]["session"]["exit"]["status"], "timeout")
        self.assertIsNone(rows[new]["claim"])
        page = inbox_view.build(section="connections", state="open")
        self.assertEqual({r["id"] for r in page["rows"]}, {new, waiting, failed})
        connections = next(s for s in page["sections"] if s["id"] == "connections")
        self.assertEqual(connections["counts"], {"new": 1, "running": 0, "waiting": 1, "failed": 1, "closed": 1})
        self.assertEqual([e["id"] for e in connections["entities"]], ["gmail"])
        self.assertEqual(inbox_view.build(section="connections", state="waiting")["rows"][0]["id"], waiting)
        self.assertEqual(inbox_view.build(section="connections", entity="notion")["count"], 0)
        self.assertEqual(inbox_view.build(state="closed")["rows"][0]["id"], closed)
        self.assertEqual(inbox_view.build(state="all")["count"], 4)
        with self.assertRaises(store.WorkError):
            inbox_view.build(state="bogus")
        with self.assertRaises(store.WorkError):
            inbox_view.build(limit=0)
        # the running set the runner owns wins over everything but closed
        self.assertEqual(inbox_view.build(section="connections", state="active", running=[(project_id, new)])["rows"][0]["id"], new)

    def test_projects_lists_every_project_and_the_sessions_no_work_item_owns(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1", project_id="sample-project"))
        page = inbox_view.build(section="projects", state="all")
        projects = next(s for s in page["sections"] if s["id"] == "projects")
        self.assertEqual({e["id"] for e in projects["entities"]} >= {"sample-project"}, True)
        kinds = {(r["kind"], r["project_id"]) for r in page["rows"]}
        self.assertIn(("workitem", "sample-project"), kinds)
        self.assertIn(("session", "sample-project"), kinds, "the sample's session row is nobody's work item")
        session = next(r for r in page["rows"] if r["kind"] == "session")
        self.assertEqual((session["state"], session["live"], session["entity"]), ("running", True, "sample-project"))
        self.assertEqual(inbox_view.build(section="projects", entity="sample-project", state="all")["count"], 2)
        # a work item that links the session owns it
        items.link_session(project_id, wid, session["id"])
        page = inbox_view.build(section="projects", state="all")
        self.assertEqual([r["kind"] for r in page["rows"]], ["workitem"])

    def test_agents_lists_the_agents_and_the_items_they_own(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        VisualizerScope(project_id).update_workitem(wid, assignee="sample_agent")
        with patch.object(inbox_view, "_agents", return_value=[{"id": "sample_agent", "label": "Sample"}, {"id": "idle", "label": "Idle"}]):
            page = inbox_view.build(section="agents", state="open")
        agents = next(s for s in page["sections"] if s["id"] == "agents")
        counts = {e["id"]: e["counts"]["new"] for e in agents["entities"]}
        self.assertEqual((counts["sample_agent"], counts["idle"]), (1, 0))
        self.assertEqual([r["id"] for r in page["rows"] if r["kind"] == "workitem"], [wid])
        self.assertEqual(page["rows"][0]["entities"]["agents"], "sample_agent")

    def test_candidates_are_new_items_of_the_section_oldest_first(self) -> None:
        project_id, later = self.ingest(connection_fact("later", minutes_ago=1))
        _, earlier = self.ingest(connection_fact("earlier", minutes_ago=30))
        _, started = self.ingest(connection_fact("started", minutes_ago=60))
        _, calendar = self.ingest(connection_fact("cal", minutes_ago=2, toolkit="googlecalendar", kind="upcoming"))
        items.write_session(project_id, started, {"session_id": "s", "runtime": "r", "project_id": project_id, "started_at": ago(59), "ended_at": ago(58), "exit": {"status": "ok", "message": None}})
        self.assertEqual([w for _p, w, _f in inbox_view.candidates("connections")], [earlier, calendar, later])
        self.assertEqual([w for _p, w, _f in inbox_view.candidates("connections", kinds=["gmail.unread"])], [earlier, later])
        self.assertEqual(inbox_view.candidates("issues"), [])


if __name__ == "__main__":
    unittest.main()
