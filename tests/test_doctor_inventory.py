"""The doctor's inventory is held to what this xo-space ships.

The fixtures are the source of truth for file names and schema versions, but
they do not ship (.dockerignore excludes tests/), so the inventory carries its
own table and these tests keep it equal to the fixtures and the schema files.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from services import xo_structure
from services.doctor import inventory

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT = ROOT / "tests" / "fixtures" / "xo-project" / ".xo"


def _fixture_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*") if p.is_file() and p.name != "README.md")


class InventoryCoversTheFixtures(unittest.TestCase):
    def test_every_state_fixture_file_has_a_class(self) -> None:
        for path in _fixture_files(STATE):
            rel = path.relative_to(STATE).as_posix()
            with self.subTest(file=rel):
                self.assertIsNotNone(inventory.spec_for(inventory.STATE, rel))

    def test_every_canonical_and_optional_project_file_has_a_class(self) -> None:
        for name in (*xo_structure.CANONICAL_FILES, *xo_structure.OPTIONAL_FILES):
            with self.subTest(file=name):
                self.assertIsNotNone(inventory.spec_for(inventory.PROJECT, name))
                self.assertIn(name, inventory.names(inventory.PROJECT))

    def test_fixture_schema_numbers_are_accepted(self) -> None:
        pairs = [(inventory.STATE, p.relative_to(STATE).as_posix(), p) for p in _fixture_files(STATE)]
        pairs += [(inventory.PROJECT, p.name, p) for p in _fixture_files(PROJECT)]
        for base, rel, path in pairs:
            spec = inventory.spec_for(base, rel)
            if spec.klass == inventory.UNPARSED:
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            accepted = inventory.accepted(spec)
            with self.subTest(file=rel):
                if accepted is None:
                    self.assertNotIn("schema", document, "a stamped file must not be exempt")
                    continue
                self.assertIn(document["schema"], accepted)
                if spec.versions is not None:
                    # The table follows the fixture: the sample is the newest version.
                    self.assertEqual(document["schema"], max(spec.versions))

    def test_every_referenced_schema_file_declares_its_versions(self) -> None:
        for spec in inventory.SPECS:
            if spec.schema_file:
                with self.subTest(schema=spec.schema_file):
                    self.assertTrue(inventory.schema_file_versions(spec.schema_file))

    def test_patterns_match_one_segment_or_many(self) -> None:
        self.assertIsNotNone(inventory.spec_for(inventory.STATE, "sharing/github.com__a__b-1234abcd.json"))
        self.assertEqual(inventory.spec_for(inventory.STATE, "sharing/removed/x.json").pattern, "sharing/removed/*.json")
        self.assertIsNone(inventory.spec_for(inventory.STATE, "inbox/other.json"))
        self.assertEqual(inventory.spec_for(inventory.STATE, "logs/a/b/c.log").klass, inventory.UNPARSED)
        self.assertEqual(inventory.spec_for(inventory.STATE, "projects/p1/stats.json").klass, inventory.KEEP)
        self.assertIsNone(inventory.spec_for(inventory.STATE, "projects/p1/x/stats.json"))


class WalkFilesTests(unittest.TestCase):
    def test_regular_files_only_and_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "f.json").write_text("{}")
            (root / "g.json").write_text("{}")
            (root / "link.json").symlink_to(root / "g.json")
            files, truncated, unreadable = inventory.walk_files(root)
            self.assertEqual(sorted(p.relative_to(root).as_posix() for p in files), ["a/f.json", "g.json"])
            self.assertFalse(truncated)
            self.assertEqual(unreadable, [])
            self.assertTrue(inventory.walk_files(root, limit=1)[1])

    def test_directories_count_toward_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create 5 nested directories with no files
            (root / "d1").mkdir()
            (root / "d2").mkdir()
            (root / "d3").mkdir()
            (root / "d4").mkdir()
            (root / "d5").mkdir()
            # With limit=3, should hit truncation before visiting all directories
            files, truncated, unreadable = inventory.walk_files(root, limit=3)
            self.assertEqual(files, [])
            self.assertTrue(truncated)
            # With default limit, should complete without truncation
            files, truncated, unreadable = inventory.walk_files(root)
            self.assertEqual(files, [])
            self.assertFalse(truncated)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_an_unlistable_subfolder_is_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "blocked"
            blocked.mkdir()
            (blocked / "f.json").write_text("{}")
            (root / "g.json").write_text("{}")
            blocked.chmod(0o000)
            try:
                files, truncated, unreadable = inventory.walk_files(root)
                self.assertEqual([p.relative_to(root).as_posix() for p in files], ["g.json"])
                self.assertFalse(truncated)
                self.assertEqual([p.relative_to(root).as_posix() for p in unreadable], ["blocked"])
            finally:
                blocked.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
