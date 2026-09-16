"""A doctor run: healthy baseline, error isolation, and the read-only contract."""

from __future__ import annotations

import json
import shutil
import unittest
from unittest.mock import patch

from services.doctor import checks, run
from tests.doctor_sandbox import PID, DoctorSandbox, snapshot


class BaselineTests(DoctorSandbox):
    def test_the_golden_samples_are_healthy(self) -> None:
        report = self.report()
        self.assertEqual(self.problems(report), [])
        self.assertEqual(report["level"], "OK")
        self.assertEqual(report["schema"], 1)
        self.assertTrue(report["checked_at"].endswith("Z"))
        self.assertEqual(report["roots"], {"state": str(self.state), "projects": str(self.projects)})

    def test_docker_host_paths_are_shown_instead_of_container_paths(self) -> None:
        (self.state / "cache" / "stats.json").write_text("", encoding="utf-8")
        with patch.dict("os.environ", {"QUIRQ_HOST_STATE_ROOT": "/host/.quirq"}):
            report = self.report()
        self.assertEqual(report["roots"]["state"], "/host/.quirq")
        [finding] = self.problems(report)
        self.assertEqual(finding["path"], "/host/.quirq/cache/stats.json")

    def test_a_missing_state_root_is_one_fail_and_is_not_created(self) -> None:
        shutil.rmtree(self.state)
        report = self.report()
        self.assertEqual([c["id"] for c in report["checks"]], ["roots"])
        self.assertEqual(self.ids(report), {"roots.state_unavailable"})
        self.assertFalse(self.state.exists())

    def test_a_check_that_raises_is_an_error_and_the_rest_still_run(self) -> None:
        def boom(ctx):
            raise RuntimeError("broken check")
        with patch.object(run, "CHECKS", (("boom", boom), ("roots", checks.roots))):
            with self.assertLogs("services.doctor.run", level="ERROR"):
                report = self.report()
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(by_id["boom"]["level"], "ERROR")
        self.assertIn("RuntimeError: broken check", by_id["boom"]["error"])
        self.assertEqual(by_id["roots"]["level"], "OK")
        self.assertEqual(report["level"], "ERROR")


class ReadCheckTests(DoctorSandbox):
    def finding(self, finding_id: str) -> dict:
        matches = [f for f in self.problems() if f["id"] == finding_id]
        self.assertEqual(len(matches), 1, self.problems())
        return matches[0]

    def test_a_corrupt_keep_file_fails(self) -> None:
        (self.state / "inbox" / "inbox.json").write_text('{"items": [', encoding="utf-8")
        finding = self.finding("read.invalid_json")
        self.assertEqual((finding["level"], finding["subject"]), ("FAIL", "inbox/inbox.json"))

    def test_a_corrupt_file_written_just_now_is_not_reported(self) -> None:
        path = self.state / "inbox" / "inbox.json"
        path.write_text("", encoding="utf-8")
        self.assertEqual(self.problems(self.report(now=path.stat().st_mtime + 1)), [])

    def test_an_empty_rebuildable_file_warns(self) -> None:
        (self.state / "cache" / "stats.json").write_text("", encoding="utf-8")
        self.assertEqual(self.finding("read.empty")["level"], "WARN")

    def test_wrong_type(self) -> None:
        (self.state / "connections" / "accounts.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.finding("read.wrong_type")["level"], "FAIL")

    def test_schema_newer_and_older(self) -> None:
        inbox = self.state / "inbox" / "inbox.json"
        document = json.loads(inbox.read_text(encoding="utf-8"))
        inbox.write_text(json.dumps({**document, "schema": 9}), encoding="utf-8")
        todos = self.projects / "sample-project" / ".xo" / "todos.json"
        todos.write_text(json.dumps({**json.loads(todos.read_text(encoding="utf-8")), "schema": 1}), encoding="utf-8")
        found = {f["subject"]: f for f in self.problems() if f["id"] == "schema.unsupported"}
        self.assertIn("newer xo-space", found["inbox/inbox.json"]["observed"])
        self.assertIn("older than this xo-space", found["sample-project/.xo/todos.json"]["observed"])
        self.assertEqual({f["level"] for f in found.values()}, {"FAIL"})

    def test_private_files_are_never_opened(self) -> None:
        (self.state / "secrets" / "secrets.env").write_bytes(b"\xff not text, not json")
        (self.state / "settings" / "runtime.env").write_text("", encoding="utf-8")
        self.assertEqual(self.problems(), [])

    def test_unknown_state_files_are_listed_as_ok(self) -> None:
        (self.state / "inbox" / "notes.txt").write_text("x", encoding="utf-8")
        report = self.report()
        unknown = [f for c in report["checks"] for f in c["findings"] if f["id"] == "inventory.unknown_file"]
        self.assertEqual([f["subject"] for f in unknown], ["inbox/notes.txt"])
        self.assertEqual(report["level"], "OK")


class ReadOnlyTests(DoctorSandbox):
    """Architecture §12 invariant 1: a run changes nothing and creates nothing."""

    def break_everything(self) -> None:
        (self.state / "inbox" / "inbox.json").write_text("{", encoding="utf-8")
        (self.state / "inbox" / "inbox.json.tmp").write_text("{}", encoding="utf-8")
        (self.state / "settings" / ".state-abc12345.json").write_text("{}", encoding="utf-8")
        leftover = self.state / "projects" / "11111111-1111-4111-8111-111111111111"
        leftover.mkdir()
        (leftover / "stats.json").write_text('{"schema": 2}', encoding="utf-8")
        (self.state / "projects" / "sample-project").mkdir()
        (self.state / "commands.log.1").write_text("old", encoding="utf-8")
        (self.projects / "sample-project" / ".xo" / "stats.json").write_text("{}", encoding="utf-8")
        shutil.copytree(self.projects / "sample-project", self.projects / "copy-of-sample")
        (self.projects / "no-xo-yet").mkdir()

    def test_a_run_over_a_broken_space_changes_nothing(self) -> None:
        self.break_everything()
        before = snapshot(self.state, self.projects)
        self.report()
        self.assertEqual(snapshot(self.state, self.projects), before)

    def test_a_missing_projects_root_is_not_created(self) -> None:
        shutil.rmtree(self.projects)
        before = snapshot(self.state)
        self.report()
        self.assertFalse(self.projects.exists())
        self.assertEqual(snapshot(self.state), before)


if __name__ == "__main__":
    unittest.main()
