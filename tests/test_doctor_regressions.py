"""#188, item by item: each observed failure has a test that proves it fixed,
and the user's own broken Space (2026-09-23) reads as intended end to end."""

from __future__ import annotations

import importlib
import json
import shutil
import unittest

from tests.doctor_sandbox import PID, DoctorSandbox

OTHER = "11111111-1111-4111-8111-111111111111"

#: #188 item → the test that pins its fix (module.Class.method).
COVERED = {
    "1 hung connections poller": "tests.test_doctor_liveness.ConnectionsTests.test_a_poller_that_stopped_is_reported_once_overdue",
    "2 Gmail failing for hours": "tests.test_doctor_liveness.ConnectionsTests.test_a_connection_failing_for_hours_is_reported_with_its_error",
    "3 crashed watcher": "tests.test_doctor_liveness.WatcherTests.test_a_crashed_watcher_says_when_and_with_what",
    "3 escalation": "tests.test_doctor_liveness.WatcherTests.test_a_stale_heartbeat_escalates_after_five_minutes",
    "4 hung GitHub poller": "tests.test_doctor_liveness.GitHubTests.test_a_mirror_that_stopped_refreshing_is_reported",
    "5 identical explanations": "tests.test_doctor_read_findings.ReadFindingTests.test_three_different_files_no_longer_read_alike",
    "6 emptied Space timeline": "tests.test_doctor_history.HistoryTests.test_an_emptied_space_timeline_is_reported",
    "7 unnamed leftover": "tests.test_doctor_leftovers.NameSourceTests.test_the_inbox_names_it_when_the_space_timeline_is_empty",
    "7 contradiction": "tests.test_doctor_relate.LeftoverFoldTests.test_a_corrupt_file_inside_a_leftover_is_part_of_the_leftover",
    "8 rewritten file called irreplaceable": "tests.test_doctor_read_findings.ReadFindingTests.test_a_reading_position_file_depends_on_the_watcher",
    "D1 delete-the-mirror advice": "tests.test_doctor_read_findings.ReadFindingTests.test_the_issue_mirror_is_never_advised_to_be_deleted",
    "D2 removal markers": "tests.test_doctor_read_findings.ReadFindingTests.test_a_removal_marker_is_never_judged",
    "D4 schema texts": "tests.test_doctor_read_findings.ReadFindingTests.test_schema_policy",
    "D5 theme and branding": "tests.test_doctor_read_findings.ReadFindingTests.test_theme_and_branding_are_judged",
    "D6 inactive usage bookmarks": "tests.test_doctor_read_findings.ReadFindingTests.test_only_the_active_usage_bookmark_is_judged",
    "vanishing: a file being written": "tests.test_doctor_read_findings.ReadFindingTests.test_a_file_being_written_is_noted_not_dropped",
    "summary counts checks": "tests.test_doctor_model.FindingCountsTests.test_the_report_counts_findings_not_checks",
    "one cause, two rows": "tests.test_doctor_relate.ProjectIdentityFoldTests.test_a_damaged_project_json_is_one_entry",
    "wrong cause for too many leftovers": "tests.test_doctor_leftovers.NameSourceTests.test_too_many_leftovers_names_both_causes",
    "every finding has a headline and a next step": "tests.test_doctor_every_finding.EveryFindingTests.test_every_finding_call_has_a_title_and_a_next_step",
    "panel shows the new report": "tests.test_space_doctor_ui.HealthPanelTests.test_row_shows_evidence_and_the_three_answers_escaped",
    "panel lists vanished findings": "tests.test_space_doctor_ui.HealthPanelTests.test_findings_that_vanish_are_listed",
}


class CoverageMapTests(unittest.TestCase):
    def test_every_item_points_at_a_real_test(self) -> None:
        for item, dotted in COVERED.items():
            module_name, class_name, method = dotted.rsplit(".", 2)
            with self.subTest(item=item):
                cls = getattr(importlib.import_module(module_name), class_name)
                self.assertTrue(callable(getattr(cls, method, None)), dotted)


class TheUsersBrokenSpaceTests(DoctorSandbox):
    """2026-09-23: offsets.json and a todos.json emptied, a stats.json corrupted,
    the Space timeline emptied, then that project's folder deleted."""

    def test_it_reads_as_intended(self) -> None:
        brain = self.projects / "brain" / ".xo"
        shutil.copytree(self.projects / "sample-project" / ".xo", brain)
        identity = json.loads((brain / "project.json").read_text(encoding="utf-8"))
        (brain / "project.json").write_text(json.dumps({**identity, "pid": OTHER, "name": "brain"}), encoding="utf-8")
        (brain / "todos.json").write_text("", encoding="utf-8")
        (self.state / "projects" / "offsets.json").write_text("", encoding="utf-8")
        stats = self.state / "projects" / PID / "stats.json"
        stats.write_text(stats.read_text(encoding="utf-8").replace(",", "", 1), encoding="utf-8")
        (self.state / "projects" / "timeline.jsonl").write_text("", encoding="utf-8")
        shutil.rmtree(self.projects / "sample-project")

        problems = self.problems()
        ids = {f["id"] for f in problems}
        self.assertTrue({"read.empty", "history.empty", "runtime.leftover"} <= ids, ids)
        for finding in problems:
            with self.subTest(finding=finding["key"]):
                self.assertTrue(finding.get("title"), finding)
                self.assertTrue(finding.get("next_step"), finding)
                self.assertNotIn("exists nowhere else", finding["why_it_matters"])
        consequences = [f["consequence"] for f in problems if f.get("consequence")]
        self.assertEqual(len(consequences), len(set(consequences)), consequences)
        [leftover] = [f for f in problems if f["id"] == "runtime.leftover"]
        self.assertEqual(leftover["details"]["project_name"], "sample-project")
        self.assertIn("stats.json", " ".join(e["value"] for e in leftover["evidence"]))
        self.assertNotIn(f"projects/{PID}/stats.json", [f["subject"] for f in problems])
