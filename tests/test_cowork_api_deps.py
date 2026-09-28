"""cowork-api.sh — start converges the venv on requirements.txt (#184).

An update moves code, not the venv. ``sync_requirements_if_changed`` runs on
every start and installs requirements.txt when its hash differs from the
stamp the last sync wrote (``venv/.requirements.sha256``, also written by
install.sh), so a restart after an update that added a dependency still
boots. Drives the real function against a temp checkout with stub ``uv`` and
``venv/bin/python`` that only record their arguments: no network, no venv.

Needs bash on a POSIX host; skipped elsewhere.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name == "posix" else None
DISPATCH = 'case "${1:-restart}" in'

_RECORDER = '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$CALLS"\nexit "${STUB_EXIT:-0}"\n'


@unittest.skipUnless(BASH, "needs bash on a POSIX host")
class SyncRequirementsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        script = (ROOT / "cowork-api.sh").read_text(encoding="utf-8")
        # Everything up to the command dispatch, then call the one function.
        script = script[:script.index(DISPATCH)] + "sync_requirements_if_changed\n"
        self.runner = self.root / "cowork-api.sh"
        self.runner.write_text(script, encoding="utf-8")
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("fastapi\n", encoding="utf-8")
        self.stamp = self.root / "venv" / ".requirements.sha256"
        self.calls = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self._stub(self.root / "venv" / "bin" / "python")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_RECORDER, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, **extra: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
               "CALLS": str(self.calls), **extra}
        return subprocess.run([BASH, str(self.runner)], cwd=str(self.root), env=env,
                              capture_output=True, text=True, timeout=30)

    def _calls(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def _hash(self) -> str:
        return hashlib.sha256(self.requirements.read_bytes()).hexdigest()

    def test_syncs_once_per_requirements_change_with_uv(self) -> None:
        self._stub(self.bin / "uv")
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(len(self._calls()), 1)
        self.assertIn("uv pip install", self._calls()[0])
        self.assertEqual(self.stamp.read_text(encoding="utf-8").strip(), self._hash())

        self._run()
        self.assertEqual(len(self._calls()), 1, "an unchanged requirements.txt must not resync")

        self.requirements.write_text("fastapi\nmcp\n", encoding="utf-8")
        self._run()
        self.assertEqual(len(self._calls()), 2)
        self.assertEqual(self.stamp.read_text(encoding="utf-8").strip(), self._hash())

    def test_finds_uv_in_local_bin_when_not_on_path(self) -> None:
        self._stub(self.home / ".local" / "bin" / "uv")
        self._run()
        self.assertEqual([c.split()[0] for c in self._calls()], ["uv"])

    def test_falls_back_to_the_venv_pip_without_uv(self) -> None:
        self._run()
        self.assertEqual(len(self._calls()), 1)
        self.assertTrue(self._calls()[0].startswith("python -m pip install"))

    def test_failed_sync_starts_anyway_and_retries_next_time(self) -> None:
        self._stub(self.bin / "uv")
        result = self._run(STUB_EXIT="1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Dependency sync failed", result.stdout)
        self.assertFalse(self.stamp.exists())
        self._run()
        self.assertEqual(len(self._calls()), 2)
        self.assertTrue(self.stamp.exists())

    def test_no_venv_means_nothing_to_sync(self) -> None:
        shutil.rmtree(self.root / "venv")
        self._stub(self.bin / "uv")
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(self._calls(), [])


if __name__ == "__main__":
    unittest.main()
