"""Content the owning store can't use is reported, in the store's own words."""

from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import peers_store, todos_store, workitems_store
from services.doctor import content
from services.inbox import store as inbox_store
from tests.doctor_sandbox import DoctorSandbox
from utils.commands import scheduler


class ValidatorSeamTests(unittest.TestCase):
    def test_every_validator_exists_and_is_callable(self) -> None:
        for (base, pattern), (module_name, function) in content.VALIDATORS.items():
            with self.subTest(file=f"{base}:{pattern}"):
                self.assertTrue(callable(getattr(importlib.import_module(module_name), function, None)))


class ContentSandbox(DoctorSandbox):
    def edit(self, path: Path, change) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    def xo(self, name: str) -> Path:
        return self.projects / "sample-project" / ".xo" / name

    def content_findings(self) -> list[dict]:
        return [f for c in self.report()["checks"] if c["id"] == "content" for f in c["findings"]]

    def one(self) -> dict:
        found = self.content_findings()
        self.assertEqual(len(found), 1, found)
        return found[0]

    def problem(self, finding: dict) -> str:
        return {e["label"]: e["value"] for e in finding["evidence"]}["Problem"]


class ContentTests(ContentSandbox):
    def test_the_samples_are_usable(self) -> None:
        self.assertEqual(self.content_findings(), [])

    def test_a_todo_list_whose_sessions_are_a_list(self) -> None:
        self.edit(self.xo("todos.json"), lambda d: d.update(sessions=[]))
        finding = self.one()
        self.assertEqual((finding["id"], finding["level"], finding["subject"]),
                         ("content.wrong_shape", "FAIL", "sample-project/.xo/todos.json"))
        self.assertEqual(finding["title"], "Project sample-project's todo list holds data its store can't use")
        self.assertEqual(self.problem(finding), "sessions is a list, expected object")
        self.assertEqual(finding["problem_key"], "file:sample-project/.xo/todos.json")
        self.assertIn("Don't delete it", finding["next_step"])

    def test_work_items_whose_items_are_a_list(self) -> None:
        self.edit(self.xo("workitems.json"), lambda d: d.update(items=[]))
        self.assertEqual(self.problem(self.one()), "items is a list, expected object")

    def test_a_collaborator_listed_twice(self) -> None:
        peer = {"user_id": "u1", "role": "editor"}
        self.edit(self.xo("peers.json"), lambda d: d.update(peers=[peer, peer]))
        self.assertIn("more than once", self.problem(self.one()))

    def test_saved_commands_whose_jobs_are_a_list(self) -> None:
        self.edit(self.state / "scheduler" / "jobs.json", lambda d: d.update(jobs=[]))
        finding = self.one()
        self.assertEqual((finding["subject"], finding["level"]), ("scheduler/jobs.json", "FAIL"))
        self.assertIn("No scheduled command runs", finding["consequence"])

    def test_a_schedule_with_no_jobs_object(self) -> None:
        self.edit(self.state / "scheduler" / "state.json", lambda d: d.pop("jobs"))
        self.assertEqual(self.problem(self.one()), "no jobs object")

    def test_inbox_items_that_are_not_a_list(self) -> None:
        self.edit(self.state / "inbox" / "inbox.json", lambda d: d.update(items="oops"))
        finding = self.one()
        self.assertEqual((finding["subject"], finding["level"]), ("inbox/inbox.json", "FAIL"))
        self.assertIn("marking items done, deleting them or adding notes fails", finding["consequence"])
        self.assertIn("leaves a file it can't parse alone", finding["self_repair"])

    def test_an_inbox_with_no_items_yet_is_fine(self) -> None:
        self.edit(self.state / "inbox" / "inbox.json", lambda d: d.pop("items"))
        self.assertEqual(self.content_findings(), [])

    def test_a_file_that_does_not_parse_is_only_the_read_checks(self) -> None:
        self.xo("todos.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.content_findings(), [])
        self.assertIn("read.invalid_json", self.ids())

    def test_a_validator_that_cannot_be_imported_costs_only_this_check(self) -> None:
        broken = {("state", "inbox/inbox.json"): ("services.inbox.store", "renamed_away")}
        with patch.object(content, "VALIDATORS", broken):
            report = self.report()
        checks = {c["id"]: c for c in report["checks"]}
        self.assertEqual(checks["content"]["level"], "ERROR")
        self.assertEqual(checks["read"]["level"], "OK")


class StoreAgreementTests(ContentSandbox):
    """The doctor reports exactly what the store refuses with."""

    def test_the_store_refuses_with_the_same_reason(self) -> None:
        cases = [
            ("todos.json", lambda d: d.update(sessions=[]),
             lambda p: todos_store.create_todo(p, runtime="r", content="x")),
            ("workitems.json", lambda d: d.update(items=[]), workitems_store.list_workitems),
            ("peers.json", lambda d: d.update(peers={}), peers_store.list_peers),
        ]
        for name, change, use in cases:
            with self.subTest(file=name):
                original = self.xo(name).read_text(encoding="utf-8")
                self.edit(self.xo(name), change)
                reason = self.problem(self.one())
                with self.assertRaises(Exception) as caught:
                    use(self.xo(name))
                self.assertIn(reason, str(caught.exception))
                self.xo(name).write_text(original, encoding="utf-8")

    def test_the_scheduler_refuses_what_the_doctor_reports(self) -> None:
        self.edit(self.state / "scheduler" / "jobs.json", lambda d: d.update(jobs=[]))
        self.one()
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.list_jobs()

    def test_the_inbox_store_refuses_what_the_doctor_reports(self) -> None:
        self.edit(self.state / "inbox" / "inbox.json", lambda d: d.update(items="oops"))
        self.one()
        document, ok = inbox_store.load_document()
        self.assertFalse(ok)
        self.assertEqual(document["items"], [])


if __name__ == "__main__":
    unittest.main()
