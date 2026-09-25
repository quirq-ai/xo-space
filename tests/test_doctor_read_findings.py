"""Read findings say which file, what breaks, what happens by itself, and what to do."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

from services.timestamps import iso
from tests.doctor_sandbox import PID, DoctorSandbox


class ReadFindingTests(DoctorSandbox):
    def by_subject(self, report=None) -> dict[str, dict]:
        return {f["subject"]: f for f in self.problems(report)}

    def test_three_different_files_no_longer_read_alike(self) -> None:
        # #188 issue 5: all three once got "exists nowhere else".
        (self.state / "projects" / "offsets.json").write_text("", encoding="utf-8")
        stats = self.state / "projects" / PID / "stats.json"
        stats.write_text(stats.read_text(encoding="utf-8").replace(",", "", 1), encoding="utf-8")
        (self.projects / "sample-project" / ".xo" / "todos.json").write_text("", encoding="utf-8")
        found = self.by_subject()
        rows = [found["projects/offsets.json"], found[f"projects/{PID}/stats.json"],
                found["sample-project/.xo/todos.json"]]
        self.assertEqual(len({row["consequence"] for row in rows}), 3)
        for row in rows:
            self.assertNotIn("exists nowhere else", row["why_it_matters"])
            self.assertTrue(row["title"] and row["next_step"] and row["self_repair"])
            self.assertEqual({e["label"] for e in row["evidence"]} >= {"Owned by", "Size", "Last modified"}, True)
        self.assertEqual(found[f"projects/{PID}/stats.json"]["title"],
                         "Project sample-project's usage record is damaged (not valid JSON)")
        self.assertEqual(found["sample-project/.xo/todos.json"]["problem_key"], "file:sample-project/.xo/todos.json")

    def test_a_reading_position_file_depends_on_the_watcher(self) -> None:
        # #188 issue 8: the server rewrites it itself while the watcher runs.
        (self.state / "projects" / "offsets.json").write_text("", encoding="utf-8")
        stopped = self.by_subject()["projects/offsets.json"]  # sandbox: watcher disabled
        self.assertEqual(stopped["level"], "FAIL")
        self.assertIn("isn't running", stopped["consequence"])
        beat = {"schema": 1, "last_tick_at": iso(datetime.fromtimestamp(self.now - 1, timezone.utc))}
        (self.state / "cache" / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"}):
            alive = self.by_subject()["projects/offsets.json"]
        self.assertEqual(alive["level"], "WARN")
        self.assertIn("Don't restart", alive["next_step"])

    def test_the_issue_mirror_is_never_advised_to_be_deleted(self) -> None:
        # D1: deleting it closes the project's GitHub items in the Inbox.
        (self.state / "projects" / PID / "github" / "issues.json").write_text("", encoding="utf-8")
        finding = self.by_subject()[f"projects/{PID}/github/issues.json"]
        self.assertEqual(finding["level"], "WARN")
        self.assertIn("Leave it", finding["next_step"])

    def test_a_removal_marker_is_never_judged(self) -> None:
        # D2: existence is its data; deleting it re-clones the removed project.
        marker = next((self.state / "sharing" / "removed").glob("*.json"))
        marker.write_text("", encoding="utf-8")
        self.assertNotIn(str(marker.relative_to(self.state)), self.by_subject())

    def test_theme_and_branding_are_judged(self) -> None:
        # D5: a damaged one breaks its page, and the UI can't fix it.
        (self.state / "settings" / "theme.json").write_text("{", encoding="utf-8")
        finding = self.by_subject()["settings/theme.json"]
        self.assertEqual(finding["level"], "FAIL")
        self.assertIn("Delete it", finding["next_step"])

    def test_only_the_active_usage_bookmark_is_judged(self) -> None:
        # D6: other agents' bookmarks are never read. The active path is pinned
        # through USAGE_SYNC_STATE_FILE so the test doesn't depend on AGENT_NAME.
        active = self.state / "usage" / "active.json"
        (self.state / "usage" / "sample_agent.json").write_text("[]", encoding="utf-8")
        with patch.dict(os.environ, {"USAGE_SYNC_STATE_FILE": str(active)}):
            self.assertNotIn("usage/sample_agent.json", self.by_subject())
            active.write_text("[]", encoding="utf-8")
            finding = self.by_subject()["usage/active.json"]
        self.assertEqual(finding["level"], "FAIL")
        self.assertIn("Usage reporting has stopped", finding["consequence"])

    def test_schema_policy(self) -> None:
        def stamp(path, number):
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "schema": number}),
                            encoding="utf-8")
        xo = self.projects / "sample-project" / ".xo"
        stamp(self.state / "inbox" / "inbox.json", 9)   # ignored: newer → WARN
        stamp(xo / "workitems.json", 0)                 # refused: older (reads only 1) → FAIL
        stamp(xo / "todos.json", 1)                     # ignored when older → nothing
        found = {f["subject"]: f for f in self.problems() if f["id"] == "schema.unsupported"}
        self.assertEqual(found["inbox/inbox.json"]["level"], "WARN")
        self.assertIn("newer xo-space", found["inbox/inbox.json"]["observed"])
        self.assertEqual(found["sample-project/.xo/workitems.json"]["level"], "FAIL")
        self.assertIn("older than this xo-space", found["sample-project/.xo/workitems.json"]["observed"])
        self.assertNotIn("sample-project/.xo/todos.json", found)

    def test_an_unstamped_todos_file_is_accepted(self) -> None:
        todos = self.projects / "sample-project" / ".xo" / "todos.json"
        document = json.loads(todos.read_text(encoding="utf-8"))
        document.pop("schema", None)
        todos.write_text(json.dumps(document), encoding="utf-8")
        self.assertNotIn("sample-project/.xo/todos.json", self.by_subject())

    def test_a_file_being_written_is_noted_not_dropped(self) -> None:
        path = self.state / "inbox" / "inbox.json"
        path.write_text("", encoding="utf-8")
        report = self.report(now=path.stat().st_mtime + 1)
        self.assertEqual(self.problems(report), [])
        notes = [f for c in report["checks"] for f in c["findings"] if f["id"] == "read.recent"]
        self.assertEqual([n["level"] for n in notes], ["OK"])

    def test_the_server_log_is_watched_for_growth(self) -> None:
        log = self.state / "logs" / "quirq.log"
        with patch("services.doctor.checks.MAX_FILE_BYTES", 10):
            log.write_bytes(b"x" * 64)
            grown = [f for f in self.problems() if f["id"] == "growth.file_size"]
        self.assertIn("logs/quirq.log", [f["subject"] for f in grown])
