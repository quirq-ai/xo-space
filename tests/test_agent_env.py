from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api._filters import preview_value
from services.cowork_agent.registry import agent_env


class AgentEnvTests(unittest.TestCase):
    def test_mask_never_leaks_value_fragments_or_length(self) -> None:
        self.assertEqual(preview_value("short"), "••••••")
        self.assertEqual(
            preview_value("prefix-a-very-long-secret-suffix"),
            "••••••",
        )
        self.assertIsNone(preview_value(""))

    def test_writes_are_private_atomic_and_update_process_environment(self) -> None:
        keys = ("QUIRQ_TEST_ALPHA", "QUIRQ_TEST_BETA")
        original = {key: os.environ.get(key) for key in keys}

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                env_file = Path(temp_dir) / "secrets.env"
                with patch.object(agent_env, "ENV_FILE", env_file):
                    agent_env.save_env_entries(
                        [
                            {"key": keys[0], "value": "first"},
                            {"key": keys[1], "value": "second"},
                        ]
                    )

                    self.assertEqual(
                        stat.S_IMODE(env_file.stat().st_mode),
                        0o600,
                    )
                    self.assertEqual(os.environ[keys[0]], "first")
                    self.assertEqual(os.environ[keys[1]], "second")

                    agent_env.upsert_env_entry(keys[0], "replacement")
                    self.assertEqual(os.environ[keys[0]], "replacement")

                    agent_env.save_env_entries(
                        [{"key": keys[1], "value": "second"}]
                    )
                    self.assertNotIn(keys[0], os.environ)
                    self.assertEqual(
                        agent_env.load_env_entries(),
                        [{"key": keys[1], "value": "second"}],
                    )
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
