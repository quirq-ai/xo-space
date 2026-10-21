"""ch 15 §8: no session XO starts runs with a CLI's skip or bypass flag.

Each runtime's argv is checked on every preset and every plain-chat mode in
``tests/test_storm_presets.py``. This scans the source: since PR 19 removed
today's flagged chat path, no skip flag is written anywhere but the Connect
Antigravity login trigger (a connect flow, not a session; ch 15 §8's last
row), and no manifest declares the posture any more."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
         "--dangerously-bypass-approvals-and-sandbox", "dangerously_skip_permissions",
         "dangerously_bypass_approvals_and_sandbox")
#: agy's login trigger runs ``agy -p hello … --dangerously-skip-permissions``
#: under a PTY only to make agy start its login (swarm contract §1.3).
LOGIN_TRIGGER = Path("services/cowork_agent/adapters/antigravity/routes.py")
SKIPPED = {".git", "venv", ".venv", "node_modules", "__pycache__", "tests", "docs"}
SUFFIXES = {".py", ".json", ".sh", ".toml", ".yaml", ".yml", ".js", ".ts", ".md"}


def sources() -> list[Path]:
    found = []
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO)
        if rel.parts and rel.parts[0] in SKIPPED or any(p in SKIPPED for p in rel.parts):
            continue
        if path.is_file() and path.suffix in SUFFIXES:
            found.append(rel)
    return found


class NoSkipFlagTests(unittest.TestCase):
    def test_only_the_login_trigger_names_a_skip_flag(self) -> None:
        named = sorted({str(rel) for rel in sources()
                        if any(flag in (REPO / rel).read_text(encoding="utf-8", errors="replace") for flag in FLAGS)})
        self.assertEqual(named, [str(LOGIN_TRIGGER)])


if __name__ == "__main__":
    unittest.main()
