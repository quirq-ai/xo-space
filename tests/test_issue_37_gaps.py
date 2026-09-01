"""Regression coverage for the contracts clarified in issue #37."""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters import loader
from services.cowork_agent.project_layout import relative_path_suffix
from services.cowork_agent.xo_projects_sync.tarball import extract_tarball


class RelativePathSuffixTests(unittest.TestCase):
    def test_preserves_posix_path_suffix_rules(self) -> None:
        self.assertEqual(relative_path_suffix("docs/guide.MD"), ".MD")
        self.assertEqual(relative_path_suffix(".env"), "")


class CapabilityLoaderTests(unittest.TestCase):
    def test_existing_capability_with_missing_dependency_fails_loudly(self) -> None:
        missing = ModuleNotFoundError("No module named 'fcntl'")
        missing.name = "fcntl"

        with patch.object(loader.importlib, "import_module", side_effect=missing):
            with self.assertRaisesRegex(ModuleNotFoundError, "fcntl"):
                loader.try_load_capability("routes", agent="demo")

    def test_missing_capability_module_remains_optional(self) -> None:
        expected = "services.cowork_agent.adapters.demo.routes"
        missing = ModuleNotFoundError(f"No module named '{expected}'")
        missing.name = expected

        with patch.object(loader.importlib, "import_module", side_effect=missing):
            self.assertIsNone(loader.try_load_capability("routes", agent="demo"))


class TarballExtractionTests(unittest.TestCase):
    def test_refuses_an_internal_dotdot_member_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            payload = b"must not be written"
            member = tarfile.TarInfo("a/../b.txt")
            member.size = len(payload)
            with tarfile.open(tarball, "w:gz") as tar:
                tar.addfile(member, io.BytesIO(payload))

            with self.assertRaisesRegex(RuntimeError, "contains '..'"):
                extract_tarball(tarball, target)

            self.assertFalse((target / "b.txt").exists())


if __name__ == "__main__":
    unittest.main()
