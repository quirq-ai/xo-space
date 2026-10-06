from __future__ import annotations

import re
import unittest
from pathlib import Path

from routers.cowork_agent import doctor as doctor_routes
from services.doctor import leftovers, run

ROOT = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\u2013\u2014]")
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity", "grokbot")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class DoctorDocsTests(unittest.TestCase):
    """Engineering docs for xo-doctor stay aligned with the live checks and routes."""

    def setUp(self) -> None:
        self.dev = read("DEVELOPING.md")
        self.section = self.dev[self.dev.index("## 13. xo-doctor"): self.dev.index("## 14.")]

    def test_developing_lists_the_live_routes_and_the_one_action(self) -> None:
        self.assertIn("`GET /api/doctor`", self.section)
        self.assertIn(
            "`POST /api/doctor/runtime-leftovers/{key}/move-aside`", self.section,
        )
        self.assertEqual(doctor_routes.REPORT_TIMEOUT_S, 60.0)
        self.assertIn("60 s", self.section)
        self.assertIn("quarantine/runtime-leftovers/", self.section)
        self.assertIn(leftovers.ACTION["kind"], self.section)
        self.assertIn("Setup → Server → Technical details", self.section)

    def test_developing_lists_every_check_family(self) -> None:
        families = [name for name, _check in run.CHECKS]
        for family in families:
            self.assertIn(f"`{family}`", self.section, family)
        self.assertIn("MAX_FINDINGS_PER_CHECK", self.section)
        self.assertEqual(run.MAX_FINDINGS_PER_CHECK, 100)

    def test_ui_and_installation_point_at_the_same_page(self) -> None:
        ui = read("space_ui/README.md")
        self.assertIn("GET /api/doctor", ui)
        self.assertIn("Move aside", ui)
        install = read("INSTALLATION.md")
        self.assertIn("Technical details", install)
        self.assertIn("/api/doctor", install)

    def test_no_dashes_and_no_agent_names(self) -> None:
        self.assertIsNone(DASHES.search(self.section))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, self.section)


if __name__ == "__main__":
    unittest.main()
