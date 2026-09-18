"""Space session telemetry for the gateway-backed agents (hermes, openclaw).

Both providers read the agent's own store read-only and are discovered like
every other ``session_telemetry`` capability. Each test builds a small store
in a temporary home, runs the provider through the core payload validator, and
checks the numbers, the project join through the session index, and that an
agent that is not installed is reported unavailable rather than shown.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.hermes import session_telemetry as hermes_telemetry
from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.adapters.openclaw import session_telemetry as openclaw_telemetry
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.visualizer import session_telemetry


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.projects = self.base / "projects"
        (self.projects / "demo" / ".xo").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.projects),
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "QUIRQ_COMMAND_LOG": "off",
        })
        env.start()
        self.addCleanup(env.stop)

    def index_row(self, backend: str, native: str) -> None:
        sessions_io.write_session_row("demo", f"{backend}:demo:web:{native[:8]}", {
            "sessionId": native, "nativeSessionId": native,
            "directory": str(self.projects / "demo"), "backend": backend,
        })

    @staticmethod
    def validate(module) -> dict:
        return session_telemetry._validate_contribution(
            module.SOURCE_ID, module, module.collect_session_telemetry(),
        )


class DiscoveryTests(unittest.TestCase):
    def test_both_providers_are_discovered(self) -> None:
        providers = list_capability_providers("session_telemetry")
        self.assertIn("hermes", providers)
        self.assertIn("openclaw", providers)


class HermesTelemetryTests(_Sandbox):
    def state_db(self, path: Path, sessions: list[tuple], messages: list[tuple]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript(
            "create table sessions (id text, model text, started_at real, ended_at real,"
            " last_activity_at real, cwd text, parent_session_id text, input_tokens integer,"
            " output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,"
            " reasoning_tokens integer, estimated_cost_usd real, actual_cost_usd real,"
            " cost_status text, title text);"
            "create table messages (id integer primary key, session_id text, role text,"
            " content text, tool_name text, timestamp real);"
        )
        connection.executemany(
            "insert into sessions (id, model, started_at, last_activity_at, parent_session_id,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " reasoning_tokens, estimated_cost_usd, cost_status, title)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,'secret title')",
            sessions,
        )
        connection.executemany(
            "insert into messages (session_id, role, content, tool_name, timestamp)"
            " values (?,?,'secret text',?,?)",
            messages,
        )
        connection.commit()
        connection.close()
        return path

    def test_sessions_tokens_tools_and_project_come_from_state_db(self) -> None:
        day = 1789500000.0  # 2026-09-15T19:20:00Z
        db = self.state_db(self.base / "hermes" / "state.db", [
            ("root-1", "gpt-x", day, day + 60, None, 1000, 200, 5000, 0, 50, 0.0, "included"),
            ("child-1", "gpt-x", day + 10, day + 50, "root-1", 100, 20, 0, 0, 0, None, "unknown"),
            ("empty-1", "gpt-x", day, day, None, 0, 0, 0, 0, 0, None, "unknown"),
        ], [
            ("root-1", "user", None, day), ("root-1", "tool", "terminal", day + 1),
            ("root-1", "tool", "terminal", day + 2), ("child-1", "tool", "patch", day + 20),
        ])
        self.index_row("hermes", "root-1")
        with mock.patch.object(hermes_telemetry, "_profile_state_dbs", return_value=[("default", db)]):
            data = self.validate(hermes_telemetry)

        self.assertEqual(data["totals"]["sessions"], 1, "a tokenless root is not a session")
        # Reasoning is inside output: 1000 + 200 + 5000, plus the child's 120.
        self.assertEqual(data["totals"]["tokens"], 6320)
        [row] = data["sessions"]
        self.assertEqual((row["id"], row["agent"], row["project"]), ("root-1", "hermes", "demo"))
        self.assertEqual((row["fresh"], row["output"], row["cache_read"]), (1100, 220, 5000))
        self.assertEqual((row["own_tokens"], row["total_tokens"]), (6200, 6320))
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["started_at"], "2026-09-15T19:20:00.000Z")
        self.assertEqual(row["tools"][0], {"name": "terminal", "calls": 2, "errors": 0})
        self.assertEqual([sub["id"] for sub in row["subagents"]], ["child-1"])
        self.assertFalse(row["cost_known"], "the child's cost status is unknown")
        self.assertNotIn("secret", json.dumps(data))

    def test_not_installed_is_unavailable(self) -> None:
        with mock.patch.object(hermes_telemetry, "_profile_state_dbs", return_value=[]):
            with self.assertRaises(FileNotFoundError):
                hermes_telemetry.collect_session_telemetry()


class OpenClawTelemetryTests(_Sandbox):
    def transcript(self, agents: Path, agent: str, sid: str, lines: list[dict]) -> None:
        sessions = agents / agent / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{sid}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8",
        )

    def test_sessions_tokens_cost_and_project_come_from_transcripts(self) -> None:
        agents = self.base / "openclaw" / "agents"
        self.transcript(agents, "main", "oc-1", [
            {"type": "session", "id": "oc-1", "timestamp": "2026-09-15T10:00:00.000Z"},
            {"type": "message", "timestamp": "2026-09-15T10:00:01.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "secret prompt"}]}},
            {"type": "message", "timestamp": "2026-09-15T10:00:05.000Z",
             "message": {"role": "assistant", "model": "claude-x", "provider": "anthropic",
                         "usage": {"input": 300, "output": 40, "cacheRead": 1000, "cacheWrite": 60,
                                   "cost": {"total": 0.25}},
                         "content": [{"type": "toolCall", "name": "exec"}]}},
        ])
        self.transcript(agents, "main", "oc-empty", [
            {"type": "session", "id": "oc-empty", "timestamp": "2026-09-15T11:00:00.000Z"},
        ])
        self.index_row("openclaw", "oc-1")
        with mock.patch.dict(os.environ, {"OPENCLAW_AGENTS_DIR": str(agents)}):
            data = self.validate(openclaw_telemetry)

        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(data["totals"]["tokens"], 1400)
        self.assertEqual(data["totals"]["cost_usd"], 0.25)
        [row] = data["sessions"]
        self.assertEqual((row["id"], row["agent"], row["project"], row["model"]), ("oc-1", "openclaw", "demo", "claude-x"))
        self.assertEqual((row["turns"], row["cost_known"]), (1, True))
        self.assertEqual(row["started_at"], "2026-09-15T10:00:01.000Z")
        self.assertEqual(row["tools"], [{"name": "exec", "calls": 1, "errors": 0}])
        self.assertEqual(data["daily_models"][0]["day"], "2026-09-15")
        self.assertNotIn("secret", json.dumps(data))

    def test_not_installed_is_unavailable(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCLAW_AGENTS_DIR": str(self.base / "absent")}):
            with self.assertRaises(FileNotFoundError):
                openclaw_telemetry.collect_session_telemetry()


if __name__ == "__main__":
    unittest.main()
