from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent.visualizer.peers_store import (
    PEERS_SCHEMA,
    VALID_ROLES,
    _MAX_PEERS,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "xo-projects"
DOC = SKILL_DIR / "references" / "peers-http-api.md"
SKILL = SKILL_DIR / "SKILL.md"
DASHES = re.compile("[\\u2013\\u2014]")
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")


class PeersHttpApiDocsTests(unittest.TestCase):
    """The peers HTTP contract the xo-projects skill points at stays true."""

    def setUp(self) -> None:
        self.text = DOC.read_text(encoding="utf-8")

    def test_file_exists_and_is_linked_from_the_skill_and_developing(self) -> None:
        self.assertTrue(DOC.is_file(), "peers-http-api.md is missing")
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("references/peers-http-api.md", skill)
        developing = (ROOT / "DEVELOPING.md").read_text(encoding="utf-8")
        self.assertIn("references/peers-http-api.md", developing)

    def test_lists_the_live_routes(self) -> None:
        for route in (
            "GET    /api/xo-projects/{project_id}/peers",
            "POST   /api/xo-projects/{project_id}/peers",
            "GET    /api/xo-projects/{project_id}/peers/{user_id}",
            "PATCH  /api/xo-projects/{project_id}/peers/{user_id}",
            "DELETE /api/xo-projects/{project_id}/peers/{user_id}",
        ):
            self.assertIn(route, self.text)

    def test_matches_the_store_vocabulary(self) -> None:
        for role in sorted(VALID_ROLES):
            self.assertIn(f"`{role}`", self.text)
        self.assertEqual(VALID_ROLES, frozenset({"owner", "collaborator", "viewer"}))
        self.assertIn(str(_MAX_PEERS), self.text)
        self.assertEqual(_MAX_PEERS, 1000)
        self.assertEqual(PEERS_SCHEMA, 1)
        self.assertIn("peer_exists", self.text)
        self.assertIn("hard delete", self.text)
        self.assertIn("Empty roster = solo", self.text)
        self.assertIn("`.xo/peers.json`", self.text)

    def test_no_dashes_and_no_agent_names(self) -> None:
        self.assertIsNone(DASHES.search(self.text))
        for agent in AGENT_NAMES:
            self.assertNotIn(agent, self.text)


if __name__ == "__main__":
    unittest.main()
