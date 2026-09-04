import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.visualizer.session_telemetry import build_session_telemetry


class TestExpandedSessionTelemetryHarnesses(unittest.TestCase):
    def test_providers_discovered(self):
        providers = list_capability_providers("session_telemetry")
        for expected in ("kimi_code", "deepseek_harness", "pi_agent", "opencode", "omp", "grok_build"):
            self.assertIn(expected, providers)

    @patch("services.cowork_agent.adapters.kimi_code.session_telemetry._kimi_home")
    def test_kimi_code_telemetry_missing_dir(self, mock_home):
        mock_home.return_value = Path("/nonexistent/kimi/path")
        # Should raise FileNotFoundError gracefully when directory missing
        from services.cowork_agent.adapters.kimi_code.session_telemetry import collect_session_telemetry
        with self.assertRaises(FileNotFoundError):
            collect_session_telemetry()

    @patch("services.cowork_agent.adapters.deepseek_harness.session_telemetry._dsh_home")
    def test_deepseek_harness_telemetry_missing_dir(self, mock_home):
        mock_home.return_value = Path("/nonexistent/dsh/path")
        from services.cowork_agent.adapters.deepseek_harness.session_telemetry import collect_session_telemetry
        with self.assertRaises(FileNotFoundError):
            collect_session_telemetry()

if __name__ == "__main__":
    unittest.main()
