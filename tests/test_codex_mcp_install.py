"""Tests for the codex ``mcp_install`` capability.

Every test redirects ``CODEX_HOME`` at a temp dir, so nothing here touches a
real ``~/.codex/config.toml``.

The thing worth protecting is the text splice: codex's config is hand-written
TOML with comments, and the installer preserves it byte for byte rather than
round-tripping it through a parser. The tests below pin both halves of that —
the composio table lands, and everything else survives untouched.
"""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.codex import mcp_install
from services.cowork_agent.composio import service as composio_service

PROXY = "http://127.0.0.1:5002/mcp/composio-proxy/u/tok-1"


class CodexMcpInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "codex"
        self.home.mkdir()
        self.config = self.home / "config.toml"

        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def _write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def _parsed(self) -> dict:
        return tomllib.loads(self.config.read_text(encoding="utf-8"))

    # ---- the happy paths ----

    def test_config_path_follows_codex_home(self) -> None:
        self.assertEqual(mcp_install.config_path(), self.config)

    def test_a_missing_config_is_created(self) -> None:
        # Codex runs fine with no config file, so "absent" must not mean
        # "unconnectable" the way it does for ~/.claude.json.
        result = mcp_install.install(PROXY)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertEqual(
            self._parsed()["mcp_servers"]["composio"],
            {"url": PROXY, "enabled": True},
        )

    def test_a_created_config_is_private(self) -> None:
        mcp_install.install(PROXY)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_an_existing_config_keeps_its_own_mode(self) -> None:
        self._write('model = "gpt-5"\n')
        self.config.chmod(0o644)
        mcp_install.install(PROXY)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)

    def test_unrelated_settings_and_comments_survive_verbatim(self) -> None:
        self._write(
            "# my codex config\n"
            'model = "gpt-5"\n'
            'approval_policy = "on-request"\n'
            "\n"
            "[sandbox_workspace_write]\n"
            "network_access = true  # needed for npm\n"
        )
        self.assertTrue(mcp_install.install(PROXY)["ok"])
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("# my codex config", text)
        self.assertIn("network_access = true  # needed for npm", text)
        self.assertEqual(self._parsed()["model"], "gpt-5")

    def test_a_second_install_is_a_no_op(self) -> None:
        mcp_install.install(PROXY)
        before = self.config.read_text(encoding="utf-8")
        result = mcp_install.install(PROXY)
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["restart_required"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_a_new_token_replaces_the_old_entry_rather_than_adding_one(self) -> None:
        mcp_install.install(PROXY)
        rotated = PROXY.replace("tok-1", "tok-2")
        self.assertTrue(mcp_install.install(rotated)["changed"])
        text = self.config.read_text(encoding="utf-8")
        self.assertEqual(text.count("[mcp_servers.composio]"), 1)
        self.assertEqual(self._parsed()["mcp_servers"]["composio"]["url"], rotated)

    def test_other_mcp_servers_are_left_alone(self) -> None:
        self._write(
            "[mcp_servers.docs]\n"
            'command = "npx"\n'
            'args = ["-y", "@some/docs-mcp"]\n'
        )
        self.assertTrue(mcp_install.install(PROXY)["ok"])
        servers = self._parsed()["mcp_servers"]
        self.assertEqual(servers["docs"]["args"], ["-y", "@some/docs-mcp"])
        self.assertIn("composio", servers)

    def test_a_legacy_table_is_removed_so_tools_are_not_listed_twice(self) -> None:
        self._write(f'[mcp_servers.cowork]\nurl = "{PROXY}"\nenabled = true\n')
        self.assertTrue(mcp_install.install(PROXY)["ok"])
        servers = self._parsed()["mcp_servers"]
        self.assertNotIn("cowork", servers)
        self.assertIn("composio", servers)

    def test_a_quoted_table_header_is_still_recognised(self) -> None:
        self._write(f'[mcp_servers."composio"]\nurl = "http://stale/"\n')
        self.assertTrue(mcp_install.install(PROXY)["ok"])
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("http://stale/", text)
        self.assertEqual(self._parsed()["mcp_servers"]["composio"]["url"], PROXY)

    def test_no_credential_is_written_into_the_config(self) -> None:
        # The whole point of the loopback proxy: the Composio key stays server
        # side, so nothing secret may reach an agent's config file.
        with patch.dict(os.environ, {"COMPOSIO_API_KEY": "secret-key"}):
            mcp_install.install(PROXY)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("secret-key", text)
        self.assertNotIn("bearer", text.lower())

    # ---- the refusals ----

    def test_no_proxy_url_is_refused(self) -> None:
        self.assertFalse(mcp_install.install("")["ok"])
        self.assertFalse(self.config.exists())

    def test_an_unparseable_config_is_never_rewritten(self) -> None:
        broken = "model = = = \n"
        self._write(broken)
        result = mcp_install.install(PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not valid TOML", result["error"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), broken)

    def test_an_inline_mcp_servers_table_is_refused_not_corrupted(self) -> None:
        # Appending [mcp_servers.composio] under an inline `mcp_servers = {...}`
        # is a duplicate-key error; the guard catches it before the write.
        inline = 'mcp_servers = { docs = { command = "npx" } }\n'
        self._write(inline)
        result = mcp_install.install(PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("inline table", result["error"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), inline)

    def test_a_splice_that_would_move_other_settings_aborts(self) -> None:
        # _strip_tables is line-based, so this is the backstop: if a chop ever
        # eats a neighbouring key, the before/after comparison refuses the write.
        self._write('model = "gpt-5"\n')
        with patch.object(
            mcp_install, "_strip_tables", side_effect=lambda lines, patterns: []
        ):
            result = mcp_install.install(PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("outside mcp_servers", result["error"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), 'model = "gpt-5"\n')

    def test_a_url_carrying_a_quote_cannot_break_out_of_the_string(self) -> None:
        hostile = 'http://127.0.0.1/u/"\nmodel = "pwned"\n'
        self.assertTrue(mcp_install.install(hostile)["ok"])
        parsed = self._parsed()
        self.assertEqual(parsed["mcp_servers"]["composio"]["url"], hostile)
        self.assertNotIn("model", parsed)


class CodexGatewayWiringTests(unittest.TestCase):
    """The capability is what makes codex a supported gateway target."""

    def setUp(self) -> None:
        # install_into_gateway mints a proxy token, which writes the session
        # store and takes a lock under quirq state. Both are redirected here —
        # see the header of tests/test_composio.py for the same two traps.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)

        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(tmp / "quirq")})
        env.start()
        self.addCleanup(env.stop)

        store = patch.object(
            composio_service, "_SESSIONS_PATH", tmp / "data" / "composio_sessions.json"
        )
        store.start()
        self.addCleanup(store.stop)

        self._reset_caches()
        self.addCleanup(self._reset_caches)

    @staticmethod
    def _reset_caches() -> None:
        composio_service._SESSION_IDS.clear()
        composio_service._PROXY_TOKENS.clear()
        composio_service._SESSIONS_LOADED = False

    def test_codex_is_listed_as_a_gateway_install_target(self) -> None:
        self.assertIn("codex", composio_service.gateway_install_agents())

    def test_install_into_gateway_reaches_the_codex_capability(self) -> None:
        with patch.object(mcp_install, "install", return_value={"ok": True}) as installed:
            result = composio_service.install_into_gateway(
                "user_1__ws__ws-test", "codex",
            )
        self.assertTrue(result["ok"])
        installed.assert_called_once()
        self.assertIn("/mcp/composio-proxy/u/", installed.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
