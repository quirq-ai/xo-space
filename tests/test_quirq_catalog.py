from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import quirq_catalog


class QuirqCatalogTests(unittest.TestCase):
    def test_catalog_summarizes_state_without_exposing_secret_or_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".quirq"
            projects_root = Path(tmp) / "projects"
            project_xo = projects_root / "demo" / ".xo"
            activity = root / "watcher" / "activity" / "projects"
            activity.mkdir(parents=True)
            (project_xo / "sessions").mkdir(parents=True)
            (root / "secrets.env").write_text(
                "PRIVATE_TOKEN=do-not-return\n",
                encoding="utf-8",
            )
            (root / "state.json").write_text(
                json.dumps({"onboarding_completed": True}),
                encoding="utf-8",
            )
            (root / "scheduler" / "runs").mkdir(parents=True)
            (root / "scheduler" / "jobs.json").write_text('{"secret":"do-not-return"}')
            (root / "scheduler" / "state.json").write_text('{"output_tail":"do-not-return"}')
            (root / "scheduler" / "runs" / "demo.jsonl").write_text('{"output_tail":"do-not-return"}\n')
            (activity / "demo.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "updated_at": "2026-07-25T00:00:00Z",
                        "open_sessions": [
                            {
                                "session_id": "private-session-id",
                                "runtime": "test_runtime",
                                "agent": "private-model",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (project_xo / "project.json").write_text(
                '{"name":"demo"}\n',
                encoding="utf-8",
            )
            (project_xo / "todos.json").write_text(
                '{"private_todo":"do-not-return"}\n',
                encoding="utf-8",
            )
            (project_xo / "activity.json").write_text(
                '{"legacy":"do-not-return"}\n',
                encoding="utf-8",
            )
            env = {
                "QUIRQ_STATE_ROOT": str(root),
                "QUIRQ_HOST_STATE_ROOT": str(root),
                "XO_PROJECTS_ROOT": str(projects_root),
                "QUIRQ_HOST_PROJECTS_ROOT": str(projects_root),
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    quirq_catalog,
                    "load_env_entries",
                    return_value=[
                        {"key": "PRIVATE_TOKEN", "value": "do-not-return"}
                    ],
                ),
                patch.object(
                    quirq_catalog,
                    "configured_settings",
                    return_value={
                        "agent_name": "test",
                        "watcher_enabled": True,
                        "watcher_interval_seconds": 1,
                        "watcher_source_mode": "all",
                    },
                ),
                patch.object(
                    quirq_catalog,
                    "effective_settings",
                    return_value={
                        "agent_name": "test",
                        "watcher_enabled": True,
                        "watcher_interval_seconds": 1,
                        "watcher_source_mode": "all",
                    },
                ),
            ):
                result = quirq_catalog.quirq_catalog()

            rendered = repr(result)
            self.assertNotIn("do-not-return", rendered)
            self.assertNotIn("private-session-id", rendered)
            self.assertNotIn("private-model", rendered)
            self.assertNotIn("private_todo", rendered)
            self.assertIn("PRIVATE_TOKEN", rendered)
            self.assertEqual(result["activity"]["projects"][0]["project_id"], "demo")
            self.assertEqual(result["activity"]["projects"][0]["open_sessions"], 1)
            secret_row = next(
                row for row in result["tree"] if row["path"] == "secrets.env"
            )
            self.assertTrue(secret_row["sensitive"])
            tree = {row["path"]: row for row in result["tree"]}
            self.assertIn("Saved manual commands", tree["scheduler"]["description"])
            self.assertIn("Command execution", tree["scheduler/state.json"]["description"])
            self.assertIn("output tail", tree["scheduler/runs/demo.jsonl"]["description"])
            outputs = result["project_outputs"]
            self.assertEqual(outputs["project_count"], 1)
            self.assertEqual(outputs["projects"][0]["project_id"], "demo")
            self.assertIn(
                "todos.json",
                outputs["projects"][0]["watcher_files"],
            )
            self.assertEqual(outputs["legacy_activity_files"], 1)
            self.assertIn(
                ".quirq/watcher/activity",
                outputs["legacy_activity_note"],
            )


if __name__ == "__main__":
    unittest.main()
