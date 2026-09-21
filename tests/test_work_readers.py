"""``services/work``: the readers, the attention derivation, the Inbox
groups and the service, over a copy of the sample state root
(``tests/fixtures/quirq-state``) and the sample project. Hermetic:
XO_PROJECTS_ROOT and QUIRQ_STATE_ROOT point into a temp dir."""
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
from services.cowork_agent.visualizer import workspace_index
from services.work import attention, inbox, readers, service, store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT = ROOT / "tests" / "fixtures" / "xo-project"
PID = "00000000-0000-4000-8000-000000000000"
SESSION = "11111111-1111-4111-8111-111111111111"
WORKITEM = "22222222-2222-4222-8222-222222222222"
FIXTURE_NOW = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)   # the sample's clock


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Sample(unittest.TestCase):
    """A copy of the sample state root and the sample project, with a
    workitems.json that has one open item, so every reader has something."""

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
        workspace_index.reset_cache() if hasattr(workspace_index, "reset_cache") else None

    def tearDown(self) -> None:
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    def doc(self) -> dict:
        return store.load_document()[0]

    def work_file(self, page: str = "inbox") -> dict:
        return json.loads((self.state / "work" / page / f"{page}.json").read_text(encoding="utf-8"))

    def add_workitem(self, *, title="Add a license", status="open", assignee=None, updated=None, github=None) -> str:
        path = self.projects / "sample-project" / ".xo" / "workitems.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        wid = f"{len(doc['items']) + 1:08x}-0000-4000-8000-000000000000"
        stamp = updated or iso(FIXTURE_NOW)
        rec = {"id": wid, "title": title, "labels": [], "source": {"kind": "github", "github": github} if github else {"kind": "local"},
               "assignee": assignee, "links": {"todo_ids": [], "session_ids": []}, "created_at": stamp, "updated_at": stamp,
               "created_by": "sample_agent", "deleted_at": None, "deleted_by": None}
        if not github:
            rec.update(body=None, status=status, state_reason="completed" if status == "closed" else None)
        doc["items"][wid] = rec
        path.write_text(json.dumps(doc), encoding="utf-8")
        return wid

    def add_todo(self, *, content="Write the README", status="blocked", todo_id="a1b2c3d4", updated="2026-01-01T09:06:00Z") -> None:
        path = self.projects / "sample-project" / ".xo" / "todos.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["sessions"].setdefault("_project", {"runtime": "sample_agent", "todos": []})["todos"].append(
            {"id": todo_id, "content": content, "status": status, "description": "waiting on a key", "updated_at": updated})
        path.write_text(json.dumps(doc), encoding="utf-8")


class ReaderTests(_Sample):
    def test_every_reader_answers_the_one_entry_shape_newest_first(self) -> None:
        doc = self.doc()
        for name in readers.READER_NAMES:
            with self.subTest(reader=name):
                rows = readers.reader(name)(doc, limit=50)
                for e in rows:
                    self.assertEqual(set(e) >= {"key", "ts", "source", "kind", "title", "detail", "project_id", "pid", "actor", "ref", "tone"}, True)
                    self.assertEqual(e["source"], name)
                stamps = [e["ts"] for e in rows]
                self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_timeline_lines_get_titles_from_the_records_they_name(self) -> None:
        self.add_todo(status="completed")
        self.add_workitem()
        by_kind = {e["kind"]: e for e in readers.timeline(self.doc(), limit=50)}
        self.assertEqual(by_kind["session.started"]["title"], "Session started in sample-project")
        self.assertEqual(by_kind["session.started"]["actor"], {"runtime": "sample_agent", "session_id": SESSION})
        self.assertEqual(by_kind["todo.added"]["title"], "Todo added: Write the README")
        self.assertEqual(by_kind["file.edited"]["ref"], {"path": "README.md"})
        self.assertEqual(by_kind["workitem.claimed"]["ref"]["workitem_id"], WORKITEM)
        self.assertTrue(by_kind["workitem.claimed"]["title"].startswith("Work started: "))
        self.assertEqual(by_kind["workitem.claimed"]["pid"], PID)
        # the cursor is the reader's, not the file's
        self.assertEqual(readers.timeline(self.doc(), limit=50, before="2026-01-01T09:05:00Z"), [by_kind["session.started"]])

    def test_issue_and_connection_entries_carry_their_refs(self) -> None:
        [issue] = readers.issues(self.doc(), limit=10)
        self.assertEqual((issue["key"], issue["kind"], issue["title"]), ("issue:sample-project:1", "issue.open", "#1 Add a license"))
        self.assertEqual(issue["ref"]["issue"]["repo"], "acme/sample-project")
        self.assertEqual(issue["ref"]["issue"]["node_id"], "I_kwDOsample0001")
        [mail] = readers.connections(self.doc(), limit=10)
        self.assertEqual((mail["key"], mail["kind"], mail["toolkit"]), ("connection:gmail:unread:msg-example-1", "gmail.unread", "gmail"))
        self.assertEqual(mail["tone"], "attention", "gmail.unread is listed in sources.connections.attention")
        self.assertEqual(mail["ref"]["url"], "https://mail.google.com/mail/u/0/#inbox/msg-example-1")

    def test_job_runs_and_posts(self) -> None:
        [run] = readers.jobs(self.doc(), limit=10)
        self.assertEqual((run["kind"], run["title"], run["detail"]), ("job.finished", "Nightly tests", "ok · 12.5 s"))
        self.assertEqual(run["ref"]["job"]["id"], "nightly-tests-a1b2c3")
        self.assertEqual(run["project_id"], "sample-project")
        posts = readers.posts(self.doc(), limit=10)
        self.assertEqual([p["key"] for p in posts], ["post:c0ffee02"], "history.json posts; the retired Inbox file is gone")
        self.assertEqual(posts[0]["kind"], "agent.question")
        self.assertEqual(posts[0]["actor"]["runtime"], "sample_agent")
        self.assertEqual(posts[0]["ref"]["workitem_id"], WORKITEM)

    def test_sharing_reads_the_relay_snapshot(self) -> None:
        sharing_status.record_share("acme/other", available=True) if hasattr(sharing_status, "record_share") else None
        sharing_status._event("acme/other", "shared_with_you", "from ws_1")
        [row] = readers.sharing(self.doc(), limit=10)
        self.assertEqual((row["kind"], row["ref"]["repo"]), ("sharing.shared_with_you", "acme/other"))


class AttentionTests(_Sample):
    def test_work_items_assigned_to_me_and_unassigned(self) -> None:
        mine = self.add_workitem(title="Mine", assignee="local")
        loose = self.add_workitem(title="Loose")
        self.add_workitem(title="Done", status="closed")
        items = {it["reason"]: it for it in attention.derive(self.doc(), me=["me", "local"])}
        self.assertEqual(items["assigned_to_me"]["key"], f"workitem:sample-project:{mine}")
        self.assertEqual(items["assigned_to_me"]["since"], iso(FIXTURE_NOW))
        self.assertEqual(items["unassigned"]["key"], f"workitem:sample-project:{loose}")
        self.assertEqual(items["assigned_to_me"]["primary"], "claim")
        self.assertNotIn("Done", [it["title"] for it in items.values()])

    def test_an_item_in_progress_is_not_a_decision(self) -> None:
        # the sample claim names WORKITEM with a session that is live in the activity snapshot
        path = self.projects / "sample-project" / ".xo" / "workitems.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["items"][WORKITEM] = {"id": WORKITEM, "title": "Claimed", "body": None, "labels": [], "status": "open", "state_reason": None,
                                  "source": {"kind": "local"}, "assignee": "local", "links": {"todo_ids": [], "session_ids": []},
                                  "created_at": iso(FIXTURE_NOW), "updated_at": iso(FIXTURE_NOW), "created_by": "sample_agent",
                                  "deleted_at": None, "deleted_by": None}
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual([it["reason"] for it in attention.derive(self.doc(), me=["local"]) if it["key"].startswith("workitem:")], [])
        [w] = inbox.open_work()
        self.assertTrue(w["in_progress"])
        self.assertEqual(w["claim"]["runtime"], "sample_agent")

    def test_blocked_todos_my_issues_connections_questions_and_errors(self) -> None:
        self.add_todo()
        doc = self.doc()
        doc["promoted"].clear()   # the sample promoted the mail; forget that here
        self.assertEqual(attention.blocked_todos({"sample-project": PID})[0]["key"], "todo:sample-project:a1b2c3d4")
        self.assertEqual(attention.blocked_todos({"sample-project": PID})[0]["since"], "2026-01-01T09:06:00Z")
        mirror = self.state / "projects" / PID / "github" / "issues.json"
        m = json.loads(mirror.read_text(encoding="utf-8"))
        m["issues"]["I_kwDOsample0001"]["assignees"] = [{"login": "Local"}]
        mirror.write_text(json.dumps(m), encoding="utf-8")
        [issue] = attention.my_issues({"sample-project": PID}, frozenset({"local"}), frozenset())
        self.assertEqual(issue["reason"], "issue_mine")
        self.assertEqual(attention.my_issues({"sample-project": PID}, frozenset({"local"}), frozenset({"I_kwDOsample0001"})), [],
                         "an adopted issue is tracked, not a decision")
        # the sample's connections section tracks that mail as an item, which replaces the raw decision; untrack it here
        with patch.object(attention, "_tracked_keys", return_value=frozenset()):
            [mail] = attention.connection_items(doc, {})
            self.assertEqual((mail["reason"], mail["source"]), ("connection", "gmail"))
            self.assertEqual(attention.connection_items(doc, {mail["key"]: {}}), [], "a promoted entry is tracked, not a decision")
        # fed as a work item (section 18), the raw event is not a second decision
        from services.inbox import facts
        facts.ingest(facts.build_fact(title="Welcome to sample-project", kind="gmail.unread", section="connections", entity="gmail",
                                      project_id="sample-project", ts="2026-01-01T09:14:00Z",
                                      source={"kind": "connection", "key": mail["key"],
                                              "connection": {"toolkit": "gmail", "type": "unread", "event": "msg-example-1"}}))
        self.assertEqual(attention.connection_items(doc, {}), [], "tracked as a work item, the raw event is not a second decision")
        questions = attention.question_items(doc, {}, {})
        self.assertEqual([q["key"] for q in questions], ["post:c0ffee02"])
        connections_store.update_state("gmail", last_error="expired")
        errors = attention.source_errors()
        self.assertEqual([e["key"] for e in errors], ["source:gmail"])
        self.assertEqual(errors[0]["since"], "2026-01-01T09:15:00Z", "since the last good poll")

    def test_derive_orders_by_reason_and_never_fails_on_one_source(self) -> None:
        self.add_todo()
        self.add_workitem(title="Loose")
        with patch.object(attention, "source_errors", side_effect=RuntimeError("boom")):
            reasons = [it["reason"] for it in attention.derive(self.doc(), me=["local"])]
        self.assertEqual(reasons, ["unassigned", "todo_blocked", "agent_question"])


class InboxTests(_Sample):
    def test_dismissed_pairs_leave_and_a_new_since_returns(self) -> None:
        self.add_todo()
        self.assertEqual([d["key"] for d in inbox.decisions(self.doc(), me=["local"]) if d["reason"] == "todo_blocked"], [],
                         "the sample dismissed this todo at this since")
        self.add_todo(todo_id="a1b2c3d4", updated="2026-01-02T09:06:00Z")
        self.assertEqual([d["since"] for d in inbox.decisions(self.doc(), me=["local"]) if d["reason"] == "todo_blocked"],
                         ["2026-01-02T09:06:00Z"])

    def test_completed_groups_job_runs_and_drops_acked_rows(self) -> None:
        now = FIXTURE_NOW
        rows = inbox.completed(self.doc(), now=now, rows=[])
        self.assertEqual(rows, [], "the sample acknowledged the nightly run")
        doc = self.doc()
        doc["acked"].clear()
        [job] = inbox.completed(doc, now=now, rows=[])
        self.assertEqual((job["kind"], job["key"], job["title"]), ("job", "job:runs:nightly-tests-a1b2c3:2026-01-01", "Nightly tests finished"))
        self.assertEqual(job["primary"], "output")
        closed = self.add_workitem(title="Shipped", status="closed", updated=iso(now - timedelta(hours=3)))
        stale = self.add_workitem(title="Old", status="closed", updated=iso(now - timedelta(days=3)))
        rows = inbox.completed(doc, now=now, rows=attention.rollup_rows())
        kinds = {(r["kind"], r["title"]) for r in rows}
        self.assertIn(("workitem", "Shipped"), kinds)
        self.assertNotIn(("workitem", "Old"), kinds)
        self.assertEqual([r for r in rows if r["kind"] == "workitem"][0]["ref"]["workitem_id"], closed)
        self.assertNotEqual(closed, stale)

    def test_meetings_are_calendar_events_from_now_to_the_end_of_tomorrow(self) -> None:
        cal = self.state / "connections" / "googlecalendar"
        cal.mkdir()
        (cal / "config.json").write_text(json.dumps({"schema": 1, "enabled": True, "interval_s": 900, "collectors": ["upcoming"]}), encoding="utf-8")
        now = FIXTURE_NOW
        lines = [{"ts": iso(now + timedelta(hours=2)), "type": "upcoming", "key": "e1", "title": "Design sync", "body": "Meet", "url": "https://cal.test/e1", "toolkit": "googlecalendar"},
                 {"ts": iso(now - timedelta(minutes=10)), "type": "upcoming", "key": "e2", "title": "Standup", "body": "", "url": None, "toolkit": "googlecalendar"},
                 {"ts": iso(now + timedelta(days=3)), "type": "upcoming", "key": "e3", "title": "Offsite", "body": "", "url": None, "toolkit": "googlecalendar"},
                 {"ts": iso(now - timedelta(hours=3)), "type": "upcoming", "key": "e4", "title": "Over", "body": "", "url": None, "toolkit": "googlecalendar"}]
        (cal / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        got = inbox.meetings(self.doc(), now=now)
        self.assertEqual([m["title"] for m in got], ["Standup", "Design sync"], "under way first, then soonest; not the offsite, not the one that ended")
        self.assertEqual(got[1]["ends"], iso(now + timedelta(hours=3)))
        self.assertEqual(got[1]["ref"]["url"], "https://cal.test/e1")

    def test_compose_answers_every_group_and_the_badge(self) -> None:
        self.add_workitem(title="Loose")
        page = inbox.compose(self.doc(), me=["local"], now=FIXTURE_NOW)
        self.assertEqual(set(page) >= {"decisions", "calendar", "completed", "work", "counts", "badge", "me", "projects"}, True)
        self.assertEqual(page["projects"], [{"id": "sample-project", "pid": PID}])
        self.assertEqual(page["counts"]["work"], 1)
        self.assertEqual(page["badge"], page["counts"]["decisions"] + page["counts"]["completed"])
        self.assertEqual(page["work"][0]["origin"], "space")


class ServiceTests(_Sample):
    def test_feed_merges_sources_marks_tracked_and_pinned_and_pages(self) -> None:
        page = service.feed(limit=3)
        self.assertEqual(len(page["entries"]), 3)
        self.assertIsNotNone(page["next_cursor"])
        self.assertTrue(all(status["ok"] for status in page["sources"].values()))
        every = service.feed(limit=100)
        self.assertIsNone(every["next_cursor"])
        by_key = {e["key"]: e for e in every["entries"]}
        self.assertEqual(by_key["connection:gmail:unread:msg-example-1"]["tracked"], {"project_id": "sample-project", "workitem_id": WORKITEM})
        self.assertTrue(by_key[f"timeline:session.started:sample-project:{SESSION}"]["pinned"])
        self.assertEqual(every["watermark"], "2026-01-01T09:00:00Z")
        only = service.feed(limit=100, sources=["jobs"], kinds=["job.finished"])
        self.assertEqual([e["source"] for e in only["entries"]], ["jobs"])
        self.assertEqual(service.feed(limit=100, project="nowhere")["entries"], [])
        for kwargs in ({"limit": 0}, {"before": "soon"}, {"sources": ["mail"]}, {"project": "../x"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(service.WorkError):
                    service.feed(**kwargs)

    def test_a_failing_reader_is_reported_not_raised(self) -> None:
        with patch.object(readers, "jobs", side_effect=RuntimeError("disk")):
            page = service.feed(limit=100)
        self.assertEqual(page["sources"]["jobs"]["ok"], False)
        self.assertIn("disk", page["sources"]["jobs"]["error"])
        self.assertTrue(page["entries"])

    def test_marks_round_trip_through_the_file(self) -> None:
        service.dismiss("todo:sample-project:9", "2026-01-01T00:00:00Z")
        service.ack("job:runs:x:2026-01-01")
        service.set_pin("k", True)
        self.assertEqual(service.set_watermark("2026-01-02T00:00:00Z"), {"watermark": "2026-01-02T00:00:00Z"})
        saved = self.work_file()
        self.assertIn("todo:sample-project:9@2026-01-01T00:00:00Z", saved["dismissed"])
        self.assertIn("job:runs:x:2026-01-01", saved["acked"])
        self.assertIn("k", self.work_file("history")["pinned"])
        service.undismiss("todo:sample-project:9")
        service.unack("job:runs:x:2026-01-01")
        service.set_pin("k", False)
        saved = self.work_file()
        self.assertNotIn("todo:sample-project:9@2026-01-01T00:00:00Z", saved["dismissed"])
        self.assertNotIn("job:runs:x:2026-01-01", saved["acked"])
        self.assertEqual(self.work_file("history")["pinned"], ["timeline:session.started:sample-project:" + SESSION])
        post = service.create_post(title="Found a second bug", kind="finding", source="sample_agent", project_id="sample-project")
        self.assertEqual(post["pid"], PID)
        self.assertEqual(self.work_file("history")["posts"][0]["id"], post["id"])

    def test_promote_creates_a_local_item_or_adopts_the_issue_and_is_idempotent(self) -> None:
        with patch.object(service.coder_identity, "resolve_user_id", return_value="local"):
            first = service.promote(key="post:c0ffee02", project_id="sample-project", assignee="me")
            self.assertTrue(first["created"])
            self.assertEqual(first["workitem"]["source"], {"kind": "local"})
            self.assertEqual(first["workitem"]["assignee"], "local")
            self.assertEqual(first["workitem"]["labels"], ["work", "posts"])
            self.assertIn("Capping is a 20-line change", first["workitem"]["body"])
            again = service.promote(key="post:c0ffee02", project_id="sample-project")
            self.assertEqual((again["created"], again["workitem_id"]), (False, first["workitem_id"]))
            issue = service.promote(key="issue:sample-project:1", project_id="sample-project")
            self.assertTrue(issue["created"])
            self.assertEqual(issue["workitem"]["source"]["github"]["node_id"], "I_kwDOsample0001")
        saved = self.work_file()["promoted"]
        self.assertEqual(saved["post:c0ffee02"]["workitem_id"], first["workitem_id"])
        self.assertEqual(saved["issue:sample-project:1"]["workitem_id"], issue["workitem_id"])
        self.assertEqual([d["key"] for d in inbox.decisions(self.doc(), me=["local"]) if d["key"] == "post:c0ffee02"], [],
                         "a promoted question is tracked, not a decision")
        with self.assertRaises(service.WorkError) as ctx:
            service.promote(key="post:nope", project_id="sample-project")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(service.WorkError) as ctx:
            service.promote(key="post:c0ffee01", project_id="elsewhere")
        self.assertEqual(ctx.exception.code, "invalid_project_id")

    def test_summary_is_the_page_in_numbers(self) -> None:
        self.add_workitem(title="Loose")
        with patch.object(attention, "identities", return_value=["local"]):
            page = service.inbox_page(now=FIXTURE_NOW)
            summary = service.summary(now=FIXTURE_NOW)
        self.assertEqual(summary["badge"], page["badge"])
        self.assertEqual(summary["work"], 1)
        self.assertEqual(summary["errors"], 0)


if __name__ == "__main__":
    unittest.main()
