from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.xo_projects_sync.tarball import extract_tarball


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    """Hand-build a tar.gz with exact member names, bypassing build_tarball
    (which can never emit an unsafe name — that is the point of the test)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


class ExtractTarballTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.target = self.tmp / "restored"

    def test_normal_members_extract(self) -> None:
        archive = self.tmp / "ok.tar.gz"
        _write_tar(archive, {"a.txt": b"one", "sub/b.txt": b"two"})

        extract_tarball(archive, self.target)

        self.assertEqual((self.target / "a.txt").read_bytes(), b"one")
        self.assertEqual((self.target / "sub" / "b.txt").read_bytes(), b"two")

    def test_dotdot_member_is_refused_even_when_it_resolves_inside(self) -> None:
        # "a/../b.txt" lands inside the target, so the resolve() check alone
        # allows it — but CPython 3.12.14+ refuses to create the "a/.."
        # intermediate and the extract dies mid-flight. Refuse it outright.
        archive = self.tmp / "dotdot.tar.gz"
        _write_tar(archive, {"a/../b.txt": b"sneaky"})

        with self.assertRaisesRegex(RuntimeError, r"'\.\.'"):
            extract_tarball(archive, self.target)
        self.assertFalse((self.target / "b.txt").exists())

    def test_escaping_member_is_refused(self) -> None:
        archive = self.tmp / "escape.tar.gz"
        _write_tar(archive, {"../evil.txt": b"outside"})

        with self.assertRaises(RuntimeError):
            extract_tarball(archive, self.target)
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_absolute_member_is_refused(self) -> None:
        archive = self.tmp / "absolute.tar.gz"
        _write_tar(archive, {"/tmp/abs.txt": b"outside"})

        with self.assertRaisesRegex(RuntimeError, "absolute"):
            extract_tarball(archive, self.target)


if __name__ == "__main__":
    unittest.main()
