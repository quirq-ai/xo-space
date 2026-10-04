"""The repo-wide checks that used to live only in .github/workflows/tests.yml.

quirq infra's generated presubmit runs this suite with plain ``pytest``, and
``infra/repo.toml`` names each class below as its own target, so these checks
keep gating every change once the hand-written workflow is gone:

- route parity: ``scripts/check_route_parity.py`` (import gate + every agent
  runtime is the shared core plus exactly its own routes.py)
- plugin bundles: ``scripts/check_plugin_sync.sh`` plus a ``bash -n`` pass
  over the plugin entry scripts (what test-codex-plugin.yml ran)
- Space UI syntax: every ``space_ui/js`` module parsed as an ES module, since
  the UI has no build step and no compiler would catch a syntax error. Each
  file goes through stdin with ``--input-type=module``: a plain
  ``node --check file.js`` (what tests.yml ran) exits 0 on Node 22 for a
  module file with a syntax error, because module detection swallows it

The install.sh and uninstall.sh harnesses already run from
tests/test_install_sh.py and tests/test_uninstall_sh.py.

A missing tool skips a check on a contributor's machine, but fails it in CI
(``CI`` set), so a runner without bash or node can never pass by skipping.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IN_CI = bool(os.environ.get("CI"))


def _tool(name: str) -> str | None:
    """The path of ``name``; in CI a missing tool is a failure, not a skip."""
    path = shutil.which(name)
    if path is None and IN_CI:
        raise AssertionError(f"{name} is not on PATH in CI; this check cannot be skipped there")
    return path


class RepoCheckCase(unittest.TestCase):
    def run_check(self, argv: list[str], timeout: int = 300, stdin: Path | None = None) -> None:
        source = stdin.read_text(encoding="utf-8") if stdin is not None else None
        proc = subprocess.run(argv, cwd=ROOT, input=source, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            self.fail(f"{' '.join(argv)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")


class RouteParity(RepoCheckCase):
    def test_route_parity(self) -> None:
        self.run_check([sys.executable, "scripts/check_route_parity.py"])


class PluginBundles(RepoCheckCase):
    def test_bundles_in_sync(self) -> None:
        bash = _tool("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        self.run_check([bash, "scripts/check_plugin_sync.sh"])

    def test_plugin_scripts_parse(self) -> None:
        bash = _tool("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        self.run_check([bash, "-n", "plugins/quirq/scripts/space.sh", "plugin/scripts/discover.sh"])


class SpaceUiSyntax(RepoCheckCase):
    def test_every_module_parses(self) -> None:
        node = _tool("node")
        if node is None:
            self.skipTest("node is not installed")
        modules = sorted((ROOT / "space_ui" / "js").rglob("*.js"))
        self.assertTrue(modules, "no space_ui/js modules found")
        for module in modules:
            with self.subTest(module=str(module.relative_to(ROOT))):
                self.run_check([node, "--input-type=module", "--check"], timeout=60, stdin=module)


if __name__ == "__main__":
    unittest.main()
