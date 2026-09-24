"""Architecture §9.1: what counts as leftover, and when no action is offered."""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import time
import unittest
from pathlib import Path
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

    def test_a_folder_dated_in_the_future_has_no_action(self) -> None:
        folder = self.state / "projects" / "11111111-1111-4111-8111-111111111111"
        folder.mkdir(parents=True)
        (folder / "stats.json").write_text("{}", encoding="utf-8")
        ahead = self.now + 86400
        for path in (folder / "stats.json", folder):
            os.utime(path, (ahead, ahead))
        leftover = [f for f in self.problems() if f["id"] == "runtime.leftover"]
        self.assertEqual(len(leftover), 1)
        self.assertIsNone(leftover[0].get("action"))

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
        report = self.report()
        runtime = [f for c in report["checks"] if c["id"] == "runtime" for f in c["findings"]]
        self.assertEqual(runtime, [])  # no leftover listed, no action offered
        [identity] = [f for f in self.problems(report) if f["subject"] == "sample-project/.xo/project.json"]
        self.assertIn("Leftover checks", [e["label"] for e in identity["evidence"]])

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

    def test_a_folder_key_copy_dated_in_the_future_still_warns(self) -> None:
        self.runtime("sample-project")
        folder = self.state / "projects" / "sample-project"
        ahead = self.now + 86400
        for path in (folder / "stats.json", folder / "sessions", folder):
            os.utime(path, (ahead, ahead))
        [finding] = self.splits()
        self.assertEqual(finding["subject"], "sample-project")

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

    def test_a_line_that_overflows_the_json_parser_does_not_break_detection(self) -> None:
        self.runtime(OTHER)
        path = self.state / "projects" / "timeline.jsonl"
        with open(path, "ab") as handle:
            handle.write(b"[" * 200_000 + b"\n")
        self.append_timeline({"pid": OTHER, "project_id": "old-project"})
        report = self.report()
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertNotEqual(by_id["runtime"]["level"], "ERROR")
        [finding] = [f for f in by_id["runtime"]["findings"] if f["id"] == "runtime.leftover"]
        self.assertEqual(finding["details"]["project_name"], "old-project")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo not available on this platform")
    def test_a_fifo_at_the_timeline_path_does_not_hang(self) -> None:
        self.runtime(OTHER)
        path = self.state / "projects" / "timeline.jsonl"
        path.unlink()
        os.mkfifo(path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        def _timeout(_signum, _frame):
            raise TimeoutError("check() blocked reading a FIFO")

        # A safety net only: if the fix regresses and open() blocks on the
        # FIFO, don't hang the whole suite forever. TimeoutError is itself an
        # OSError subclass, so a bare "did it raise" assertion can't tell a
        # blocked-then-interrupted read from a fast, correct skip; time it
        # instead.
        previous = signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(2)
        started = time.monotonic()
        try:
            finding = self.leftover_finding()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "check() blocked reading the FIFO instead of skipping it")
        self.assertNotIn("belonged to", finding["observed"])
        self.assertNotIn("project_name", finding["details"])

    def test_an_overlong_project_id_is_not_used(self) -> None:
        self.runtime(OTHER)
        self.append_timeline({"pid": OTHER, "project_id": "x" * 300})
        finding = self.leftover_finding()
        self.assertNotIn("belonged to", finding["observed"])
        self.assertNotIn("project_name", finding["details"])


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

    def test_refuses_while_a_project_folder_links_to_missing_storage(self) -> None:
        # A project on a disk that isn't mounted: its runtime folder is keyed
        # by a pid nobody can read, so it must not look abandoned.
        self.runtime(OTHER)
        (self.projects / "on-a-missing-disk").symlink_to(self.state.parent / "unmounted" / "project")
        self.assertEqual(self.code(OTHER), ("doctor_keys_unknown", 409))
        [blocked] = [f for f in self.runtime_findings() if f["id"] == "runtime.keys_unknown"]
        self.assertEqual(blocked["subject"], "on-a-missing-disk")
        self.assertTrue(all("action" not in f for f in self.runtime_findings()))

    def test_refuses_while_a_project_xo_links_to_missing_storage(self) -> None:
        self.runtime(OTHER)
        (self.projects / "external-xo").mkdir()
        (self.projects / "external-xo" / ".xo").symlink_to(self.state.parent / "unmounted" / ".xo")
        self.assertEqual(self.code(OTHER), ("doctor_keys_unknown", 409))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_refuses_while_a_project_links_to_storage_it_cannot_reach(self) -> None:
        # Not dangling, unreachable: the target exists behind a folder this
        # user can't enter (a mount that went unreadable). Before, is_dir()
        # raised, the project was skipped, and its live pid folder moved.
        victim = "44444444-4444-4444-8444-444444444444"
        external = self.state.parent / "ext"
        (external / "gamma" / ".xo").mkdir(parents=True)
        (external / "gamma" / ".xo" / "project.json").write_text(
            json.dumps({"schema": 2, "pid": victim, "name": "gamma"}), encoding="utf-8")
        (self.projects / "gamma").symlink_to(external / "gamma")
        self.runtime(victim)
        external.chmod(0)
        self.addCleanup(external.chmod, 0o755)
        self.assertEqual(self.code(victim), ("doctor_keys_unknown", 409))
        self.assertTrue((self.state / "projects" / victim).is_dir())

    def test_refuses_while_a_project_entry_cannot_be_checked(self) -> None:
        # Any error other than "not there" (a stale NFS handle, an I/O
        # error) means the project can't be read, never that it isn't one.
        self.runtime(OTHER)
        (self.projects / "on-a-stale-mount").mkdir()
        real_is_dir = Path.is_dir

        def is_dir(path: Path) -> bool:
            if path.name == "on-a-stale-mount":
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_is_dir(path)

        with patch.object(Path, "is_dir", is_dir):
            self.assertEqual(self.code(OTHER), ("doctor_keys_unknown", 409))

    def test_a_resolve_that_raises_during_the_move_is_a_move_failure(self) -> None:
        # Path.resolve() raises RuntimeError, not OSError, on a symlink loop;
        # it must become a refusal, never escape as a 500.
        self.runtime(OTHER)
        real_resolve = Path.resolve

        def resolve(path: Path, *args, **kwargs):
            if path.name == OTHER:
                raise RuntimeError("Symlink loop from 'x'")
            return real_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            self.assertEqual(self.code(OTHER), ("doctor_move_failed", 500))
        self.assertTrue((self.state / "projects" / OTHER).is_dir())

    def test_a_state_root_symlink_loop_is_refused_not_raised(self) -> None:
        shutil.rmtree(self.state)
        self.state.symlink_to(self.state)
        self.assertIn(self.code(OTHER)[0], ("doctor_gone", "doctor_move_failed"))

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


class NameSourceTests(LeftoverSandbox):
    """#188 issue 7: the name survives an emptied Space timeline."""

    def orphan_sample(self) -> None:
        shutil.rmtree(self.projects / "sample-project")   # its runtime folder PID stays behind
        (self.projects / "third-project").mkdir()          # stay under the count guard
        (self.state / "projects" / "timeline.jsonl").write_text("", encoding="utf-8")

    def clear_inbox(self) -> None:
        inbox = self.state / "inbox" / "inbox.json"
        document = json.loads(inbox.read_text(encoding="utf-8"))
        document["items"] = []
        inbox.write_text(json.dumps(document), encoding="utf-8")

    def leftover(self) -> dict:
        [finding] = [f for f in self.runtime_findings() if f["id"] == "runtime.leftover"]
        return finding

    def test_the_inbox_names_it_when_the_space_timeline_is_empty(self) -> None:
        self.orphan_sample()
        finding = self.leftover()
        self.assertIn("It belonged to project sample-project", finding["observed"])
        self.assertEqual(finding["details"]["name_source"], "the Inbox")
        self.assertEqual(finding["title"], "Project sample-project's runtime data is no longer used")

    def test_its_own_session_list_names_it(self) -> None:
        self.orphan_sample()
        self.clear_inbox()
        finding = self.leftover()
        self.assertEqual(finding["details"]["project_name"], "sample-project")
        self.assertEqual(finding["details"]["name_source"], "its session list")

    def test_a_github_repository_is_only_a_hint(self) -> None:
        self.orphan_sample()
        self.clear_inbox()
        shutil.rmtree(self.state / "projects" / PID / "sessions")
        finding = self.leftover()
        self.assertNotIn("It belonged to project", finding["observed"])
        self.assertIn("GitHub repository acme/sample-project", finding["observed"])
        self.assertEqual(finding["details"]["repository"], "acme/sample-project")
        self.assertNotIn("project_name", finding["details"])

    def test_the_leftover_explains_itself(self) -> None:
        self.runtime(OTHER)
        finding = [f for f in self.runtime_findings() if f["id"] == "runtime.leftover"][0]
        labels = {e["label"] for e in finding["evidence"]}
        self.assertTrue({"Size", "Files", "Last written", "Contains"} <= labels)
        self.assertIn("quarantine", finding["next_step"])
        self.assertEqual(finding["problem_key"], f"leftover:{OTHER}")
        self.assertEqual(finding["subject"], OTHER)  # the UI POSTs the subject

    def test_too_many_leftovers_names_both_causes(self) -> None:
        self.runtime(OTHER)
        self.runtime("33333333-3333-4333-8333-333333333333")
        [blocked] = [f for f in self.runtime_findings() if f["id"] == "runtime.too_many_leftovers"]
        self.assertIn("deleted", blocked["why_it_matters"])
        self.assertIn("projects folder", blocked["why_it_matters"])

    def test_unreadable_projects_are_listed_in_details(self) -> None:
        self.runtime(OTHER)
        (self.projects / "sample-project" / ".xo" / "project.json").write_text("{", encoding="utf-8")
        [blocked] = [f for f in self.runtime_findings() if f["id"] == "runtime.keys_unknown"]
        self.assertEqual([p["name"] for p in blocked["details"]["projects"]], ["sample-project"])


if __name__ == "__main__":
    unittest.main()
