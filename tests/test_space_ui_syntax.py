"""Every Space UI module must parse as an ES module.

One character can take the whole app down: the UI is a hand-wired ESM
graph with no build step, so a syntax error in any imported module —
say, an ASCII apostrophe inside a single-quoted prose string — fails
the entire import chain and white-screens the page. ``node --check`` on
a bare ``.js`` path parses in the wrong source mode and can false-pass
exactly that error, so this guard copies each module to ``.mjs`` first,
which forces the ESM grammar the browser actually uses.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class SpaceUISyntaxTests(unittest.TestCase):
    def test_every_ui_module_parses_as_esm(self) -> None:
        modules = sorted((ROOT / "space_ui" / "js").rglob("*.js"))
        self.assertGreater(len(modules), 10)  # the glob found the tree
        failures = []
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.mjs"
            for module in modules:
                probe.write_bytes(module.read_bytes())
                proc = subprocess.run(
                    ["node", "--check", str(probe)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or "").strip().splitlines()
                    failures.append(
                        f"{module.relative_to(ROOT)}: "
                        + " ".join(detail[-2:])[:200]
                    )
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
