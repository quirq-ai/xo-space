"""relate: one entry per underlying problem (#188 design §10)."""

from __future__ import annotations

import json
import os
import shutil
from unittest.mock import patch

from tests.doctor_sandbox import PID, DoctorSandbox
from tests.test_doctor_liveness import LivenessSandbox, _stamp


class LeftoverFoldTests(DoctorSandbox):
    def test_a_corrupt_file_inside_a_leftover_is_part_of_the_leftover(self) -> None:
        # #188 issue 7: they were two unlinked rows, one saying "exists nowhere else".
        (self.projects / "keeper-1").mkdir()
        (self.projects / "keeper-2").mkdir()
        shutil.rmtree(self.projects / "sample-project")
        stats = self.state / "projects" / PID / "stats.json"
        stats.write_text(stats.read_text(encoding="utf-8").replace(",", "", 1), encoding="utf-8")
        report = self.report()
        problems = self.problems(report)
        self.assertNotIn(f"projects/{PID}/stats.json", [f["subject"] for f in problems])
        [leftover] = [f for f in problems if f["id"] == "runtime.leftover"]
        self.assertEqual(leftover["level"], "WARN")
        self.assertEqual([r["id"] for r in leftover["related"]], ["read.invalid_json"])
        self.assertIn("stats.json", " ".join(e["value"] for e in leftover["evidence"]))
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(by_id["read"]["level"], "OK")


class ProjectIdentityFoldTests(DoctorSandbox):
    def test_a_damaged_project_json_is_one_entry(self) -> None:
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("", encoding="utf-8")
        problems = self.problems()
        self.assertEqual([f["id"] for f in problems], ["read.empty"])
        self.assertIn("Leftover checks", [e["label"] for e in problems[0]["evidence"]])

    def test_a_project_with_no_file_finding_stays_in_keys_unknown(self) -> None:
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("", encoding="utf-8")
        (self.projects / "on-a-missing-disk").symlink_to(self.projects / "nowhere")
        [blocked] = [f for f in self.problems() if f["id"] == "runtime.keys_unknown"]
        self.assertEqual(blocked["subject"], "on-a-missing-disk")
        self.assertEqual([p["name"] for p in blocked["details"]["projects"]], ["on-a-missing-disk"])


class ProjectsRootFoldTests(DoctorSandbox):
    def test_a_missing_projects_root_is_one_entry(self) -> None:
        shutil.rmtree(self.projects)
        problems = self.problems()
        self.assertEqual([f["id"] for f in problems], ["roots.projects_unavailable"])
        self.assertEqual([r["id"] for r in problems[0]["related"]], ["runtime.projects_root_suspect"])


class ComponentFoldTests(LivenessSandbox):
    def test_missed_schedules_belong_to_a_stuck_watcher(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true", "XO_SCHEDULER_ENABLED": "true"}):
            self.beat(400)
            job = {"id": "j1", "name": "nightly", "enabled": True, "every_seconds": 86400,
                   "command": {"argv": ["true"], "timeout": 60}}
            (self.state / "scheduler" / "jobs.json").write_text(
                json.dumps({"schema": 1, "jobs": {"j1": job}}), encoding="utf-8")
            (self.state / "scheduler" / "state.json").write_text(json.dumps({"schema": 1, "jobs": {"j1": {
                "next_run": _stamp(self.now - 600), "last_run": None, "running_since": None,
                "last_result": None}}}), encoding="utf-8")
            problems = self.problems()
        self.assertEqual([f["id"] for f in problems if f["id"].startswith("scheduler.")], [])
        [watcher] = [f for f in problems if f["id"] == "watcher.heartbeat"]
        self.assertEqual([r["id"] for r in watcher["related"]], ["scheduler.overdue"])

    def test_stale_mirrors_belong_to_a_crashed_github_poller(self) -> None:
        project = self.projects / "sample-project" / ".xo" / "project.json"
        document = json.loads(project.read_text(encoding="utf-8"))
        document["git"] = {"remote_url": "https://github.com/acme/sample-project"}
        project.write_text(json.dumps(document), encoding="utf-8")
        mirror = self.state / "projects" / PID / "github" / "issues.json"
        mirror.write_text(json.dumps({**json.loads(mirror.read_text(encoding="utf-8")),
                                      "fetched_at": _stamp(self.now - 7200), "error": None}), encoding="utf-8")
        with patch.dict(os.environ, {"XO_GITHUB_POLL_ENABLED": "true"}), \
             self.tasks(self.record("github poller", state="crashed", ended_at=self.now - 7000, error="RuntimeError: x")):
            problems = self.problems()
        [crashed] = [f for f in problems if f["id"] == "component.crashed"]
        self.assertEqual([r["id"] for r in crashed["related"]], ["github.stale"])
        self.assertNotIn("github.stale", [f["id"] for f in problems])
