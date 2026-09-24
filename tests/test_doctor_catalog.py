"""The catalog: every parsed file is described, and no two read alike."""

from __future__ import annotations

import re
import unittest

from services.doctor import catalog, inventory
from services.doctor.model import FAIL, WARN
from services.doctor.reading import ReadResult


class CatalogCoverageTests(unittest.TestCase):
    def test_every_parsed_pattern_has_an_entry_and_nothing_else_does(self) -> None:
        parsed = {spec.pattern for spec in inventory.SPECS if spec.parsed}
        self.assertEqual(set(catalog.ABOUT), parsed)

    def test_no_two_files_share_a_consequence(self) -> None:
        seen: dict[str, str] = {}
        for pattern, about in catalog.ABOUT.items():
            self.assertNotIn(about.consequence, seen, f"{pattern} reads like {seen.get(about.consequence)}")
            seen[about.consequence] = pattern

    def test_every_entry_answers_all_three_questions(self) -> None:
        for pattern, about in catalog.ABOUT.items():
            for part in ("name", "owner", "consequence", "self_repair", "next_step"):
                with self.subTest(pattern=pattern, part=part):
                    self.assertTrue(getattr(about, part).strip())

    def test_placeholders_are_only_project_and_toolkit(self) -> None:
        for pattern, about in catalog.ABOUT.items():
            texts = [about.name, about.consequence, about.self_repair, about.next_step]
            texts += [t for o in about.overrides.values() for t in o.values()]
            for text in texts:
                self.assertEqual(set(re.findall(r"{(\w*)}", text)) - {"project", "toolkit"}, set(), pattern)

    def test_the_v1_generic_sentences_are_gone(self) -> None:
        for about in catalog.ABOUT.values():
            self.assertNotIn("exists nowhere else", about.consequence)
            self.assertNotIn("deleting it is safe", about.next_step.lower())


class LevelPolicyTests(unittest.TestCase):
    def spec(self, rel: str, base: str = inventory.STATE) -> inventory.Spec:
        return inventory.spec_for(base, rel)

    def test_levels_follow_the_behaviour(self) -> None:
        self.assertEqual(catalog.level_for(self.spec("scheduler/jobs.json"), "invalid_json", watcher_alive=True), FAIL)
        self.assertEqual(catalog.level_for(self.spec("cache/stats.json"), "empty", watcher_alive=True), WARN)
        self.assertEqual(catalog.level_for(self.spec("connections/gmail/state.json"), "empty", watcher_alive=True), WARN)

    def test_a_read_position_depends_on_the_watcher(self) -> None:
        spec = self.spec("projects/offsets.json")
        self.assertEqual(catalog.level_for(spec, "empty", watcher_alive=True), WARN)
        self.assertEqual(catalog.level_for(spec, "empty", watcher_alive=False), FAIL)

    def test_per_outcome_overrides(self) -> None:
        self.assertEqual(catalog.level_for(self.spec("usage/x.json"), "wrong_type", watcher_alive=True), FAIL)
        self.assertEqual(catalog.level_for(self.spec("usage/x.json"), "empty", watcher_alive=True), WARN)
        self.assertEqual(catalog.level_for(self.spec("inbox/inbox.json"), "empty", watcher_alive=True), WARN)
        self.assertEqual(catalog.level_for(self.spec("agent.json", inventory.PROJECT), "invalid_json",
                                           watcher_alive=True), WARN)


class LabelTests(unittest.TestCase):
    def test_labels_fill_project_and_toolkit(self) -> None:
        stats = inventory.spec_for(inventory.STATE, "projects/p/stats.json")
        labels = catalog.labels(stats, "projects/abc/stats.json", lambda key: {"abc": "chromium"}[key])
        self.assertEqual(catalog.fill(catalog.about(stats).name, labels), "Project chromium's usage record")
        conn = inventory.spec_for(inventory.STATE, "connections/gmail/config.json")
        labels = catalog.labels(conn, "connections/gmail/config.json", lambda key: key)
        self.assertEqual(catalog.fill(catalog.about(conn).name, labels), "Gmail's connection settings file")
        todos = inventory.spec_for(inventory.PROJECT, "todos.json")
        labels = catalog.labels(todos, "brain/.xo/todos.json", lambda key: key)
        self.assertEqual(catalog.fill(catalog.about(todos).name, labels), "Project brain's todo list")

    def test_fill_never_raises_on_a_missing_label(self) -> None:
        self.assertEqual(catalog.fill("Project {project}'s x", {}), "Project ?'s x")


class EvidenceTests(unittest.TestCase):
    def test_read_evidence_lists_owner_size_time_and_problem(self) -> None:
        about = catalog.about(inventory.spec_for(inventory.STATE, "inbox/inbox.json"))
        result = ReadResult("invalid_json", "Expecting ',' delimiter at line 13 column 9", size=2048, mtime=1000.0)
        rows = {row["label"]: row["value"] for row in catalog.read_evidence(about, result, None, 1180.0, {})}
        self.assertEqual(rows["Owned by"], "the Inbox")
        self.assertEqual(rows["Size"], "2 KB")
        self.assertIn("3 minutes ago", rows["Last modified"])
        self.assertEqual(rows["Problem"], "Expecting ',' delimiter at line 13 column 9")

    def test_schema_evidence_names_found_and_accepted(self) -> None:
        about = catalog.about(inventory.spec_for(inventory.PROJECT, "workitems.json"))
        result = ReadResult("schema_unsupported", "older", schema=1)
        rows = {row["label"]: row["value"] for row in catalog.read_evidence(about, result, frozenset({2}), 0.0, {})}
        self.assertEqual(rows["Format version"], "1 (this xo-space reads 2)")


if __name__ == "__main__":
    unittest.main()
