from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from routers import space
from scripts import list_runtime_mounts
from services.cowork_agent.adapters.claude_code import session_prompts
from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.visualizer.argus_index import build_argus_stats
from services.cowork_agent.visualizer import session_telemetry


ROOT = Path(__file__).resolve().parents[1]


class SpaceSessionsUiTests(unittest.TestCase):
    def test_ui_exposes_current_session_features(self) -> None:
        view = (
            ROOT / "space_ui" / "js" / "views" / "sessions.js"
        ).read_text(encoding="utf-8")
        css = (
            ROOT / "space_ui" / "css" / "sessions.css"
        ).read_text(encoding="utf-8")
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn("sess-sources", view)
        self.assertIn("const PAGE_SIZE=10", view)
        self.assertIn("Prompts by turn", view)
        self.assertIn("data/session_prompts.json?agent=", view)
        self.assertIn("unclassified", view)
        self.assertIn("cost is unavailable", view)
        self.assertIn(".sess-pager", css)
        self.assertIn(".sess-prompt", css)
        self.assertIn("sessions.js?v=", app)
        self.assertIn("sessions.css?v=20260725-sessions2", index)

    def test_telemetry_only_providers_are_discovered(self) -> None:
        providers = list_capability_providers("session_telemetry")
        self.assertIn("claude_code", providers)
        self.assertIn("codex", providers)
        self.assertIn("cursor", providers)

    def test_docker_mount_discovery_marks_telemetry_homes_optional(self) -> None:
        root = Path("/runtime-home")
        provider = SimpleNamespace(
            runtime_mounts=lambda: [root / ".native-sessions"]
        )
        with mock.patch.object(
            list_runtime_mounts.Path,
            "home",
            return_value=root,
        ), mock.patch.object(
            list_runtime_mounts,
            "all_agents",
            return_value=[],
        ), mock.patch.object(
            list_runtime_mounts,
            "list_capability_providers",
            return_value=["telemetry"],
        ), mock.patch.object(
            list_runtime_mounts,
            "try_load_capability",
            return_value=provider,
        ):
            mounts = list_runtime_mounts.runtime_mounts()

        self.assertEqual(
            mounts,
            [(".native-sessions", "/runtime-home/.native-sessions", False)],
        )


class ArgusSessionsTests(unittest.TestCase):
    def test_preserves_agent_dimension_and_filters_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "argus.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                create table app_meta (key text primary key, value text);
                insert into app_meta values ('schema_version', '6');
                create table sessions (
                    id text primary key, agent text not null, project_path text,
                    started_at text, ended_at text, duration_sec integer,
                    total_fresh_input_tokens integer,
                    total_output_tokens integer,
                    total_cache_read_tokens integer,
                    total_cache_write_tokens integer,
                    total_cost_usd real, primary_model text, turn_count integer,
                    agent_version text, pricing_table_version text
                );
                create table turns (
                    id text primary key, session_id text, timestamp text, model text,
                    fresh_input_tokens integer, output_tokens integer,
                    cache_read_tokens integer, cache_write_tokens integer,
                    cost_usd real
                );
                create table tool_calls (
                    id text primary key, session_id text, timestamp text,
                    tool_name text, is_error integer
                );
                """
            )
            rows = [
                (
                    "claude-parent", "claude_code", "/work/alpha",
                    "2026-07-18T00:00:00Z", "2026-07-18T00:10:00Z",
                    600, 10, 5, 20, 0, 0.5, "claude-test", 1, "2.0", "v1",
                ),
                (
                    "codex-parent", "codex", "/work/beta",
                    "2026-07-18T01:00:00Z", "2026-07-18T01:10:00Z",
                    600, 100, 50, 200, 0, 0, "gpt-test", 1, "1.0", None,
                ),
            ]
            connection.executemany(
                "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.executemany(
                "insert into turns values (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "ct", "claude-parent", "2026-07-18T00:01:00Z",
                        "claude-test", 10, 5, 20, 0, 0.5,
                    ),
                    (
                        "xt", "codex-parent", "2026-07-18T01:01:00Z",
                        "gpt-test", 100, 50, 200, 0, 0,
                    ),
                ],
            )
            connection.commit()
            connection.close()

            combined = build_argus_stats(path)
            self.assertEqual(
                combined["totals"]["sessions_by_agent"],
                {"claude_code": 1, "codex": 1},
            )
            self.assertEqual(
                {row["agent"] for row in combined["daily_models"]},
                {"claude_code", "codex"},
            )

            filtered = build_argus_stats(path, agent="claude_code")
            self.assertEqual(
                [row["key"] for row in filtered["sessions"]],
                ["claude_code:claude-parent"],
            )
            self.assertEqual(filtered["project_keys"], ["/work/alpha"])


class CombinedSessionsTests(unittest.TestCase):
    @staticmethod
    def _good_payload() -> dict:
        return {
            "source": {
                "id": "good",
                "label": "Good",
                "cost_status": "estimated",
            },
            "meta_priority": 1,
            "meta": {"pricing_version": "v1"},
            "totals": {"sessions": 1, "tokens": 10, "cost_usd": 1.5},
            "project_keys": ["/work/a"],
            "sessions": [
                {
                    "id": "one",
                    "key": "good:one",
                    "agent": "good",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ],
            "daily_models": [],
            "daily_sessions": [],
            "daily_tools": [],
        }

    def test_one_bad_provider_does_not_hide_healthy_data(self) -> None:
        good = SimpleNamespace(
            SOURCE_ID="good",
            SOURCE_LABEL="Good",
            COST_STATUS="estimated",
            collect_session_telemetry=self._good_payload,
        )
        bad = SimpleNamespace(
            SOURCE_ID="bad",
            SOURCE_LABEL="Bad",
            COST_STATUS="unavailable",
            collect_session_telemetry=mock.Mock(
                side_effect=RuntimeError("not ready")
            ),
        )

        def load(_capability: str, *, agent: str):
            return {"bad": bad, "good": good}[agent]

        with mock.patch.object(
            session_telemetry,
            "list_capability_providers",
            return_value=["bad", "good"],
        ), mock.patch.object(
            session_telemetry,
            "try_load_capability",
            side_effect=load,
        ):
            data = session_telemetry.build_session_telemetry()

        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(data["sessions"][0]["key"], "good:one")
        statuses = {
            row["id"]: row["status"] for row in data["meta"]["sources"]
        }
        self.assertEqual(statuses, {"bad": "unavailable", "good": "available"})

    def test_unavailable_provider_is_logged_once_until_it_changes(self) -> None:
        # The watcher rebuilds this view every tick; a runtime that is simply
        # not installed must not repeat the same warning on every rebuild.
        good = SimpleNamespace(
            SOURCE_ID="good",
            SOURCE_LABEL="Good",
            COST_STATUS="estimated",
            collect_session_telemetry=self._good_payload,
        )
        flaky = SimpleNamespace(
            SOURCE_ID="flaky",
            SOURCE_LABEL="Flaky",
            COST_STATUS="unavailable",
            collect_session_telemetry=mock.Mock(
                side_effect=[
                    RuntimeError("state DB not found"),
                    RuntimeError("state DB not found"),
                    RuntimeError("permission denied"),
                    self._good_payload(),
                ]
            ),
        )

        def load(_capability: str, *, agent: str):
            return {"flaky": flaky, "good": good}[agent]

        session_telemetry._unavailable_reasons.clear()
        self.addCleanup(session_telemetry._unavailable_reasons.clear)
        with mock.patch.object(
            session_telemetry,
            "list_capability_providers",
            return_value=["flaky", "good"],
        ), mock.patch.object(
            session_telemetry,
            "try_load_capability",
            side_effect=load,
        ), mock.patch("builtins.print") as fake_print:
            for _ in range(4):
                session_telemetry.build_session_telemetry()

        lines = [str(call.args[0]) for call in fake_print.call_args_list]
        self.assertEqual(
            [line.split(" — ")[0] for line in lines],
            [
                "⚠️ session telemetry provider flaky unavailable (state DB not found)",
                "⚠️ session telemetry provider flaky unavailable (permission denied)",
                "✅ session telemetry provider flaky available again",
            ],
        )


class SessionPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_claude_prompts_are_lazy_clean_and_grouped_by_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            transcript = projects / "encoded-project" / "session-1.jsonl"
            transcript.parent.mkdir(parents=True)
            rows = [
                {
                    "type": "user",
                    "timestamp": "2026-07-25T01:00:00Z",
                    "message": {
                        "content": "Plan this "
                        "<system-reminder>DO_NOT_LEAK</system-reminder>"
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "reply"},
                            {"type": "tool_use", "name": "Read"},
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "content": "ignored"}]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-07-25T01:01:00Z",
                    "message": {
                        "content": "<command-name>/review</command-name>"
                        "<command-args>src</command-args>"
                    },
                },
            ]
            transcript.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"CLAUDE_PROJECTS_DIR": str(projects)},
            ):
                data = session_prompts.collect_session_prompts("session-1")

        self.assertEqual(data["total_prompts"], 2)
        self.assertEqual(data["prompts"][0]["text"], "Plan this")
        self.assertEqual(data["prompts"][0]["responses"], 1)
        self.assertEqual(data["prompts"][0]["tool_uses"], 1)
        self.assertEqual(data["prompts"][1]["text"], "/review src")
        self.assertNotIn("DO_NOT_LEAK", json.dumps(data))

    async def test_prompt_endpoint_degrades_for_unsupported_source(self) -> None:
        space._session_prompts_cache.clear()
        response = await space.session_prompts_data("cursor", "session-1")
        body = json.loads(response.body)
        self.assertFalse(body["supported"])
        self.assertEqual(body["prompts"], [])


if __name__ == "__main__":
    unittest.main()
