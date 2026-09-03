"""uninstall.sh — the functions that decide what gets removed.

Runs ``tests/uninstall_sh_harness.sh``, which sources ``uninstall.sh`` minus
its final ``main "$@"`` and drives ``resolve_repo_dir``, ``resolve_roots``,
the ``remove_path`` guards and ``remove_checkout`` against temp directories,
plus one full ``--yes`` run of a script copy inside a fabricated managed
install. ``lsof`` and ``docker`` are shadowed with no-op fakes and the
daemon tmp dir is redirected, so nothing on the real machine is inspected,
killed, or removed.

Needs bash on a POSIX host (git for the dirty-checkout case); skipped
elsewhere. The harness prints one PASS/FAIL line per case.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "uninstall_sh_harness.sh"
BASH = shutil.which("bash") if os.name == "posix" else None


class UninstallShTests(unittest.TestCase):
    @unittest.skipUnless(BASH and shutil.which("git"), "needs bash and git on a POSIX host")
    def test_harness_passes(self) -> None:
        result = subprocess.run(
            [BASH, str(HARNESS)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode, 0,
            "uninstall.sh harness failed:\n" + result.stdout + result.stderr,
        )
        self.assertNotIn("FAIL", result.stdout)

    def test_projects_are_never_purged_without_a_terminal(self) -> None:
        """Text-level: the typed-confirmation purge must stay tty-gated, and
        every deletion must funnel through remove_path (one guard point)."""
        script = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
        purge = script.split("purge_projects() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("[ -t 0 ]", purge)
        self.assertIn('[ "$typed" = "$PROJECTS_ROOT" ]', purge)
        # rm -rf appears exactly once: inside remove_path.
        self.assertEqual(script.count("rm -rf"), 1)


if __name__ == "__main__":
    unittest.main()
