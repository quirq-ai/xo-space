"""The Quirq view's Health panel: on-demand checks and one confirmed action."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class HealthPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quirq = read("js/views/quirq.js")

    def test_panel_and_run_button_exist(self) -> None:
        self.assertIn('id="quirq-health"', self.quirq)
        self.assertIn('id="quirq-health-run"', self.quirq)
        self.assertIn("apiFetch('/api/doctor')", self.quirq)

    def test_checks_never_ride_the_ten_second_poll(self) -> None:
        interval = re.search(r"setInterval\(([^;]*)\)", self.quirq).group(1)
        self.assertNotIn("loadHealth", interval)

    def test_the_action_posts_json_after_confirmation(self) -> None:
        self.assertIn("'/api/doctor/runtime-leftovers/'+encodeURIComponent(key)+'/move-aside'", self.quirq)
        self.assertIn("{method:'POST',body:{}}", self.quirq)
        self.assertIn("data-move-confirm", self.quirq)
        self.assertIn("data-move-cancel", self.quirq)

    def test_a_second_move_is_blocked_while_one_is_in_flight(self) -> None:
        self.assertIn("let movingKey=null;", self.quirq)
        match = re.search(r"async function handleHealthClick\(event\)\{(.*?)\n\}", self.quirq, re.S)
        self.assertIsNotNone(match, "handleHealthClick not found")
        self.assertIn("if(movingKey)return", match.group(1))

    def test_assets_are_stamped(self) -> None:
        # The stamp's shape, never its value: development's cache-stamp rule
        # (test_space_pr97_ui) fails no test for a routine bump.
        self.assertRegex(read("js/app.js"), r"\./views/quirq\.js\?v=\d{8}-[a-z0-9]+'")
        self.assertRegex(read("index.html"), r'href="css/quirq\.css\?v=\d{8}-[a-z0-9]+"')

    def test_preview_stubs_answer_the_doctor(self) -> None:
        preview = ROOT / "tests" / "space_ui_preview"
        for script in ("section-navigation.mjs", "contextual-toolbar.mjs"):
            with self.subTest(script=script):
                self.assertIn("url.pathname==='/api/doctor'", (preview / script).read_text(encoding="utf-8"))
        self.assertIn('"/api/doctor"', (preview / "server.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
