"""`in_progress` on a GitHub issue row.

Without it, "which of these issues is someone working on right now" costs a
frontend two calls and a client-side join on `workitem_id` — `GET
/github/issues` knows which issues are adopted, and `GET /workitems` knows
which workitems are claimed, and neither knows both. The browse list is
exactly where that question is asked, so the answer belongs on the row.

The value is the same derivation the workitem listing uses: a live agent
claim, never a stored flag (see `workitem_claims`). Two consequences the
tests below pin, because both look like bugs if you meet them cold:

* an **unadopted** issue is never in progress — there is no workitem to claim;
* an issue stays in progress for the ~60s anti-flicker grace window after its
  agent's session disappears, because a claim younger than that window is in
  progress on its own authority.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import github_mirror
from services.cowork_agent.visualizer import state as watcher_state
from services.cowork_agent.visualizer import workitem_claims as claims
from services.cowork_agent.visualizer import workitems_store as store

_PID = "00000001-0000-4000-8000-000000000001"
_NODE = "I_kwDOnode1"


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class IssueRowProgressTests(unittest.TestCase):
    PROJECT = "demo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "projects"
        (self.root / self.PROJECT / ".xo").mkdir(parents=True)
        env = {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(base / ".quirq"),
            "XO_GITHUB_POLL_ENABLED": "false",
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.xo = self.root / self.PROJECT / ".xo"
        (self.xo / "project.json").write_text(
            json.dumps(
                {
                    "schema": 2, "pid": _PID, "name": self.PROJECT,
                    "owner_user_id": "ankitdwivedi",
                    "created_at": "2026-01-01T00:00:00Z",
                    "git": {
                        "remote_url": "https://github.com/owner/repo",
                        "default_branch": "main",
                    },
                }
            ),
            encoding="utf-8",
        )

        mirror = github_mirror.mirror_path(self.PROJECT, create=True)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(
            json.dumps(
                {
                    "schema": 1, "repo": "owner/repo",
                    "fetched_at": "2026-09-08T00:00:00Z",
                    "since": None, "rate": None, "error": None,
                    "issues": {
                        _NODE: {
                            "node_id": _NODE, "number": 7, "title": "an issue",
                            "state": "open", "state_reason": None,
                            "assignees": [],
                            "url": "https://github.com/owner/repo/issues/7",
                            "updated_at": "2026-09-08T00:00:00Z",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        app = FastAPI()
        from routers.cowork_agent.bff.visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}"

    # ── helpers ──────────────────────────────────────────────────────────

    def row(self) -> dict:
        res = self.client.get(f"{self.base}/github/issues")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["issues"][0]

    def adopt(self) -> str:
        """Adopt via the store, so the test needs no network."""
        record, _ = store.adopt_workitem(
            self.xo / "workitems.json",
            runtime="claude_code",
            title="an issue",
            labels=[],
            github={
                "repo": "owner/repo", "number": 7, "node_id": _NODE,
                "url": "https://github.com/owner/repo/issues/7",
            },
        )
        return record["id"]

    def presence(self, *session_ids: str) -> None:
        path = watcher_state.project_activity_path(self.PROJECT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1, "updated_at": "2026-09-08T00:00:00Z",
                    "open_sessions": [
                        {"session_id": s, "runtime": "claude_code",
                         "agent": "m", "user_id": "u"} for s in session_ids
                    ],
                }
            ),
            encoding="utf-8",
        )

    def claim(self, workitem_id: str, *, age_seconds: int) -> None:
        started = _iso(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_seconds)
        )
        claims.claim_workitem(
            claims.claims_path_for(
                project_layout.runtime_dir_for_project(self.PROJECT, create=True)
            ),
            workitem_id, session_id="s1", runtime="claude_code",
            started_at=started,
        )

    # ── the cases ────────────────────────────────────────────────────────

    def test_an_unadopted_issue_is_never_in_progress(self) -> None:
        """There is no workitem to claim, so the answer is not 'unknown'."""
        row = self.row()
        self.assertFalse(row["adopted"])
        self.assertIsNone(row["workitem_id"])
        self.assertFalse(row["in_progress"])

    def test_adopting_alone_does_not_start_progress(self) -> None:
        self.adopt()
        row = self.row()
        self.assertTrue(row["adopted"])
        self.assertFalse(row["in_progress"])

    def test_a_live_claim_shows_on_the_issue_row(self) -> None:
        workitem_id = self.adopt()
        self.presence("s1")
        self.claim(workitem_id, age_seconds=3600)
        row = self.row()
        self.assertTrue(row["in_progress"])
        self.assertEqual(row["workitem_id"], workitem_id)

    def test_a_dead_session_clears_it_with_no_cleanup(self) -> None:
        """Nothing deletes the claim; presence alone decides."""
        workitem_id = self.adopt()
        self.presence("s1")
        self.claim(workitem_id, age_seconds=3600)
        self.assertTrue(self.row()["in_progress"])

        claims_path = claims.claims_path_for(
            project_layout.runtime_dir_for_project(self.PROJECT)
        )
        before = claims_path.read_bytes()
        self.presence()  # the only way this system "kills" an agent
        self.assertFalse(self.row()["in_progress"])
        self.assertEqual(claims_path.read_bytes(), before)

    def test_a_young_claim_survives_the_session_being_absent(self) -> None:
        """The anti-flicker window. This looks like a bug if you meet it
        cold, so it is pinned: a just-claimed workitem reads as in progress
        even before its session appears in the presence snapshot, because
        `activity.py` drops rows whose model is not yet known."""
        workitem_id = self.adopt()
        self.presence()  # not live yet
        self.claim(workitem_id, age_seconds=0)
        self.assertTrue(self.row()["in_progress"])
        self.assertLessEqual(claims.grace_seconds(), 3600)

    def test_no_join_is_needed(self) -> None:
        """The point of the field: the browse list answers on its own."""
        workitem_id = self.adopt()
        self.presence("s1")
        self.claim(workitem_id, age_seconds=3600)
        issues = self.client.get(f"{self.base}/github/issues").json()["issues"]
        self.assertEqual(
            [(i["number"], i["in_progress"]) for i in issues], [(7, True)]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
