"""No finding is built without a headline and a next step (#188 design §1)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

DOCTOR = Path(__file__).resolve().parents[1] / "services" / "doctor"
#: OK-level notes: informational, never a problem to act on.
EXEMPT = {"inventory.unknown_file", "read.recent"}


class EveryFindingTests(unittest.TestCase):
    def test_every_finding_call_has_a_title_and_a_next_step(self) -> None:
        missing = []
        for source in sorted(DOCTOR.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Finding"):
                    continue
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and first.value in EXEMPT:
                    continue
                keywords = {kw.arg for kw in node.keywords}
                if not {"title", "next_step"} <= keywords:
                    missing.append(f"{source.name}:{node.lineno}")
        self.assertEqual(missing, [], "Finding(...) without title= and next_step=")


if __name__ == "__main__":
    unittest.main()
