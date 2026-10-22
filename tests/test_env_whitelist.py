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
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        state_root = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": tmp.name})
        state_root.start()
        self.addCleanup(state_root.stop)

    def write_router_env(self, text: str) -> None:
        (self.state / "secrets").mkdir(exist_ok=True)
        (self.state / "secrets" / "router.env").write_text(text)

    def test_passes_the_baseline_xo_address_store_and_names_only(self) -> None:
        environ = {"PATH": "/bin", "HOME": "/h", "LC_CTYPE": "C", "XDG_DATA_HOME": "/d", "NVM_BIN": "/n",
                   "https_proxy": "p", "NODE_EXTRA_CA_CERTS": "/ca", "HOST": "0.0.0.0", "PORT": "5302",
                   "WANTED": "w", "STORED": "live", "XO_API_KEY": "secret", "JEV_API_KEY": "secret"}
        env = ew.base_env(names=("WANTED", "ABSENT"), environ=environ, store={"STORED": "file", "FILE_ONLY": "f"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "LC_CTYPE": "C", "XDG_DATA_HOME": "/d",
                               "NVM_BIN": "/n", "https_proxy": "p", "NODE_EXTRA_CA_CERTS": "/ca", "HOST": "0.0.0.0",
                               "PORT": "5302", "WANTED": "w", "STORED": "live", "FILE_ONLY": "f"})

    def test_git_signs_in_as_in_the_person_s_terminal_but_opens_no_editor(self) -> None:
        """§0.111: git's askpass helper, ssh command and ssh agent pass (on
        Coder the first two run ``coder gitssh``); ``GIT_EDITOR`` doesn't."""
        environ = {"PATH": "/bin", "GIT_ASKPASS": "/tmp/coder/askpass", "GIT_SSH_COMMAND": "/tmp/coder gitssh --",
                   "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock", "GIT_EDITOR": "vim"}
        env = ew.base_env(environ=environ, store={})
        self.assertEqual(env, {k: v for k, v in environ.items() if k != "GIT_EDITOR"})

    def test_the_never_list_beats_every_name_the_server_env_gives(self) -> None:
        """An MCP config naming a never-list variable doesn't get it: XO's
        credentials, another session's tokens, a decision-model key, and what
        a parent CLI session handed the server, for every installed agent."""
        self.write_router_env("ROUTER_KEY=v\n")
        never = ("XO_API_KEY", "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN", "XO_COMMS_TOKEN", "XO_SESSION_TOKEN",
                 "ROUTER_KEY", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "CODEX_API_KEY", "CODEX_THREAD_ID")
        environ = {"PATH": "/bin", "GH_PAT": "p", "CODEX_HOME": "/c", **{name: "server-value" for name in never}}
        env = ew.base_env(names=(*never, "GH_PAT", "CODEX_HOME"), environ=environ, store={})
        self.assertEqual(env, {"PATH": "/bin", "GH_PAT": "p", "CODEX_HOME": "/c"})

    def test_a_stored_key_passes_even_on_the_never_list(self) -> None:
        env = ew.base_env(environ={"PATH": "/bin", "CLAUDE_CODE_USE_BEDROCK": "1"},
                          store={"CLAUDE_CODE_USE_BEDROCK": "0", "XO_API_KEY": "mine"})
        self.assertEqual(env, {"PATH": "/bin", "CLAUDE_CODE_USE_BEDROCK": "1", "XO_API_KEY": "mine"})

    def test_never_list_reads_router_env_names_at_call_time(self) -> None:
        self.assertNotIn("ROUTER_KEY", ew.never_list())             # no router.env yet
        self.write_router_env("# decision models\nROUTER_KEY=v\nOTHER_ROUTER_KEY=w\n")
        never = ew.never_list()
        for name in ("ROUTER_KEY", "OTHER_ROUTER_KEY", "XO_API_KEY", "CLAUDECODE", "CLAUDE_CODE_X", "CODEX_X"):
            self.assertIn(name, never)
        for name in ("CODEX_HOME", "GH_PAT", "CLAUDE_CONFIG_DIR", "PATH"):
            self.assertNotIn(name, never)

    def test_an_unreadable_router_env_fails_the_spawn(self) -> None:
        (self.state / "secrets" / "router.env").mkdir(parents=True)
        with self.assertRaises(OSError):
            ew.base_env(environ={"PATH": "/bin"}, store={})

    def test_a_malformed_inherited_env_block_fails_loudly(self) -> None:
        broken = mock.Mock(raw={ew.INHERITED_ENV_KEY: {"names": "CLAUDECODE"}})
        broken.name = "broken"
        with mock.patch.object(ew, "all_agents", return_value=[broken]), \
                self.assertRaisesRegex(ValueError, "inherited_env.names"):
            ew.never_list()

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
