"""Architecture §9.1: what counts as leftover, and when no action is offered."""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
import unittest
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.doctor import leftovers, projects
from services.doctor.context import Context
from tests.doctor_sandbox import PID, DoctorSandbox

OTHER = "11111111-1111-4111-8111-111111111111"


class LeftoverSandbox(DoctorSandbox):
    def setUp(self) -> None:
        super().setUp()
        (self.projects / "second-project").mkdir()  # two live projects, so one leftover is under the count guard

    def runtime(self, key: str) -> None:
        folder = self.state / "projects" / key
        # exist_ok: PID's folder is already live in the golden sample.
        (folder / "sessions").mkdir(parents=True, exist_ok=True)
        (folder / "stats.json").write_text('{"schema": 2}', encoding="utf-8")

    def runtime_findings(self, now=None) -> list[dict]:
        report = self.report(now)
        return [f for c in report["checks"] if c["id"] == "runtime" for f in c["findings"]]


class RuntimeKeyDriftTests(LeftoverSandbox):
    """The doctor's keys include whatever project_layout.runtime_key() resolves."""

    def test_runtime_key_is_always_in_use(self) -> None:
        cases = {
            "with-pid": {"schema": 2, "pid": OTHER, "name": "with-pid"},
            "template": {"schema": 2, "pid": "22222222-2222-4222-8222-222222222222", "_template": True},
            "unsafe-pid": {"schema": 2, "pid": "../escape"},
        }
        for name, document in cases.items():
            (self.projects / name / ".xo").mkdir(parents=True)
            (self.projects / name / ".xo" / "project.json").write_text(json.dumps(document), encoding="utf-8")
        (self.projects / "No Xo Yet").mkdir()
        scanned = {p.name: p for p in projects.scan(Context.from_environment(self.now))}
        for name in (*cases, "No Xo Yet", "sample-project", "second-project"):
            with self.subTest(project=name):
                self.assertIn(project_layout.runtime_key(name), scanned[name].keys_in_use)


class DetectionTests(LeftoverSandbox):
    def test_the_samples_have_no_leftovers(self) -> None:
        self.assertEqual(self.runtime_findings(), [])

    def test_an_old_unused_runtime_folder_is_leftover_with_an_action(self) -> None:
        self.runtime(OTHER)
        [finding] = self.runtime_findings()
        self.assertEqual((finding["id"], finding["subject"], finding["level"]), ("runtime.leftover", OTHER, "WARN"))
        self.assertEqual(finding["action"], {"kind": "move_runtime_leftover_aside"})
        self.assertEqual(finding["details"]["files"], 1)
        self.assertIn("sessions", finding["details"]["contains"])

    def test_a_folder_written_in_the_last_ten_minutes_has_no_action(self) -> None:
        self.runtime(OTHER)
        [finding] = self.runtime_findings(now=time.time() + 60)
        self.assertNotIn("action", finding)

    def test_a_folder_named_after_a_project_is_not_leftover(self) -> None:
        self.runtime("sample-project")  # pre-pid runtime folder, not yet merged
        self.assertEqual([f for f in self.runtime_findings() if f["id"] == "runtime.leftover"], [])

    def test_files_and_symlinks_in_projects_are_never_candidates(self) -> None:
        outside = self.state.parent / "outside"
        outside.mkdir()
        (self.state / "projects" / OTHER).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.runtime_findings(), [])

    def test_a_partly_unreadable_leftover_has_no_action(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.runtime(OTHER)
        sessions = self.state / "projects" / OTHER / "sessions"
        sessions.chmod(0o000)
        self.addCleanup(sessions.chmod, 0o755)
        [finding] = self.runtime_findings()
        self.assertEqual(finding["id"], "runtime.leftover")
        self.assertNotIn("action", finding)

    def test_a_corrupt_project_json_blocks_leftover_detection(self) -> None:
        self.runtime(OTHER)
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("{", encoding="utf-8")
        [finding] = self.runtime_findings()
        self.assertEqual(finding["id"], "runtime.keys_unknown")
        self.assertNotIn("action", finding)

    def test_a_missing_or_empty_projects_root_is_suspect(self) -> None:
        self.runtime(OTHER)
        shutil.rmtree(self.projects)
        self.projects.mkdir()
        [finding] = self.runtime_findings()
        self.assertEqual((finding["id"], finding["level"]), ("runtime.projects_root_suspect", "FAIL"))

    def test_as_many_leftovers_as_projects_blocks_every_action(self) -> None:
        self.runtime(OTHER)
        self.runtime("33333333-3333-4333-8333-333333333333")
        found = self.runtime_findings()
        self.assertEqual(found[0]["id"], "runtime.too_many_leftovers")
        self.assertEqual([f["id"] for f in found[1:]], ["runtime.leftover", "runtime.leftover"])
        self.assertTrue(all("action" not in f for f in found))


class RuntimeSplitTests(LeftoverSandbox):
    def splits(self, now=None) -> list[dict]:
        return [f for f in self.runtime_findings(now) if f["id"] == "runtime.split"]

    def test_an_old_folder_key_copy_next_to_the_pid_folder_warns(self) -> None:
        self.runtime("sample-project")
        [finding] = self.splits()
        self.assertEqual(finding["subject"], "sample-project")
        self.assertNotIn("action", finding)
        self.assertEqual([f for f in self.runtime_findings() if f["id"] == "runtime.leftover"], [])

    def test_a_freshly_written_folder_key_copy_does_not_warn(self) -> None:
        self.runtime("sample-project")
        self.assertEqual(self.splits(now=time.time()), [])

    def test_a_project_with_no_pid_does_not_warn(self) -> None:
        xo = self.projects / "second-project" / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({"schema": 2, "name": "second-project"}), encoding="utf-8")
        self.runtime("second-project")
        self.assertEqual(self.splits(), [])

    def test_a_corrupt_project_json_does_not_warn(self) -> None:
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("{", encoding="utf-8")
        self.runtime("sample-project")
        self.assertEqual(self.splits(), [])

    def test_a_folder_key_another_project_actively_uses_does_not_warn(self) -> None:
        # "Sample-Project" has no pid, so the server keys it by its folder
        # name, which normalizes to the same key as sample-project's own
        # (pre-pid) folder. Restarting the server would merge that folder
        # into sample-project's pid folder, deleting Sample-Project's data.
        self.runtime("sample-project")
        (self.projects / "Sample-Project").mkdir()
        self.assertEqual(self.splits(), [])

    def test_a_folder_key_equal_to_another_projects_pid_does_not_warn(self) -> None:
        self.runtime("sample-project")
        xo = self.projects / "other" / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({"schema": 2, "pid": "sample-project", "name": "other"}),
                                          encoding="utf-8")
        self.assertEqual(self.splits(), [])


class LastKnownNameTests(LeftoverSandbox):
    def leftover_finding(self) -> dict:
        [finding] = [f for f in self.runtime_findings() if f["id"] == "runtime.leftover"]
        return finding

    def append_timeline(self, document: dict) -> None:
        path = self.state / "projects" / "timeline.jsonl"
        with open(path, "ab") as handle:
            handle.write(json.dumps(document).encode("utf-8") + b"\n")

    def test_a_known_pid_names_the_project(self) -> None:
        self.runtime(OTHER)
        self.append_timeline({"pid": OTHER, "project_id": "old-project"})
        finding = self.leftover_finding()
        self.assertIn("It belonged to project old-project", finding["observed"])
        self.assertEqual(finding["details"]["project_name"], "old-project")

    def test_an_unknown_pid_has_no_name(self) -> None:
        self.runtime(OTHER)
        finding = self.leftover_finding()
        self.assertNotIn("belonged to", finding["observed"])
        self.assertNotIn("project_name", finding["details"])

    def test_the_name_is_found_past_five_megabytes_of_noise(self) -> None:
        self.runtime(OTHER)
        path = self.state / "projects" / "timeline.jsonl"
        with open(path, "ab") as handle:
            handle.write(os.urandom(5 * 1024 * 1024).replace(b"\n", b" "))
            handle.write(b"\n")
        self.append_timeline({"pid": OTHER, "project_id": "old-project"})
        finding = self.leftover_finding()
        self.assertEqual(finding["details"]["project_name"], "old-project")

    def test_other_timeline_fields_never_reach_the_report(self) -> None:
        self.runtime(OTHER)
        self.append_timeline({"pid": OTHER, "project_id": "old-project", "note": "PLANTED-TIMELINE-SECRET"})
        report = self.report()
        self.assertNotIn("PLANTED-TIMELINE-SECRET", json.dumps(report))


class MoveAsideTests(LeftoverSandbox):
    def code(self, key: str, **kwargs) -> tuple[str, int]:
        with self.assertRaises(leftovers.DoctorError) as caught:
            leftovers.move_aside(key, now=kwargs.get("now", self.now))
        return caught.exception.code, caught.exception.status

    def test_moves_the_folder_into_quarantine(self) -> None:
        self.runtime(OTHER)
        result = leftovers.move_aside(OTHER, now=self.now)
        self.assertFalse((self.state / "projects" / OTHER).exists())
        # The golden sample already ships one unrelated quarantined example
        # (see tests/fixtures/quirq-state/quarantine/); filter to this key's own.
        [moved] = [p for p in (self.state / "quarantine" / "runtime-leftovers").iterdir()
                   if p.name.startswith(OTHER + "-")]
        self.assertTrue(moved.name.startswith(OTHER + "-") and moved.name.endswith("Z"))
        self.assertTrue((moved / "stats.json").is_file())
        self.assertEqual((result["moved"], result["key"], result["to"], result["files"]), (True, OTHER, str(moved), 1))

    def test_refuses_a_key_a_project_uses(self) -> None:
        self.runtime(PID)
        self.runtime("sample-project")
        self.assertEqual(self.code(PID), ("doctor_not_leftover", 409))
        self.assertEqual(self.code("sample-project"), ("doctor_not_leftover", 409))
        self.assertTrue((self.state / "projects" / PID).is_dir())

    def test_refuses_unsafe_keys_files_and_symlinks(self) -> None:
        outside = self.state.parent / "outside"
        outside.mkdir()
        (self.state / "projects" / OTHER).symlink_to(outside, target_is_directory=True)
        for key in ("../escape", "", "offsets.json", "a.b", OTHER):
            with self.subTest(key=key):
                self.assertEqual(self.code(key)[1], 400)
        self.assertTrue(outside.is_dir())

    def test_a_safe_key_with_no_folder_is_gone_not_invalid(self) -> None:
        self.assertEqual(self.code("missing-key"), ("doctor_gone", 409))

    def test_a_second_move_of_the_same_key_is_gone(self) -> None:
        self.runtime(OTHER)
        leftovers.move_aside(OTHER, now=self.now)
        self.assertEqual(self.code(OTHER), ("doctor_gone", 409))

    def test_a_rename_that_finds_the_source_already_gone_is_gone_not_failed(self) -> None:
        self.runtime(OTHER)
        source = self.state / "projects" / OTHER

        def _vanish(_src, _dst):
            shutil.rmtree(source)
            raise FileNotFoundError()

        with patch("services.doctor.leftovers.os.rename", side_effect=_vanish):
            self.assertEqual(self.code(OTHER), ("doctor_gone", 409))

    def test_a_leftover_that_vanishes_during_the_survey_is_gone_not_too_recent(self) -> None:
        self.runtime(OTHER)
        source = self.state / "projects" / OTHER
        real_measure_tree = leftovers.measure_tree

        def _vanish_then_measure(path):
            if path == source:
                shutil.rmtree(source)
                from services.doctor.reading import Tree
                return Tree(0, 0, None, False)
            return real_measure_tree(path)

        with patch("services.doctor.leftovers.measure_tree", side_effect=_vanish_then_measure):
            self.assertEqual(self.code(OTHER), ("doctor_gone", 409))

    def test_refuses_recent_folders(self) -> None:
        self.runtime(OTHER)
        self.assertEqual(self.code(OTHER, now=time.time()), ("doctor_too_recent", 409))

    def test_refuses_while_a_run_level_rule_fails(self) -> None:
        self.runtime(OTHER)
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.code(OTHER), ("doctor_keys_unknown", 409))
        (self.projects / "sample-project" / ".xo" / "project.json").unlink()
        self.runtime("33333333-3333-4333-8333-333333333333")
        self.assertEqual(self.code(OTHER), ("doctor_too_many_leftovers", 409))
        shutil.rmtree(self.projects)
        self.projects.mkdir()
        self.assertEqual(self.code(OTHER), ("doctor_projects_root_suspect", 409))

    def test_a_failed_rename_moves_nothing_and_never_copies(self) -> None:
        self.runtime(OTHER)
        with patch("services.doctor.leftovers.os.rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            self.assertEqual(self.code(OTHER), ("doctor_move_failed", 500))
        self.assertTrue((self.state / "projects" / OTHER / "stats.json").is_file())
        # Same golden-sample caveat as above: check nothing was added for this key.
        self.assertEqual([p for p in (self.state / "quarantine" / "runtime-leftovers").iterdir()
                           if p.name.startswith(OTHER + "-")], [])

    def test_refuses_an_existing_target(self) -> None:
        self.runtime(OTHER)
        with patch("services.doctor.leftovers._stamp", return_value="20260101T000000Z"):
            target = self.state / "quarantine" / "runtime-leftovers" / f"{OTHER}-20260101T000000Z"
            target.mkdir(parents=True)
            self.assertEqual(self.code(OTHER), ("doctor_move_failed", 500))
        self.assertTrue((self.state / "projects" / OTHER).is_dir())

    def test_a_filesystem_error_during_the_survey_is_a_service_error(self) -> None:
        self.runtime(OTHER)
        with patch("services.doctor.leftovers.measure_tree", side_effect=FileNotFoundError()):
            self.assertEqual(self.code(OTHER), ("doctor_move_failed", 500))
        self.assertTrue((self.state / "projects" / OTHER).is_dir())


if __name__ == "__main__":
    unittest.main()
