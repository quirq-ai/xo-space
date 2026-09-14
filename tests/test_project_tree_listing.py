"""Project browsing lists one level without misleading raw child counts."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routers.cowork_agent.bff.xo_projects import project_tree
from services.cowork_agent import project_layout


class ProjectTreeListingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "sample"
        self.project.mkdir()
        root_patch = patch.object(project_layout, "xo_projects_root", return_value=self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_parent_listing_never_enumerates_child_directories(self) -> None:
        child = self.project / "dependencies"
        child.mkdir()
        for index in range(12):
            (child / f"file-{index}.txt").write_text("fixture", encoding="utf-8")
        (self.project / "README.md").write_text("project", encoding="utf-8")
        scanned: list[Path] = []
        original = os.scandir

        def one_level(path):
            directory = Path(path)
            scanned.append(directory)
            self.assertNotEqual(directory, child, "parent browsing must not scan child contents")
            return original(path)

        with patch.object(os, "scandir", side_effect=one_level):
            result = project_layout.list_project_tree("sample")
        self.assertNotIn(child, scanned)
        self.assertEqual(result["dirs"][0]["relative_path"], "dependencies")
        self.assertNotIn("entries", result["dirs"][0])
        self.assertEqual(result["files"][0]["relative_path"], "README.md")
        self.assertEqual(result["files"][0]["size_bytes"], 7)
        self.assertIsInstance(result["files"][0]["modified_at"], float)

    def test_hidden_only_folder_has_unknown_count_and_empty_visible_listing(self) -> None:
        folder = self.project / "private"
        folder.mkdir()
        (folder / ".hidden").write_text("fixture", encoding="utf-8")
        (folder / "scratch.tmp").write_text("fixture", encoding="utf-8")
        (folder / ".internal").mkdir()
        parent = project_tree("sample")
        self.assertEqual(len(parent.dirs), 1)
        self.assertEqual(parent.dirs[0].name, "private")
        self.assertIsNone(parent.dirs[0].entries)
        self.assertIn("entries", parent.dirs[0].model_dump(), "optional wire field stays compatible")
        child = project_tree("sample", "private")
        self.assertEqual(child.relative_path, "private")
        self.assertEqual(child.parent_relative_path, "")
        self.assertEqual(child.dirs, [])
        self.assertEqual(child.files, [])

    def test_navigating_a_child_keeps_visible_files_and_breadcrumbs(self) -> None:
        folder = self.project / "src" / "nested"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text("pass\n", encoding="utf-8")
        child = project_tree("sample", "src/nested")
        self.assertEqual(child.project_id, "sample")
        self.assertEqual(child.relative_path, "src/nested")
        self.assertEqual(child.parent_relative_path, "src")
        self.assertEqual(len(child.files), 1)
        self.assertEqual(child.files[0].relative_path, "src/nested/main.py")
        self.assertEqual(child.files[0].size_bytes, 5)
        self.assertTrue(child.files[0].modified_at)


if __name__ == "__main__":
    unittest.main()
