"""Regression coverage for the contracts clarified in issue #37."""
from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routers.cowork_agent import xo_projects_sync as sync_router
from services.cowork_agent.adapters import loader
from services.cowork_agent.project_layout import relative_path_suffix
from services.cowork_agent.xo_projects_sync.tarball import extract_tarball


def _add_file(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    tar.addfile(member, io.BytesIO(payload))


def _add_symlink(tar: tarfile.TarFile, name: str, target: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = target
    tar.addfile(member)


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
    def test_preserves_relative_and_absolute_symlinks_without_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            with tarfile.open(tarball, "w:gz") as tar:
                _add_symlink(tar, "shared", "../common")
                _add_symlink(tar, "venv/bin/python", "/usr/bin/python3")

            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                extract_tarball(tarball, target)

            self.assertEqual(os.readlink(target / "shared"), "../common")
            self.assertEqual(os.readlink(target / "venv/bin/python"), "/usr/bin/python3")

    def test_refuses_an_internal_dotdot_member_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            with tarfile.open(tarball, "w:gz") as tar:
                _add_file(tar, "a/../b.txt", b"must not be written")

            with self.assertRaisesRegex(RuntimeError, "contains '..'"):
                extract_tarball(tarball, target)

            self.assertFalse((target / "b.txt").exists())

    def test_refuses_an_absolute_member_name_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            with tarfile.open(tarball, "w:gz") as tar:
                _add_file(tar, "/outside.txt", b"must not be written")

            with self.assertRaisesRegex(RuntimeError, "is absolute"):
                extract_tarball(tarball, target)

            self.assertFalse((root / "outside.txt").exists())

    def test_refuses_a_hard_link_target_outside_the_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            with tarfile.open(tarball, "w:gz") as tar:
                member = tarfile.TarInfo("linked.txt")
                member.type = tarfile.LNKTYPE
                member.linkname = "../outside.txt"
                tar.addfile(member)

            with self.assertRaisesRegex(RuntimeError, "hard-link target.*contains '..'"):
                extract_tarball(tarball, target)

            self.assertFalse((root / "outside.txt").exists())

    def test_refuses_a_member_that_writes_through_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            outside = root / "outside"
            with tarfile.open(tarball, "w:gz") as tar:
                _add_symlink(tar, "linked", "../outside")
                _add_file(tar, "linked/payload.txt", b"must not be written")

            with self.assertRaisesRegex(RuntimeError, "writes through a symlink"):
                extract_tarball(tarball, target)

            self.assertFalse((outside / "payload.txt").exists())
            self.assertFalse((target / "linked").exists())

    def test_converts_tarfile_filter_errors_to_runtime_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tarball = root / "snapshot.tar.gz"
            target = root / "target"
            with tarfile.open(tarball, "w:gz") as tar:
                _add_file(tar, "safe.txt", b"safe")

            with patch.object(
                tarfile.TarFile,
                "extractall",
                side_effect=tarfile.FilterError("blocked by filter"),
            ):
                with self.assertRaisesRegex(RuntimeError, "blocked by filter"):
                    extract_tarball(tarball, target)


class RestoreRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_errors_use_the_restore_failed_shape(self) -> None:
        with patch.object(
            sync_router,
            "_require_config_and_auth",
            new=AsyncMock(return_value=(object(), object(), "owner")),
        ), patch.object(
            sync_router.restore_mod,
            "restore_one",
            new=AsyncMock(side_effect=RuntimeError("unsafe tarball")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await sync_router.restore_project("demo")

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(
            raised.exception.detail,
            {"error": "restore_failed", "detail": "unsafe tarball"},
        )


if __name__ == "__main__":
    unittest.main()
