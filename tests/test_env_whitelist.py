"""The agent-neutral env whitelist every storm runtime builds on (storm ch 16 §1)."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import env_whitelist as ew
from services.cowork_agent.registry import agent_env


class BaseEnvTests(unittest.TestCase):
    def test_passes_the_baseline_xo_address_store_and_names_only(self) -> None:
        environ = {"PATH": "/bin", "HOME": "/h", "LC_CTYPE": "C", "XDG_DATA_HOME": "/d", "NVM_BIN": "/n",
                   "https_proxy": "p", "NODE_EXTRA_CA_CERTS": "/ca", "HOST": "0.0.0.0", "PORT": "5302",
                   "WANTED": "w", "STORED": "live", "XO_API_KEY": "secret", "JEV_API_KEY": "secret"}
        env = ew.base_env(names=("WANTED", "ABSENT"), environ=environ, store={"STORED": "file", "FILE_ONLY": "f"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "LC_CTYPE": "C", "XDG_DATA_HOME": "/d",
                               "NVM_BIN": "/n", "https_proxy": "p", "NODE_EXTRA_CA_CERTS": "/ca", "HOST": "0.0.0.0",
                               "PORT": "5302", "WANTED": "w", "STORED": "live", "FILE_ONLY": "f"})

    def test_drop_removes_inherited_names_never_stored_keys(self) -> None:
        env = ew.base_env(names=("X_TOKEN",), environ={"X_TOKEN": "a", "PATH": "/bin"}, store={"X_KEY": "b"},
                          drop=lambda name: name.startswith("X_"))
        self.assertEqual(env, {"PATH": "/bin", "X_KEY": "b"})

    def test_stored_env_reads_the_secrets_store_and_runtime_env_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets.env"
            runtime = Path(tmp) / "runtime.env"
            secrets.write_text("# comment\nGITHUB_TOKEN=one\n")
            runtime.write_text("AGENT_NAME=claude_code\n")
            with mock.patch.object(agent_env, "ENV_FILE", secrets), \
                    mock.patch.dict(os.environ, {"QUIRQ_RUNTIME_FILE": str(runtime)}):
                self.assertEqual(ew.stored_env(), {"GITHUB_TOKEN": "one", "AGENT_NAME": "claude_code"})
                secrets.write_text("GITHUB_TOKEN=two\nNEW_KEY=x\n")
                self.assertEqual(ew.stored_env()["NEW_KEY"], "x")
                runtime.unlink()
                self.assertEqual(ew.stored_env(), {"GITHUB_TOKEN": "two", "NEW_KEY": "x"})

    def test_referenced_vars_reads_names_only(self) -> None:
        config = {"a": {"env": {"K": "${ONE}", "L": "plain"}, "args": ["--x=${TWO:-dflt}", 3, None]},
                  "b": [{"headers": {"Authorization": "Bearer ${THREE}"}}], "c": "$NOT_BRACED ${4BAD}"}
        self.assertEqual(ew.referenced_vars(config), {"ONE", "TWO", "THREE"})

    def test_dropped_names_are_logged_never_values(self) -> None:
        with self.assertLogs("services.cowork_agent.adapters.env_whitelist", logging.DEBUG) as logs:
            ew.log_dropped("claude session cse_1", {"PATH": "/bin"},
                           {"PATH": "/bin", "XO_API_KEY": "super-secret-value"})
        self.assertIn("XO_API_KEY", logs.output[0])
        self.assertNotIn("super-secret-value", logs.output[0])


if __name__ == "__main__":
    unittest.main()
