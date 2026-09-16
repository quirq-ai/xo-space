"""DEVELOPING.md's capability table matches the adapters on disk.

The table is how a new agent author learns which modules exist and which are
optional. It went stale once (codex and cursor were missing), so it is read
back here: every adapter folder has a column, every capability module shipped
by any adapter has a row, and each cell says whether that module exists.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "services" / "cowork_agent" / "adapters"
DEVELOPING = ROOT / "DEVELOPING.md"

# Adapter-internal helpers, not capabilities core loads by name. A new helper
# module that core never loads by name belongs here, not in the table.
_HELPERS = {
    "auth", "direct_stream", "dump", "gateway_pool", "mcp_config", "paths",
    "profile_env", "remote_control", "sessionslist", "state_db", "store",
    "tokens", "transcript",
}


def _adapter_modules() -> dict[str, set[str]]:
    return {
        entry.name: {
            module.stem for module in entry.glob("*.py")
            if not module.stem.startswith("_")
        }
        for entry in sorted(ADAPTERS.iterdir())
        if entry.is_dir() and entry.name.isidentifier() and not entry.name.startswith("_")
    }


def _table() -> tuple[list[str], dict[str, dict[str, bool]]]:
    text = DEVELOPING.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("| ")]
    header = next(line for line in lines if line.startswith("| capability | what it provides |"))
    agents = [cell.strip() for cell in header.strip("|").split("|")][2:]
    start = lines.index(header) + 1
    rows: dict[str, dict[str, bool]] = {}
    for line in lines[start:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        match = re.fullmatch(r"`(\w+)`", cells[0])
        if not match:
            if rows:
                break
            continue
        rows[match.group(1)] = {
            agent: cell == "✓" for agent, cell in zip(agents, cells[2:])
        }
    return agents, rows


class CapabilityTableTests(unittest.TestCase):
    def test_every_adapter_has_a_column(self) -> None:
        agents, _ = _table()
        self.assertEqual(sorted(agents), sorted(_adapter_modules()))

    def test_every_capability_has_a_row_and_each_cell_is_true(self) -> None:
        modules = _adapter_modules()
        _, rows = _table()
        shipped = set().union(*modules.values()) - _HELPERS
        self.assertEqual(sorted(rows), sorted(shipped))
        for capability, cells in rows.items():
            for agent, marked in cells.items():
                with self.subTest(capability=capability, agent=agent):
                    self.assertEqual(marked, capability in modules[agent])


if __name__ == "__main__":
    unittest.main()
