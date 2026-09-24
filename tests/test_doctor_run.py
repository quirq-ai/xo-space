"""A doctor run: healthy baseline, error isolation, and the read-only contract."""

from __future__ import annotations

import json
import os
import shutil
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent import doctor as doctor_router
from services.doctor import checks, inventory, run
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

    def test_a_state_root_that_is_a_symlink_loop_is_one_fail_not_a_crash(self) -> None:
        # Before: Path.resolve() raised RuntimeError("Symlink loop") and GET
        # /api/doctor answered 500 with no report at all.
        shutil.rmtree(self.state)
        self.state.symlink_to(self.state)
        self.assertEqual(self.ids(self.report()), {"roots.state_unavailable"})

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

    def test_a_corrupt_file_dated_in_the_future_is_reported(self) -> None:
        path = self.state / "inbox" / "inbox.json"
        path.write_text("{corrupt", encoding="utf-8")
        ahead = self.now + 86400
        os.utime(path, (ahead, ahead))
        self.assertIn("read.invalid_json", self.ids())

    def test_an_empty_rebuildable_file_warns(self) -> None:
        (self.state / "cache" / "stats.json").write_text("", encoding="utf-8")
        finding = self.finding("read.empty")
        self.assertEqual(finding["level"], "WARN")
        self.assertIn("deleting it is safe", finding["why_it_matters"])
        self.assertNotIn("after a minute", finding["why_it_matters"])

    def test_wrong_type(self) -> None:
        # accounts.json is a cache the provider re-resolves (investigation Appendix A): WARN, not FAIL.
        (self.state / "connections" / "accounts.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.finding("read.wrong_type")["level"], "WARN")

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

    def _write_agent_json(self, document: dict) -> None:
        path = self.projects / "sample-project" / ".xo" / "agent.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        old = self.now - 86400
        os.utime(path, (old, old))

    def test_an_unstamped_agent_json_is_healthy(self) -> None:
        # agent.schema.json is the one schema that does not require `schema`:
        # records written before the stamp existed are still on disk and every
        # adapter reads them.
        self._write_agent_json({"id": "sample-project", "name": "Sample",
                                "backend": "some-backend", "created_at": "2025-01-01T00:00:00+00:00"})
        self.assertEqual(self.problems(), [])

    def test_a_stamped_agent_json_is_healthy(self) -> None:
        self._write_agent_json({"$schema": "xo/agent.schema.json", "schema": 1, "id": "sample-project"})
        self.assertEqual(self.problems(), [])

    def test_an_agent_json_from_a_newer_xo_space_is_still_unsupported(self) -> None:
        self._write_agent_json({"schema": 99, "id": "sample-project"})
        self.assertIn("schema.unsupported", self.ids())

    def test_a_malformed_agent_json_stamp_is_still_unsupported(self) -> None:
        # Only an absent stamp is legitimate; a present but wrong-typed one is not.
        for stamp in ("1", None, True):
            with self.subTest(stamp=stamp):
                self._write_agent_json({"schema": stamp, "id": "sample-project"})
                self.assertIn("schema.unsupported", self.ids())

    def test_an_unstamped_peers_json_is_accepted(self) -> None:
        # The peers store accepts a missing stamp (atomic_write.read_stamped_document), so this is not a problem.
        path = self.projects / "sample-project" / ".xo" / "peers.json"
        path.write_text(json.dumps({"peers": []}), encoding="utf-8")
        old = self.now - 86400
        os.utime(path, (old, old))
        self.assertNotIn("schema.unsupported", self.ids())

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

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_an_unlistable_state_subfolder_fails(self) -> None:
        folder = self.state / "inbox"
        folder.chmod(0o000)
        self.addCleanup(folder.chmod, 0o755)
        finding = self.finding("read.unreadable")
        self.assertEqual((finding["level"], finding["subject"]), ("FAIL", "inbox"))

    def test_a_truncated_walk_is_a_failure_not_a_warning(self) -> None:
        real_walk = inventory.walk_files
        with patch.object(inventory, "walk_files", lambda root, limit=2: real_walk(root, limit)):
            report = self.report()
        truncated = [f for c in report["checks"] for f in c["findings"] if f["id"] == "read.too_large"]
        self.assertEqual([f["level"] for f in truncated], ["FAIL"])


class ReportSizeTests(DoctorSandbox):
    def _corrupt_shards(self, count: int) -> None:
        shards = self.state / "projects" / PID / "sessions" / "sessionslist.d"
        shards.mkdir(parents=True, exist_ok=True)
        old = self.now - 86400
        for i in range(count):
            path = shards / f"s{i}.json"
            path.write_text("{not json", encoding="utf-8")
            os.utime(path, (old, old))

    def test_findings_are_capped_with_a_rollup(self) -> None:
        self._corrupt_shards(run.MAX_FINDINGS_PER_CHECK + 50)
        read = [c for c in self.report()["checks"] if c["id"] == "read"][0]
        self.assertEqual(len(read["findings"]), run.MAX_FINDINGS_PER_CHECK + 1)
        rollup = read["findings"][-1]
        self.assertEqual(rollup["id"], "read.truncated")
        self.assertEqual(rollup["details"]["dropped"], 50)

    def test_a_capped_check_keeps_its_worst_level(self) -> None:
        self._corrupt_shards(run.MAX_FINDINGS_PER_CHECK + 50)
        read = [c for c in self.report()["checks"] if c["id"] == "read"][0]
        self.assertEqual(read["level"], "FAIL")

    def test_an_uncapped_check_gains_no_rollup(self) -> None:
        self._corrupt_shards(3)
        ids = [f["id"] for c in self.report()["checks"] for f in c["findings"]]
        self.assertNotIn("read.truncated", ids)

    def test_a_truncated_walk_survives_the_cap_alongside_105_corrupt_shards(self) -> None:
        # 105 corrupt KEEP shards are already >MAX_FINDINGS_PER_CHECK FAILs on
        # their own; read.too_large must not be pushed past the cap by them.
        self._corrupt_shards(105)
        real_walk = inventory.walk_files

        def force_truncated(root, limit=inventory.MAX_WALK_ENTRIES):
            found, _truncated, unreadable = real_walk(root, limit)
            return found, True, unreadable

        with patch.object(inventory, "walk_files", force_truncated):
            report = self.report()
        read = [c for c in report["checks"] if c["id"] == "read"][0]
        ids = [f["id"] for f in read["findings"]]
        self.assertIn("read.too_large", ids)


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


class HostileFileTests(DoctorSandbox):
    """When things go wrong on disk the doctor still reports, promptly and
    completely: it never waits on a file, never reads one without limit, and
    never produces a report the HTTP response can't send."""

    def report_within(self, seconds: float = 10) -> dict:
        box: list = []
        worker = threading.Thread(target=lambda: box.append(self.report()), daemon=True)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            self.fail(f"the doctor run was still going after {seconds}s")
        return box[0]

    def findings_for(self, report: dict, subject: str) -> list[tuple[str, str]]:
        return [(f["id"], f["level"]) for c in report["checks"] for f in c["findings"] if f["subject"] == subject]

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_pipe_in_place_of_project_json_is_reported_not_waited_on(self) -> None:
        path = self.projects / "sample-project" / ".xo" / "project.json"
        path.unlink()
        os.mkfifo(path)
        report = self.report_within()
        self.assertEqual(self.findings_for(report, "sample-project/.xo/project.json"), [("read.special", "FAIL")])
        self.assertEqual(report["summary"]["ERROR"], 0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_pipe_in_place_of_the_heartbeat_is_not_waited_on(self) -> None:
        path = self.state / "cache" / "heartbeat.json"
        path.unlink()
        os.mkfifo(path)
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "true"}):
            report = self.report_within()
        self.assertIn("watcher.heartbeat", self.ids(report))

    @unittest.skipUnless(os.path.exists("/dev/null"), "needs /dev/null")
    def test_a_link_to_a_device_is_reported_not_read(self) -> None:
        # /dev/null, not /dev/zero: both are character devices, so the
        # guard is exercised the same way, but if it ever regresses this
        # reads nothing instead of reading without end and exhausting the
        # machine's memory (which a /dev/zero version of this test did).
        path = self.projects / "sample-project" / ".xo" / "agent.json"
        path.symlink_to("/dev/null")
        report = self.report_within()
        self.assertEqual(self.findings_for(report, "sample-project/.xo/agent.json"), [("read.special", "FAIL")])

    def test_an_oversized_keep_file_is_reported_not_read(self) -> None:
        path = self.state / "inbox" / "inbox.json"
        with patch("services.doctor.reading.MAX_READ_BYTES", 16):
            report = self.report()
        self.assertEqual(self.findings_for(report, "inbox/inbox.json"), [("read.file_too_large", "FAIL")])
        self.assertTrue(path.is_file())

    def test_json_nested_too_deeply_is_one_finding_not_an_error(self) -> None:
        # Before: a RecursionError escaped the reader and turned every check
        # that lists projects into ERROR, hiding which file was bad.
        path = self.projects / "sample-project" / ".xo" / "project.json"
        path.write_text("[" * 100_000, encoding="utf-8")
        old = self.now - 86400
        os.utime(path, (old, old))
        report = self.report()
        self.assertEqual(report["summary"]["ERROR"], 0)
        self.assertEqual(self.findings_for(report, "sample-project/.xo/project.json"), [("read.invalid_json", "FAIL")])

    def get_over_http(self) -> tuple[int, dict]:
        app = FastAPI()
        app.include_router(doctor_router.router)
        response = TestClient(app, raise_server_exceptions=False).get("/api/doctor")
        return response.status_code, (response.json() if response.status_code == 200 else {})

    def test_a_file_name_that_is_not_utf8_still_gives_a_report(self) -> None:
        # Before: the name reached the report as lone surrogates, the JSON
        # response refused to encode them and GET /api/doctor answered 500.
        try:
            (self.state / os.fsdecode(b"bad\xff.json")).write_text("{}", encoding="utf-8")
        except (OSError, UnicodeError):
            self.skipTest("this filesystem refuses names that are not UTF-8")
        status, report = self.get_over_http()
        self.assertEqual(status, 200)
        subjects = [f["subject"] for c in report["checks"] for f in c["findings"]]
        self.assertIn("bad\\xff.json", subjects)

    def test_a_timeline_name_with_a_lone_surrogate_still_gives_a_report(self) -> None:
        other = "11111111-1111-4111-8111-111111111111"
        (self.state / "projects" / other).mkdir()
        (self.state / "projects" / other / "stats.json").write_text('{"schema": 2}', encoding="utf-8")
        with open(self.state / "projects" / "timeline.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": other, "project_id": "gone\ud800"}) + "\n")
        status, report = self.get_over_http()
        self.assertEqual(status, 200)
        names = [f["details"].get("project_name") for c in report["checks"] for f in c["findings"]
                 if f["id"] == "runtime.leftover"]
        self.assertEqual(names, ["gone\\ud800"])


if __name__ == "__main__":
    unittest.main()
