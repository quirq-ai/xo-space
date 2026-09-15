from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import scopes
from services.cowork_agent.visualizer import state
from services.cowork_agent.visualizer.sinks import activity
from services.cowork_agent.visualizer.workspace import activity as workspace_activity
from routers.cowork_agent.bff import workspace_visualizer


class ActivityStateTests(unittest.TestCase):
    def test_paths_live_under_the_cache_and_normalize_project_id(self) -> None:
        watcher_root = Path("/machine/.quirq/cache")

        with patch.object(state, "cache_dir", return_value=watcher_root):
            project_path = state.project_activity_path("../My Project")
            workspace_path = state.workspace_activity_path()

        self.assertEqual(
            project_path,
            watcher_root / "activity" / "projects" / "my-project.json",
        )
        self.assertEqual(workspace_path, watcher_root / "activity" / "workspace.json")

    def test_activity_sink_writes_only_the_explicit_machine_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / ".quirq" / "cache" / "activity" / "projects" / "demo.json"
            legacy = root / "xo-projects" / "demo" / ".xo" / "activity.json"
            rows = [{
                "session_id": "session-1",
                "runtime": "claude_code",
                "started_at_ms": 1_700_000_000_000,
                "updated_at_ms": 1_700_000_001_000,
            }]

            with patch.object(activity, "_resolve_user_id", return_value="local-user"):
                activity.apply(
                    target,
                    rows,
                    model_by_session={"session-1": "claude-test"},
                )

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["open_sessions"][0]["session_id"], "session-1")
            self.assertEqual(payload["open_sessions"][0]["runtime"], "claude_code")
            self.assertFalse(legacy.exists())

    def test_workspace_activity_aggregates_machine_local_project_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "projects" / "demo.json"
            workspace_path = root / "workspace.json"
            project_path.parent.mkdir(parents=True)
            project_path.write_text(
                json.dumps({
                    "schema": 1,
                    "updated_at": "2026-07-25T00:00:00Z",
                    "open_sessions": [{
                        "session_id": "session-1",
                        "runtime": "claude_code",
                        "agent": "claude-test",
                        "user_id": "local",
                        "opened_at": "2026-07-25T00:00:00Z",
                        "last_activity_at": "2026-07-25T00:00:01Z",
                    }],
                }),
                encoding="utf-8",
            )

            with (
                patch.object(workspace_activity, "list_project_ids", return_value=["demo"]),
                patch.object(workspace_activity, "project_activity_path", return_value=project_path),
                patch.object(workspace_activity, "workspace_activity_path", return_value=workspace_path),
            ):
                workspace_activity.apply()

            payload = json.loads(workspace_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["open_sessions"][0]["project_id"], "demo")

    def test_visualizer_scope_ignores_legacy_project_activity_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_root = root / "xo-projects"
            legacy_path = projects_root / "demo" / ".xo" / "activity.json"
            machine_path = root / ".quirq" / "cache" / "activity" / "projects" / "demo.json"
            legacy_path.parent.mkdir(parents=True)
            machine_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps({"schema": 1, "open_sessions": [{"session_id": "stale"}]}),
                encoding="utf-8",
            )
            machine_path.write_text(
                json.dumps({"schema": 1, "open_sessions": [{"session_id": "current"}]}),
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(projects_root)}),
                patch.object(state, "project_activity_path", return_value=machine_path),
            ):
                scope = scopes.VisualizerScope("demo")
                payload = scope.read_activity()

            self.assertEqual(payload["open_sessions"][0]["session_id"], "current")

    def test_workspace_endpoint_preserves_snapshot_freshness(self) -> None:
        class FakeWorkspaceScope:
            def read_activity(self):
                return {
                    "schema": 1,
                    "updated_at": "2026-07-25T00:00:02Z",
                    "open_sessions": [{
                        "session_id": "session-1",
                        "runtime": "claude_code",
                        "agent": "claude-test",
                        "user_id": "local",
                        "opened_at": "2026-07-25T00:00:00Z",
                        "last_activity_at": "2026-07-25T00:00:01Z",
                        "project_id": "demo",
                    }],
                }

        with patch.object(
            workspace_visualizer.scopes,
            "resolve_scope",
            return_value=FakeWorkspaceScope(),
        ):
            response = workspace_visualizer.workspace_activity()

        self.assertEqual(response.updated_at, "2026-07-25T00:00:02Z")
        self.assertEqual(response.open_sessions[0].project_id, "demo")


if __name__ == "__main__":
    unittest.main()
