"""install.sh — the functions that decide what the one-liner does.

Runs ``tests/install_sh_harness.sh``, which sources ``install.sh`` minus its
final ``main "$@"`` and drives ``resolve_repo_dir``, ``fetch_repo`` and
``print_restart_hint`` against temp directories and a local ``file://`` git
remote. Nothing is cloned from the network, no venv is built and no server is
started — but the real script logic runs, which is what caught an ``exec``
that had drifted into the banner function and would have killed every
install at startup under ``set -u``.

Needs a POSIX host (skipped elsewhere) with bash and git, which fail the test
when missing (tests/required_tools.py). The harness prints one PASS/FAIL line
per case, so a failure names the case directly.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from tests.required_tools import clean_env, require_tools

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "install_sh_harness.sh"


class InstallShTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "needs a POSIX host (Windows is WSL-only)")
    def test_harness_passes(self) -> None:
        bash = require_tools(self, "bash", "git")["bash"]
        result = subprocess.run(
            [bash, str(HARNESS)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=clean_env(),
        )
        self.assertEqual(
            result.returncode, 0,
            "install.sh harness failed:\n" + result.stdout + result.stderr,
        )
        self.assertNotIn("FAIL", result.stdout)

    def test_harness_targets_the_repo_script(self) -> None:
        """Text-level: the harness must exercise this repo's install.sh by
        default, and the exec must stay in start_server (the bug it guards)."""
        harness = HARNESS.read_text(encoding="utf-8")
        self.assertIn("INSTALL_SH:-", harness)
        self.assertIn("start_server still owns the exec", harness)

        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        hint = script.split("print_restart_hint() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("exec ", hint, "print_restart_hint must be print-only")


if __name__ == "__main__":
    unittest.main()
