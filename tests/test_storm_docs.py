from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent.adapters.loader import list_capability_providers

ROOT = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\u2013\u2014]")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class StormDocsTests(unittest.TestCase):
    """Storm's public surfaces and which agents have a runtime stay aligned
    with DEVELOPING.md and the README chat examples."""

    def test_runtime_capability_matches_the_capability_table(self) -> None:
        providers = list_capability_providers("runtime")
        self.assertEqual(providers, ["claude_code", "codex"])
        table_line = next(
            line for line in read("DEVELOPING.md").splitlines()
            if line.startswith("| `runtime` |")
        )
        # columns: capability, what, openclaw, claude_code, codex, hermes,
        # antigravity, grokbot
        cells = [cell.strip() for cell in table_line.strip("|").split("|")]
        self.assertEqual(cells[2], "no")
        self.assertEqual(cells[3], "✓")
        self.assertEqual(cells[4], "✓")
        self.assertEqual(cells[5], "no")
        self.assertEqual(cells[6], "no")
        self.assertEqual(cells[7], "no")

    def test_readme_names_the_live_runtimes_and_the_stream_id_split(self) -> None:
        readme = read("README.md")
        self.assertIn("codex app-server", readme)
        self.assertNotIn("codex exec", readme)
        self.assertIn("Claude Code and Codex chat on storm", readme)
        self.assertIn("Claude Code or Codex", readme)
        self.assertNotIn("chat again when their storm runtimes land", readme)
        self.assertIn("Antigravity", readme)
        self.assertIn("/api/chat/sessions/", readme)
        self.assertIn("`GET /api/chat/stream/{stream_id}`", readme)

    def test_developing_lists_the_storm_http_surfaces(self) -> None:
        section = read("DEVELOPING.md")
        start = section.index("## 3. How dispatch works")
        end = section.index("## 4. Adding a new agent")
        body = section[start:end]
        for needle in (
            "`POST /api/chat/prompt`",
            "`GET /api/chat/runtimes`",
            "`POST/GET /api/chat/chats/{id}/attachments`",
            "`GET/DELETE /api/chat/worktrees*`",
            "`POST /mcp/space-comms`",
            "`GET /api/xo-projects/{id}/image`",
            "space_ui/js/views/chat.js",
            "storm/store/retention.py",
            "adapters/<name>/runtime/",
        ):
            self.assertIn(needle, body, needle)
        self.assertIsNone(DASHES.search(body))
        self.assertIn("501 unsupported", squash(body))

    def test_layout_names_storm_terminal_and_doctor(self) -> None:
        dev = read("DEVELOPING.md")
        start = dev.index("services/  ")
        services_block = dev[start:dev.index("  cowork_agent/  ", start)]
        self.assertIn("  doctor/", services_block)
        self.assertIn("  terminal/", services_block)
        self.assertIn("  decision_models/", services_block)
        cowork = dev[dev.index("  cowork_agent/                   what it takes"):]
        self.assertIn("    storm/", cowork)
        self.assertIn("contract.py", cowork.split("## 3.")[0])


if __name__ == "__main__":
    unittest.main()
