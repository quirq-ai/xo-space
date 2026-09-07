import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.visualizer.session_telemetry import _validate_contribution


class TestExpandedSessionTelemetryHarnesses(unittest.TestCase):
    def test_providers_discovered(self):
        providers = list_capability_providers("session_telemetry")
        for expected in ("kimi_code", "deepseek_harness", "pi_agent", "opencode", "omp", "grok_build"):
            self.assertIn(expected, providers)

    @patch("services.cowork_agent.adapters.kimi_code.session_telemetry._kimi_home")
    def test_kimi_code_telemetry_missing_dir(self, mock_home):
        mock_home.return_value = Path("/nonexistent/kimi/path")
        from services.cowork_agent.adapters.kimi_code.session_telemetry import collect_session_telemetry
        with self.assertRaises(FileNotFoundError):
            collect_session_telemetry()

    @patch("services.cowork_agent.adapters.deepseek_harness.session_telemetry._dsh_home")
    def test_deepseek_harness_telemetry_missing_dir(self, mock_home):
        mock_home.return_value = Path("/nonexistent/dsh/path")
        from services.cowork_agent.adapters.deepseek_harness.session_telemetry import collect_session_telemetry
        with self.assertRaises(FileNotFoundError):
            collect_session_telemetry()

    @patch("services.cowork_agent.adapters.kimi_code.session_telemetry._kimi_home")
    def test_kimi_code_parsing_with_fixture(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            
            sample_session = {
                "id": "sess_kimi_001",
                "project_path": "/home/user/projects/demo",
                "model": "kimi-k3",
                "created_at": "2026-09-01T12:00:00Z",
                "updated_at": "2026-09-01T12:10:00Z",
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 300,
                    "total_tokens": 1800
                }
            }
            (sessions_dir / "sess_kimi_001.json").write_text(json.dumps(sample_session), encoding="utf-8")
            
            mock_home.return_value = tmp_path
            from services.cowork_agent.adapters.kimi_code.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 1800)
            self.assertEqual(data["sessions"][0]["model"], "kimi-k3")
            self.assertEqual(data["sessions"][0]["project_path"], "/home/user/projects/demo")

    @patch("services.cowork_agent.adapters.deepseek_harness.session_telemetry._dsh_home")
    def test_deepseek_harness_parsing_with_fixture(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            logs_dir = tmp_path / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            
            sample_jsonl = [
                json.dumps({"time": "2026-09-02T10:00:00Z", "model": "deepseek-reasoner", "cwd": "/home/user/projects/ai"}),
                json.dumps({"time": "2026-09-02T10:05:00Z", "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}})
            ]
            (logs_dir / "sess_dsh_001.jsonl").write_text("\n".join(sample_jsonl), encoding="utf-8")
            
            mock_home.return_value = tmp_path
            from services.cowork_agent.adapters.deepseek_harness.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 600)
            self.assertEqual(data["sessions"][0]["model"], "deepseek-reasoner")

    @patch("services.cowork_agent.adapters.pi_agent.session_telemetry._pi_home")
    def test_pi_agent_parsing_with_fixture(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            
            sample_session = {
                "session_id": "sess_pi_001",
                "cwd": "/home/user/projects/pi_demo",
                "model": "pi-agent-v1",
                "created_at": "2026-09-03T15:00:00Z",
                "usage": {"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000}
            }
            (sessions_dir / "sess_pi_001.json").write_text(json.dumps(sample_session), encoding="utf-8")
            
            mock_home.return_value = tmp_path
            from services.cowork_agent.adapters.pi_agent.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 1000)
            self.assertEqual(data["sessions"][0]["model"], "pi-agent-v1")

    @patch("services.cowork_agent.adapters.opencode.session_telemetry._opencode_roots")
    def test_opencode_sqlite_parsing_with_fixture(self, mock_roots):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "opencode.db"
            
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    directory TEXT,
                    title TEXT,
                    time_created INTEGER,
                    time_updated INTEGER,
                    model TEXT,
                    cost REAL,
                    tokens_input INTEGER,
                    tokens_output INTEGER,
                    tokens_reasoning INTEGER,
                    tokens_cache_read INTEGER,
                    tokens_cache_write INTEGER
                )
            """)
            con.execute("""
                INSERT INTO session VALUES (
                    'ses_opencode_001',
                    '/home/user/projects/opencode_demo',
                    'Test Session',
                    1787149480663,
                    1787149665436,
                    '{"id":"deepseek-v4-flash-free","providerID":"opencode"}',
                    0.0,
                    5000,
                    1000,
                    0,
                    20000,
                    0
                )
            """)
            con.commit()
            con.close()
            
            mock_roots.return_value = [tmp_path]
            from services.cowork_agent.adapters.opencode.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 26000)  # 5000 + 1000 + 20000
            self.assertEqual(data["sessions"][0]["model"], "deepseek-v4-flash-free")
            self.assertEqual(data["sessions"][0]["project_path"], "/home/user/projects/opencode_demo")

    @patch("services.cowork_agent.adapters.omp.session_telemetry._omp_home")
    def test_omp_parsing_with_fixture(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sessions_dir = tmp_path / "agent" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            
            sample_session = {
                "id": "sess_omp_001",
                "cwd": "/home/user/projects/omp_demo",
                "model": "omp-agent-v1",
                "created_at": "2026-09-04T10:00:00Z",
                "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}
            }
            (sessions_dir / "sess_omp_001.json").write_text(json.dumps(sample_session), encoding="utf-8")
            
            mock_home.return_value = tmp_path
            from services.cowork_agent.adapters.omp.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 1500)
            self.assertEqual(data["sessions"][0]["model"], "omp-agent-v1")

    @patch("services.cowork_agent.adapters.grok_build.session_telemetry._grok_home")
    def test_grok_build_parsing_with_fixture(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            
            sample_session = {
                "id": "sess_grok_001",
                "cwd": "/home/user/projects/grok_demo",
                "model": "grok-code",
                "created_at": "2026-09-05T08:00:00Z",
                "usage": {"input_tokens": 4000, "output_tokens": 500, "total_tokens": 4500}
            }
            (sessions_dir / "sess_grok_001.json").write_text(json.dumps(sample_session), encoding="utf-8")
            
            mock_home.return_value = tmp_path
            from services.cowork_agent.adapters.grok_build.session_telemetry import collect_session_telemetry
            data = collect_session_telemetry()
            
            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 4500)
            self.assertEqual(data["sessions"][0]["model"], "grok-code")


if __name__ == "__main__":
    unittest.main()
