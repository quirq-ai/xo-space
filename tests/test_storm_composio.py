"""Composio in a plain chat on storm (owner §0.108; ch 10 §4.1): the stored
spec names it only; when the process starts it becomes exactly the entry
today's per-turn ``--mcp-config`` file held, behind the same gate (a Composio
key configured), or is left out."""

from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import command
from services.cowork_agent.connectors.composio import byo_key
from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.storm import composio, redact
from tests.storm_engine_kit import Kit

PROXY = "http://127.0.0.1:5399/mcp/composio-proxy/u/FAKE-TOKEN"
COMMS = c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1:5399/mcp/space-comms",
                    bearer_env="XO_COMMS_TOKEN", auto_approve=True)
#: What today's claude turn wrote to /tmp/xo-cowork/<session>/mcp.json
#: (``claude_code/mcp_config.py`` at 6febccd, probed with the same fake URL).
TODAYS_FILE = {"mcpServers": {"composio": {"type": "http", "url": PROXY}}}


def spec(*servers: c.McpServer) -> c.SessionSpec:
    return c.SessionSpec(cwd="/w", mcp_servers=servers)


class FilledTests(unittest.TestCase):
    def test_a_spec_without_the_marker_is_left_as_it_is(self) -> None:
        plain = spec(COMMS)
        with mock.patch.object(byo_key, "configured", side_effect=AssertionError("not asked")):
            self.assertIs(composio.filled(plain), plain)

    def test_with_a_key_the_marker_becomes_todays_entry(self) -> None:
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "_composio_proxy_url", return_value=PROXY):
            opened = composio.filled(spec(composio.marker()))
        self.assertEqual(command.mcp_config(opened), TODAYS_FILE)

    def test_with_comms_beside_it_only_the_marker_changes(self) -> None:
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "_composio_proxy_url", return_value=PROXY):
            opened = composio.filled(spec(composio.marker(), COMMS))
        self.assertEqual(opened.mcp_servers, (dataclasses.replace(composio.marker(), url=PROXY), COMMS))

    def test_the_proxy_token_is_redacted_wherever_it_is_echoed(self) -> None:
        token = "made-up-composio-proxy-token-0042"
        url = f"http://127.0.0.1:5399/mcp/composio-proxy/u/{token}"
        self.addCleanup(redact.drop_token, token)
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "_composio_proxy_url", return_value=url):
            composio.filled(spec(composio.marker()))
        self.assertEqual(redact.redact(f'mcp_status: {{"url": "{url}"}}'),
                         'mcp_status: {"url": "http://127.0.0.1:5399/mcp/composio-proxy/u/[redacted]"}')

    def test_without_a_key_composio_is_left_out_and_nothing_is_minted(self) -> None:
        with mock.patch.object(byo_key, "configured", return_value=False), \
                mock.patch.object(composio_service, "_composio_proxy_url",
                                  side_effect=AssertionError("no token without a key")):
            self.assertEqual(composio.filled(spec(composio.marker(), COMMS)).mcp_servers, (COMMS,))

    def test_a_proxy_url_that_cant_be_made_leaves_composio_out(self) -> None:
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "_composio_proxy_url", side_effect=OSError("store unwritable")), \
                self.assertLogs("services.cowork_agent.storm.composio", "WARNING") as logged:
            self.assertEqual(composio.filled(spec(composio.marker())).mcp_servers, ())
        self.assertIn("OSError", logged.output[0])


class EngineStartTests(unittest.IsolatedAsyncioTestCase):
    """Every session's CLI reads its own MCP config, where the reconcile
    sweep put the proxy URL: the token is redacted from the engine's start,
    not from the first plain chat's fill."""

    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.kit.scenario([])

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    async def stored(self, text: str) -> str:
        seq = await self.kit.engine.write(lambda w: w.append("step.notice", {"message": text}))
        return self.kit.rows("SELECT json_extract(data, '$.message') FROM events WHERE seq = ?", (seq,))[0][0]

    async def test_with_a_key_the_proxy_token_is_redacted_from_the_start(self) -> None:
        token = "made-up-composio-proxy-token-0043"
        self.addCleanup(redact.drop_token, token)
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "proxy_token", return_value=token):
            await self.kit.boot()
        self.assertEqual(await self.stored(f'"url": ".../composio-proxy/u/{token}"'),
                         '"url": ".../composio-proxy/u/[redacted]"')

    async def test_without_a_key_no_token_is_read_or_minted(self) -> None:
        with mock.patch.object(composio_service, "proxy_token") as proxy_token:
            await self.kit.boot()
        proxy_token.assert_not_called()             # it mints one when there is none

    async def test_a_token_that_cant_be_read_never_stops_the_start(self) -> None:
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "proxy_token", side_effect=OSError("store unreadable")), \
                self.assertLogs("services.cowork_agent.storm.composio", "WARNING") as logged:
            await self.kit.boot()
        self.assertIn("OSError", logged.output[0])


if __name__ == "__main__":
    unittest.main()
