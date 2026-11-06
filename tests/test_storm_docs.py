"""Pin the storm runtime facts that rot when an agent lands or a chat path
changes: which adapters ship ``runtime/``, how ``POST /api/chat/prompt``
answers, and which stream URL to read.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent.adapters.loader import list_capability_providers
from services.doctor import run as doctor_run

ROOT = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\u2013\u2014]")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class StormDocsTests(unittest.TestCase):
    def test_three_adapters_ship_a_runtime_package(self) -> None:
        self.assertEqual(
            list_capability_providers("runtime"),
            ["antigravity", "claude_code", "codex"],
        )

    def test_capability_table_marks_those_runtimes(self) -> None:
        dev = read("DEVELOPING.md")
        row = next(line for line in dev.splitlines() if line.startswith("| `runtime` |"))
        # columns after the description: openclaw claude_code codex hermes antigravity grokbot
        cells = [c.strip() for c in row.split("|")[1:-1]]
        self.assertEqual(cells[0], "`runtime`")
        self.assertEqual(cells[2:], ["no", "✓", "✓", "no", "✓", "no"])
        self.assertNotIn("until their runtimes", dev)
        self.assertNotIn("C1 and C2", dev)

    def test_readme_chat_example_is_the_session_stream(self) -> None:
        readme = squash(read("README.md"))
        self.assertIn("Claude Code, Codex or Antigravity", readme)
        self.assertIn("/api/chat/sessions/cse_4f2a.../stream", readme)
        self.assertIn("`codex app-server` JSON-RPC; storm runtime", readme)
        self.assertIn("`agy` stream-json + Google OAuth; storm runtime", readme)
        self.assertNotIn("when their storm runtimes land", readme)
        self.assertNotIn("`codex exec` subprocess", readme)
        self.assertIn("still speaks `{stream_id}`", readme)

    def test_developing_lists_storm_http_surfaces_and_the_two_streams(self) -> None:
        dev = read("DEVELOPING.md")
        sec = dev[dev.index("### 3.5 The runtime contract (storm)"): dev.index("## 4. Adding a new agent")]
        for needle in (
            "`GET /api/chat/sessions/{id}/stream`",
            "`GET /api/chat/stream/{stream_id}`",
            "`POST /api/chat/prompt`",
            "`POST /mcp/space-comms`",
            "`/api/chat/chats*`",
            "`/api/chat/review*`",
            "`/api/chat/worktrees*`",
            "space_ui/js/views/chat.js",
            "storm/store/retention.py",
        ):
            self.assertIn(needle, sec)
        self.assertIsNone(DASHES.search(sec), "en/em dashes in §3.5")

    def test_adding_an_agent_mentions_the_runtime_package(self) -> None:
        for rel in ("DEVELOPING.md", "CONTRIBUTING.md"):
            text = squash(read(rel))
            self.assertIn("runtime/", text)
            self.assertIn("leave `run`/`stream` as the refusing defaults", text)

    def test_layout_names_storm_terminal_doctor_and_contract(self) -> None:
        layout = read("DEVELOPING.md")
        layout = layout[layout.index("## 2. Repository layout"): layout.index("## 3. How dispatch works")]
        for needle in (
            "storm/",
            "terminal.py doctor.py",
            "contract.py",
            "decision_models/",
            "  doctor/",
            "  terminal/",
            "/download (100 MB",
        ):
            self.assertIn(needle, layout)

    def test_chat_view_docs_do_not_claim_storm_works(self) -> None:
        row = next(
            line for line in read("space_ui/README.md").splitlines()
            if "`js/views/chat.js`" in line
        )
        self.assertIn("does not speak that contract", row)
        self.assertIn("Deliberately unregistered", row)
        self.assertNotIn("Works across claude_code", row)


class DoctorDocsTests(unittest.TestCase):
    def test_developing_lists_every_check_family(self) -> None:
        sec = read("DEVELOPING.md")
        sec = sec[sec.index("## 13. xo-doctor"): sec.index("## 14. Releases")]
        for family, _check in doctor_run.CHECKS:
            self.assertIn(f"`{family}`", sec, family)
        self.assertIn("`GET /api/doctor`", sec)
        self.assertIn("`POST /api/doctor/runtime-leftovers/{key}/move-aside`", sec)
        self.assertIn("`doctor_identity_lost`", sec)
        self.assertIn("Health checks", sec)
        self.assertIsNone(DASHES.search(sec), "en/em dashes in §13")

    def test_installation_and_space_ui_point_at_health_checks(self) -> None:
        install = read("INSTALLATION.md")
        self.assertIn("## Health checks", install)
        self.assertIn("GET /api/doctor", install)
        self.assertIn("Move aside", install)
        self.assertIn("never falls back to cloning a fresh copy", install)
        quirq = next(
            line for line in read("space_ui/README.md").splitlines()
            if "`js/views/quirq.js`" in line
        )
        self.assertIn("Health checks", quirq)
        self.assertIn("/api/doctor", quirq)

    def test_projects_root_python_fallback_is_documented(self) -> None:
        for rel in ("README.md", "INSTALLATION.md", "DEVELOPING.md", ".env.example"):
            text = read(rel)
            self.assertIn("~", text)
            self.assertRegex(
                squash(text),
                r"(Python defaults to `~`|unset → `~`|Python default if unset: ~)",
                rel,
            )


if __name__ == "__main__":
    unittest.main()
