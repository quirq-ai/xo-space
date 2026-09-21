from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.cowork_agent.visualizer.workitem_claims import (
    CLAIM_GRACE_SECONDS,
    CLAIMS_RELPATH,
)
from services.cowork_agent.visualizer.workitems_store import (
    GITHUB_OWNED_FIELDS,
    VALID_STATE_REASONS,
    VALID_STATUSES,
    WORKITEMS_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "xo-projects"
DOC = SKILL_DIR / "references" / "workitems-http-api.md"
SKILL = SKILL_DIR / "SKILL.md"
DASHES = re.compile("[\\u2013\\u2014]")
AGENT_NAMES = ("openclaw", "hermes", "claude_code", "codex", "antigravity")


class WorkitemsHttpApiDocsTests(unittest.TestCase):
    """The workitem HTTP contract the xo-projects skill points at stays true."""

    def setUp(self) -> None:
        self.text = DOC.read_text(encoding="utf-8")

    def test_file_exists_and_is_linked_from_the_skill_and_developing(self) -> None:
        self.assertTrue(DOC.is_file(), "workitems-http-api.md is missing")
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("references/workitems-http-api.md", skill)
        developing = (ROOT / "DEVELOPING.md").read_text(encoding="utf-8")
        self.assertIn("references/workitems-http-api.md", developing)

    def test_lists_the_live_routes(self) -> None:
        for route in (
            "GET    /api/xo-projects/{project_id}/workitems",
            "POST   /api/xo-projects/{project_id}/workitems",
            "PATCH  /api/xo-projects/{project_id}/workitems/{workitem_id}",
            "DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}",
            "POST   /api/xo-projects/{project_id}/workitems/{workitem_id}/claim",
            "DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/claim",
            "PUT    /api/xo-projects/{project_id}/workitems/{workitem_id}/assignee",
            "DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/adoption",
            "GET    /api/xo-projects/{project_id}/github/issues",
            "POST   /api/xo-projects/{project_id}/github/issues/{issue_number}/adopt",
            "GET    /api/workspace/workitems",
        ):
            self.assertIn(route, self.text)

    def test_matches_the_store_vocabulary(self) -> None:
        self.assertEqual(VALID_STATUSES, frozenset({"open", "closed"}))
        self.assertIn("`open` or `closed`", self.text)
        for reason in sorted(VALID_STATE_REASONS):
            self.assertIn(f"`{reason}`", self.text)
        for field in GITHUB_OWNED_FIELDS:
            self.assertIn(f"`{field}`", self.text)
        self.assertIn("github_authoritative", self.text)
        self.assertIn("GITHUB_OWNED_FIELDS", self.text)
        self.assertIn(CLAIMS_RELPATH, self.text)
        self.assertIn("at least 60 s", self.text)
        self.assertEqual(CLAIM_GRACE_SECONDS, 60.0)
        self.assertEqual(WORKITEMS_SCHEMA, 1)
        self.assertIn("`.xo/workitems.json`", self.text)
        self.assertIn("never stored", self.text)
        self.assertIn("GitHub is read-only", self.text)

    def test_explains_todos_are_not_workitems(self) -> None:
        self.assertIn("not interchangeable", self.text)
        self.assertIn("links.todo_ids", self.text)
        self.assertIn("UUID4", self.text)
        self.assertIn("assignee_unresolved", self.text)
        self.assertIn("no_github_credential", self.text)
        self.assertIn("not_a_github_project", self.text)

    def test_no_dashes_and_no_agent_names_in_the_contract(self) -> None:
        self.assertIsNone(DASHES.search(self.text))
        # the create example may name a runtime as a payload value; the
        # vocabulary table must not grow a per-agent branch
        for agent in AGENT_NAMES:
            if agent == "claude_code":
                continue
            self.assertNotIn(agent, self.text)


if __name__ == "__main__":
    unittest.main()
