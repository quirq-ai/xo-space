"""Tests for the manifest-driven Composio MCP install (``composio/mcp.py``).

Two halves, matching the module:

*Reader* — the ``"mcp"`` block in each ``config/agents/<name>/manifest.json`` parses,
resolves to the right file, and a malformed block degrades to ``None`` instead of
raising on the boot path.

*Writer* — the shipped blocks actually edit the four config shapes correctly. The
thing most worth protecting is the TOML splice: codex's config is hand-written with
comments, and it is edited as text rather than round-tripped through a parser, so the
tests pin both halves — the composio table lands, and everything else survives
untouched.

Every test redirects at a temp file (via ``dataclasses.replace`` on the target, or
``CODEX_HOME``), so nothing here touches a real agent config.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.cowork_agent.connectors.composio import mcp
from services.cowork_agent.connectors.composio import service as composio_service

PROXY = "http://127.0.0.1:5002/mcp/composio-proxy/u/tok-1"


def _target(agent: str, path: Path) -> mcp.McpTarget:
    """The agent's real shipped block, redirected at ``path``."""
    target = mcp.load_target(agent)
    assert target is not None, f"{agent} should declare an mcp block"
    return dataclasses.replace(target, path=path)


class _TempConfig(unittest.TestCase):
    """A temp dir plus the read/write helpers the format suites share."""

    filename = "config"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.config = self.dir / self.filename

    def _write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def _text(self) -> str:
        return self.config.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Reader — the manifest blocks
# ══════════════════════════════════════════════════════════════════════════════


class ManifestBlockTests(unittest.TestCase):
    def test_the_four_shipped_agents_declare_a_usable_block(self) -> None:
        self.assertEqual(
            mcp.agents_with_targets(), ["claude_code", "codex", "hermes", "openclaw"]
        )

    def test_each_block_resolves_to_the_file_that_agent_actually_reads(self) -> None:
        expected = {
            "claude_code": Path.home() / ".claude.json",
            "codex": Path.home() / ".codex" / "config.toml",
            "hermes": Path.home() / ".hermes" / "config.yaml",
            "openclaw": Path.home() / ".openclaw" / "openclaw.json",
        }
        for agent, path in expected.items():
            with self.subTest(agent=agent):
                self.assertEqual(mcp.load_target(agent).path, path)

    def test_an_agent_without_a_block_is_not_a_target(self) -> None:
        # antigravity has no MCP support; that is normal, not an error.
        self.assertIsNone(mcp.load_target("antigravity"))

    def test_an_unknown_agent_degrades_instead_of_raising(self) -> None:
        self.assertIsNone(mcp.load_target("no-such-agent"))

    def test_home_env_overrides_the_manifest_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-elsewhere"}):
            self.assertEqual(
                mcp.load_target("codex").path,
                Path("/tmp/codex-elsewhere/config.toml"),
            )

    def test_a_blank_home_env_falls_back_to_the_manifest_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "   "}):
            self.assertEqual(
                mcp.load_target("codex").path, Path.home() / ".codex" / "config.toml"
            )

    def test_a_block_with_no_path_falls_back_to_the_manifest_config_file(self) -> None:
        # hermes declares neither `path` nor `path_in_home`.
        manifest = SimpleNamespace(
            home_dir=Path("/home/x/.hermes"), config_file=Path("/home/x/.hermes/config.yaml")
        )
        block = {
            "format": "yaml",
            "key_path": ["mcp_servers"],
            "server_name": "composio",
            "entry": {"url": "{proxy_url}"},
        }
        self.assertEqual(
            mcp._validated("hermes", manifest, block).path,
            Path("/home/x/.hermes/config.yaml"),
        )

    def test_every_shipped_entry_carries_the_proxy_placeholder_and_no_secret(self) -> None:
        for agent in mcp.agents_with_targets():
            with self.subTest(agent=agent):
                entry = mcp.load_target(agent).entry
                self.assertTrue(mcp._mentions_proxy(entry))
                blob = json.dumps(entry).lower()
                self.assertNotIn("api_key", blob)
                self.assertNotIn("authorization", blob)

    def test_the_shipped_blocks_are_enabled(self) -> None:
        for agent in mcp.agents_with_targets():
            self.assertTrue(mcp.load_target(agent).enabled, agent)

    def test_a_block_can_opt_out_with_enabled_false(self) -> None:
        # The sweep re-adds an entry removed by hand, so this flag is the one way to
        # say "not this agent" without deleting the recipe. Validation still runs.
        block = {
            "format": "json", "path": "/x/c.json", "key_path": ["mcpServers"],
            "server_name": "composio", "entry": {"url": "{proxy_url}"}, "enabled": False,
        }
        opted_out = SimpleNamespace(raw={"mcp": block}, home_dir=Path("/x"),
                                    config_file=Path("/x/c"))
        with patch(
            "services.cowork_agent.registry.agent_registry.get_agent", return_value=opted_out
        ):
            with self.assertNoLogs("services.cowork_agent.connectors.composio.mcp", "WARNING"):
                self.assertIsNone(mcp.load_target("a"))
        # A disabled agent is refused by the same name-based path as an absent block.
        with patch.object(mcp, "load_target", return_value=None):
            result = composio_service.install_into_gateway("user_1__ws__ws-test", "a")
        self.assertFalse(result["ok"])
        self.assertIn("enabled 'mcp' block", result["error"])


class ManifestBlockRejectionTests(unittest.TestCase):
    """A bad block must be reported and skipped, never crash the boot path."""

    manifest = SimpleNamespace(home_dir=Path("/home/x/.a"), config_file=Path("/home/x/.a/c"))

    def _reject(self, block: dict, fragment: str) -> None:
        with self.assertRaises(mcp._Invalid) as caught:
            mcp._validated("a", self.manifest, block)
        self.assertIn(fragment, str(caught.exception))

    def _valid(self) -> dict:
        return {
            "format": "json",
            "key_path": ["mcpServers"],
            "server_name": "composio",
            "entry": {"url": "{proxy_url}"},
        }

    def test_the_baseline_block_is_accepted(self) -> None:
        self.assertEqual(mcp._validated("a", self.manifest, self._valid()).fmt, "json")

    def test_an_unknown_format_is_rejected(self) -> None:
        self._reject({**self._valid(), "format": "ini"}, "'format' must be one of")

    def test_an_empty_key_path_is_rejected(self) -> None:
        self._reject({**self._valid(), "key_path": []}, "'key_path' must be")

    def test_an_entry_without_the_placeholder_is_rejected(self) -> None:
        # Silently installing a server pointing nowhere is worse than not installing.
        self._reject({**self._valid(), "entry": {"url": "http://fixed"}}, "no {proxy_url}")

    def test_path_in_home_without_home_env_is_rejected(self) -> None:
        self._reject({**self._valid(), "path_in_home": "c.toml"}, "requires 'home_env'")

    def test_a_non_boolean_enabled_is_rejected(self) -> None:
        # "false" the string would be truthy and silently keep the agent installed.
        self._reject({**self._valid(), "enabled": "false"}, "'enabled' must be a boolean")

    def test_enabled_defaults_to_true(self) -> None:
        self.assertTrue(mcp._validated("a", self.manifest, self._valid()).enabled)

    def test_a_non_object_block_is_rejected(self) -> None:
        self._reject_raw("not-an-object", "must be a JSON object")

    def _reject_raw(self, block: object, fragment: str) -> None:
        with self.assertRaises(mcp._Invalid) as caught:
            mcp._validated("a", self.manifest, block)
        self.assertIn(fragment, str(caught.exception))

    def test_load_target_swallows_an_invalid_block(self) -> None:
        bad = SimpleNamespace(raw={"mcp": {"format": "ini"}}, home_dir=Path("/x"),
                              config_file=Path("/x/c"))
        with patch(
            "services.cowork_agent.registry.agent_registry.get_agent", return_value=bad
        ):
            with self.assertLogs("services.cowork_agent.connectors.composio.mcp", "WARNING"):
                self.assertIsNone(mcp.load_target("a"))


# ══════════════════════════════════════════════════════════════════════════════
# Writer — TOML (codex)
# ══════════════════════════════════════════════════════════════════════════════


class TomlWriterTests(_TempConfig):
    filename = "config.toml"

    def setUp(self) -> None:
        super().setUp()
        self.target = _target("codex", self.config)

    def _parsed(self) -> dict:
        return tomllib.loads(self._text())

    def _apply(self, proxy: str = PROXY) -> dict:
        return mcp.apply(self.target, proxy)

    # ---- the happy paths ----

    def test_a_missing_config_is_created(self) -> None:
        # Codex runs fine with no config file, so "absent" must not mean
        # "unconnectable" the way it does for ~/.claude.json.
        result = self._apply()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertEqual(
            self._parsed()["mcp_servers"]["composio"], {"url": PROXY, "enabled": True}
        )

    def test_a_created_config_is_private(self) -> None:
        self._apply()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_a_stale_managed_comment_is_refreshed_once(self) -> None:
        # Values equal, wording old: rewritten once so a hand-editor never reads a
        # retired instruction, then reported current on the next pass.
        self._write(
            "[mcp_servers.composio]\n"
            "# Managed by xo-space — rewritten by\n"
            "# POST /api/connectors/composio/refresh-gateway.\n"
            f'url = "{PROXY}"\n'
            "enabled = true\n"
        )
        first = self._apply()
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["changed"])
        self.assertNotIn("refresh-gateway", self._text())
        self.assertFalse(self._apply()["changed"])

    def test_the_managed_comment_names_no_manual_route(self) -> None:
        # The comment is what a user editing config.toml by hand reads: it must say
        # the table is rewritten automatically and how to opt out, not point at an
        # endpoint that no longer exists.
        self._apply()
        text = self._text()
        self.assertIn("Managed by xo-space", text)
        self.assertIn('"enabled": false', text)
        self.assertNotIn("refresh-gateway", text)

    def test_an_existing_config_keeps_its_own_mode(self) -> None:
        self._write('model = "gpt-5"\n')
        self.config.chmod(0o644)
        self._apply()
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
        self.assertTrue(self._apply()["ok"])
        text = self._text()
        self.assertIn("# my codex config", text)
        self.assertIn("network_access = true  # needed for npm", text)
        self.assertEqual(self._parsed()["model"], "gpt-5")

    def test_a_second_install_is_a_no_op(self) -> None:
        self._apply()
        before = self._text()
        result = self._apply()
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["restart_required"])
        self.assertEqual(self._text(), before)

    def test_a_new_token_replaces_the_old_entry_rather_than_adding_one(self) -> None:
        self._apply()
        rotated = PROXY.replace("tok-1", "tok-2")
        self.assertTrue(self._apply(rotated)["changed"])
        self.assertEqual(self._text().count("[mcp_servers.composio]"), 1)
        self.assertEqual(self._parsed()["mcp_servers"]["composio"]["url"], rotated)

    def test_other_mcp_servers_are_left_alone(self) -> None:
        self._write('[mcp_servers.docs]\ncommand = "npx"\nargs = ["-y", "@some/docs-mcp"]\n')
        self.assertTrue(self._apply()["ok"])
        servers = self._parsed()["mcp_servers"]
        self.assertEqual(servers["docs"]["args"], ["-y", "@some/docs-mcp"])
        self.assertIn("composio", servers)

    def test_a_legacy_table_is_removed_so_tools_are_not_listed_twice(self) -> None:
        self._write(f'[mcp_servers.cowork]\nurl = "{PROXY}"\nenabled = true\n')
        self.assertTrue(self._apply()["ok"])
        servers = self._parsed()["mcp_servers"]
        self.assertNotIn("cowork", servers)
        self.assertIn("composio", servers)

    def test_a_quoted_table_header_is_still_recognised(self) -> None:
        self._write('[mcp_servers."composio"]\nurl = "http://stale/"\n')
        self.assertTrue(self._apply()["ok"])
        self.assertNotIn("http://stale/", self._text())
        self.assertEqual(self._parsed()["mcp_servers"]["composio"]["url"], PROXY)

    def test_no_credential_is_written_into_the_config(self) -> None:
        # The whole point of the loopback proxy: the Composio key stays server
        # side, so nothing secret may reach an agent's config file.
        with patch.dict(os.environ, {"COMPOSIO_API_KEY": "secret-key"}):
            self._apply()
        text = self._text()
        self.assertNotIn("secret-key", text)
        self.assertNotIn("bearer", text.lower())

    # ---- the refusals ----

    def test_no_proxy_url_is_refused(self) -> None:
        self.assertFalse(self._apply("")["ok"])
        self.assertFalse(self.config.exists())

    def test_an_unparseable_config_is_never_rewritten(self) -> None:
        broken = "model = = = \n"
        self._write(broken)
        result = self._apply()
        self.assertFalse(result["ok"])
        self.assertIn("not valid TOML", result["error"])
        self.assertEqual(self._text(), broken)

    def test_an_inline_mcp_servers_table_is_refused_not_corrupted(self) -> None:
        # Appending [mcp_servers.composio] under an inline `mcp_servers = {...}`
        # is a duplicate-key error; the guard catches it before the write.
        inline = 'mcp_servers = { docs = { command = "npx" } }\n'
        self._write(inline)
        result = self._apply()
        self.assertFalse(result["ok"])
        self.assertIn("inline table", result["error"])
        self.assertEqual(self._text(), inline)

    def test_a_splice_that_would_move_other_settings_aborts(self) -> None:
        # _strip_tables is line-based, so this is the backstop: if a chop ever
        # eats a neighbouring key, the before/after comparison refuses the write.
        self._write('model = "gpt-5"\n')
        with patch.object(mcp, "_strip_tables", side_effect=lambda lines, patterns: []):
            result = self._apply()
        self.assertFalse(result["ok"])
        self.assertIn("outside mcp_servers", result["error"])
        self.assertEqual(self._text(), 'model = "gpt-5"\n')

    def test_a_url_carrying_a_quote_cannot_break_out_of_the_string(self) -> None:
        hostile = 'http://127.0.0.1/u/"\nmodel = "pwned"\n'
        self.assertTrue(self._apply(hostile)["ok"])
        parsed = self._parsed()
        self.assertEqual(parsed["mcp_servers"]["composio"]["url"], hostile)
        self.assertNotIn("model", parsed)


# ══════════════════════════════════════════════════════════════════════════════
# Writer — JSON (claude_code) and nested JSON + prune (openclaw)
# ══════════════════════════════════════════════════════════════════════════════


class JsonWriterTests(_TempConfig):
    filename = "claude.json"

    def setUp(self) -> None:
        super().setUp()
        self.target = _target("claude_code", self.config)

    def _parsed(self) -> dict:
        return json.loads(self._text())

    def test_the_cli_entry_shape_is_written(self) -> None:
        self._write("{}")
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        self.assertEqual(
            self._parsed()["mcpServers"]["composio"], {"type": "http", "url": PROXY}
        )

    def test_a_missing_config_is_refused_not_created(self) -> None:
        # ~/.claude.json is owned by the claude CLI; its absence means claude
        # never ran, and creating one would be inventing state.
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])
        self.assertFalse(self.config.exists())

    def test_unrelated_settings_survive(self) -> None:
        self._write(json.dumps({"numStartups": 41, "mcpServers": {"docs": {"url": "u"}}}))
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        parsed = self._parsed()
        self.assertEqual(parsed["numStartups"], 41)
        self.assertEqual(parsed["mcpServers"]["docs"], {"url": "u"})

    def test_a_legacy_key_is_purged(self) -> None:
        self._write(json.dumps({"mcpServers": {"cowork": {"url": "old"}}}))
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        servers = self._parsed()["mcpServers"]
        self.assertNotIn("cowork", servers)
        self.assertIn("composio", servers)

    def test_a_second_install_is_a_no_op(self) -> None:
        self._write("{}")
        mcp.apply(self.target, PROXY)
        before = self._text()
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["changed"])
        self.assertEqual(self._text(), before)

    def test_an_unparseable_config_is_never_rewritten(self) -> None:
        broken = "{not json"
        self._write(broken)
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", result["error"])
        self.assertEqual(self._text(), broken)

    def test_a_non_object_document_is_refused(self) -> None:
        self._write("[1, 2, 3]")
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not an object", result["error"])
        self.assertEqual(self._text(), "[1, 2, 3]")

    def test_an_existing_config_keeps_its_own_mode(self) -> None:
        self._write("{}")
        self.config.chmod(0o644)
        mcp.apply(self.target, PROXY)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)

    def test_non_ascii_prose_is_not_rewritten_as_escapes(self) -> None:
        # ~/.claude.json is ~44 KB of the CLI's own prose. Escaping every em dash
        # would churn thousands of untouched lines on each token refresh and bury
        # the one line that actually changed.
        self._write(json.dumps({"note": "a — b · c"}, ensure_ascii=False))
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        text = self._text()
        self.assertIn("a — b · c", text)
        self.assertNotIn("\\u2014", text)
        self.assertEqual(json.loads(text)["note"], "a — b · c")


class NestedJsonAndPruneTests(_TempConfig):
    filename = "openclaw.json"

    def setUp(self) -> None:
        super().setUp()
        self.target = _target("openclaw", self.config)

    def _parsed(self) -> dict:
        return json.loads(self._text())

    def test_a_nested_key_path_is_created_and_written(self) -> None:
        self._write(json.dumps({"other": 1}))
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        parsed = self._parsed()
        self.assertEqual(
            parsed["mcp"]["servers"]["composio"],
            {"url": PROXY, "transport": "streamable-http", "enabled": True},
        )
        self.assertEqual(parsed["other"], 1)

    def test_the_stale_plugin_entry_is_pruned(self) -> None:
        self._write(
            json.dumps(
                {"plugins": {"entries": {"composio": {"enabled": True}, "slack": {"x": 1}}}}
            )
        )
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        entries = self._parsed()["plugins"]["entries"]
        self.assertNotIn("composio", entries)
        self.assertEqual(entries["slack"], {"x": 1})

    def test_a_stale_plugin_entry_alone_still_counts_as_a_change(self) -> None:
        # The server entry is already current, but the prune target is not — the
        # install must not report "already current" and leave it behind.
        self._write("{}")
        mcp.apply(self.target, PROXY)
        data = self._parsed()
        data["plugins"] = {"entries": {"composio": {"enabled": True}}}
        self._write(json.dumps(data))
        result = mcp.apply(self.target, PROXY)
        self.assertTrue(result["changed"])
        self.assertNotIn("composio", self._parsed()["plugins"]["entries"])

    def test_a_scalar_blocking_the_key_path_is_refused(self) -> None:
        self._write(json.dumps({"mcp": "a string, not a table"}))
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not a table", result["error"])


# ══════════════════════════════════════════════════════════════════════════════
# Writer — YAML (hermes)
# ══════════════════════════════════════════════════════════════════════════════


class YamlWriterTests(_TempConfig):
    filename = "config.yaml"

    def setUp(self) -> None:
        super().setUp()
        self.target = _target("hermes", self.config)

    def _parsed(self) -> dict:
        import yaml

        return yaml.safe_load(self._text())

    def test_the_gateway_entry_shape_is_written(self) -> None:
        self._write("model:\n  provider: anthropic\n")
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        parsed = self._parsed()
        self.assertEqual(
            parsed["mcp_servers"]["composio"],
            {"url": PROXY, "transport": "streamable-http", "enabled": True},
        )
        self.assertEqual(parsed["model"]["provider"], "anthropic")

    def test_a_legacy_key_is_purged(self) -> None:
        self._write("mcp_servers:\n  cowork:\n    url: old\n")
        self.assertTrue(mcp.apply(self.target, PROXY)["ok"])
        self.assertNotIn("cowork", self._parsed()["mcp_servers"])

    def test_a_second_install_is_a_no_op(self) -> None:
        self._write("{}")
        mcp.apply(self.target, PROXY)
        before = self._text()
        self.assertFalse(mcp.apply(self.target, PROXY)["changed"])
        self.assertEqual(self._text(), before)

    def test_an_unparseable_config_is_never_rewritten(self) -> None:
        broken = "model: [unclosed\n"
        self._write(broken)
        result = mcp.apply(self.target, PROXY)
        self.assertFalse(result["ok"])
        self.assertIn("not valid YAML", result["error"])
        self.assertEqual(self._text(), broken)


# ══════════════════════════════════════════════════════════════════════════════
# The service seams
# ══════════════════════════════════════════════════════════════════════════════


class GatewayWiringTests(unittest.TestCase):
    """The manifest block is what makes an agent a supported gateway target."""

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

    def test_every_agent_with_a_block_is_an_install_target(self) -> None:
        self.assertEqual(
            composio_service.gateway_install_agents(),
            ["claude_code", "codex", "hermes", "openclaw"],
        )

    def test_install_into_gateway_passes_a_scoped_proxy_url(self) -> None:
        with patch.object(mcp, "apply", return_value={"ok": True}) as applied:
            result = composio_service.install_into_gateway("user_1__ws__ws-test", "codex")
        self.assertTrue(result["ok"])
        applied.assert_called_once()
        target, proxy_url = applied.call_args[0]
        self.assertEqual(target.agent, "codex")
        self.assertIn("/mcp/composio-proxy/u/", proxy_url)

    def test_an_agent_without_a_block_is_refused_by_name(self) -> None:
        result = composio_service.install_into_gateway("user_1__ws__ws-test", "antigravity")
        self.assertFalse(result["ok"])
        self.assertIn("antigravity", result["error"])
        self.assertIn("manifest.json", result["error"])

    def test_install_into_gateway_accepts_a_precomputed_proxy_url(self) -> None:
        # The sweep mints once (one swarm round trip) and hands the URL to every
        # agent; an install given the URL must not mint again.
        with patch.object(mcp, "apply", return_value={"ok": True}) as applied, \
                patch.object(composio_service, "_composio_proxy_url") as minted:
            result = composio_service.install_into_gateway(
                "user_1__ws__ws-test", "codex", proxy_url=PROXY,
            )
        self.assertTrue(result["ok"])
        minted.assert_not_called()
        self.assertEqual(applied.call_args[0][1], PROXY)


if __name__ == "__main__":
    unittest.main()
