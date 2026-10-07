"""Runs tests/quirq/check_bridge.cjs: the quirq plugin's Space bridge
(plugins/quirq/ui/space-bridge.js) in a fake host, without a browser.
Needs Node.js 18+; skipped when node is not on PATH."""
from __future__ import annotations
import shutil
import subprocess
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parent / "quirq" / "check_bridge.cjs"


@unittest.skipUnless(shutil.which("node"), "Node.js is not on PATH")
class QuirqBridgeTests(unittest.TestCase):
    def test_bridge_check_passes(self):
        result = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Bridge checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
