"""Architecture §9.1: what counts as leftover, and when no action is offered."""

from __future__ import annotations

import json
import os
import shutil
import time
import unittest

from services.cowork_agent import project_layout
from services.doctor import projects
from services.doctor.context import Context
from tests.doctor_sandbox import PID, DoctorSandbox

OTHER = "11111111-1111-4111-8111-111111111111"


class LeftoverSandbox(DoctorSandbox):
    def setUp(self) -> None:
        super().setUp()
        (self.projects / "second-project").mkdir()  # two live projects, so one leftover is under the count guard

    def runtime(self, key: str) -> None:
        folder = self.state / "projects" / key
        (folder / "sessions").mkdir(parents=True)
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
        self.assertEqual(self.runtime_findings(), [])

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


if __name__ == "__main__":
    unittest.main()
