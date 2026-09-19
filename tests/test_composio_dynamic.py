"""Phase 1 of dynamic connectors: the agent-driven session (manage_connections,
no fixed toolkit allowlist), the connect policy, and space_scope reconciliation.

Hermetic: reuses ``test_composio_byo._KeyBase`` for the owner-only key fixture, and
redirects the space_scope store into a temp dir where a test needs it.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.cowork_agent.connectors.composio import client as byo_client, byo_key
from services.cowork_agent.connectors.composio import service
from tests.test_composio_byo import _KeyBase


class DynamicFlagTests(unittest.TestCase):
    def test_flag_defaults_off_and_reads_env_at_call_time(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSIO_DYNAMIC_CONNECTORS", None)
            self.assertFalse(service.dynamic_connectors_enabled())
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}):
            self.assertTrue(service.dynamic_connectors_enabled())

    def test_manage_connections_config_uses_sdk_shape_and_callback(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_CALLBACK_URL": "https://x/cb"}):
            cfg = service.manage_connections_config()
        # SDK create shape (enable / wait_for_connections / callback_url), not API names.
        self.assertEqual(cfg, {"enable": True, "wait_for_connections": True,
                               "callback_url": "https://x/cb"})
        self.assertNotIn("enable_wait_for_connections", cfg)


class _ScopeTemp(_KeyBase):
    """Key configured + space_scope redirected to a temp store."""

    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")
        from services.cowork_agent.connectors.composio import space_scope
        self._sp = tempfile.TemporaryDirectory()
        self.addCleanup(self._sp.cleanup)
        for p in (
            patch.object(space_scope, "_store_path",
                         return_value=Path(self._sp.name) / "scope.json"),
            patch.object(space_scope, "_LEGACY_SCOPE_PATHS", ()),
        ):
            p.start()
            self.addCleanup(p.stop)


class ClientManageConnectionsTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_create_forwards_manage_connections_and_omits_toolkits(self) -> None:
        session = SimpleNamespace(
            session_id="trs_1",
            mcp=SimpleNamespace(url="https://mcp/s", headers={"x-api-key": "sk_live"}))
        sdk = MagicMock()
        sdk.create.return_value = session
        cfg = {"tools": {}, "manage_connections": {"enable": True}}
        with patch.object(byo_client, "_sdk", return_value=sdk):
            byo_client.create_session(cfg)
        kw = sdk.create.call_args.kwargs
        self.assertEqual(kw["manage_connections"], {"enable": True})
        self.assertNotIn("toolkits", kw)

    def test_update_forwards_manage_connections(self) -> None:
        sess = MagicMock()
        sess.mcp = SimpleNamespace(url="https://mcp/s", headers={})
        sdk = MagicMock()
        sdk.use.return_value = sess
        with patch.object(byo_client, "_sdk", return_value=sdk):
            byo_client.update_session("trs_1", {"manage_connections": {"enable": True}})
        self.assertEqual(sess.update.call_args.kwargs["manage_connections"], {"enable": True})
        self.assertNotIn("toolkits", sess.update.call_args.kwargs)


class SessionConfigTests(_ScopeTemp):
    def test_dynamic_session_has_manage_connections_and_no_allowlist(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}), \
                patch.object(service, "prune_scope_to_live_accounts", return_value=False):
            cfg = service._session_config("user_x")
        self.assertIn("manage_connections", cfg)
        self.assertNotIn("toolkits", cfg)

    def test_dynamic_mode_does_not_raise_when_nothing_enabled(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}), \
                patch.object(service, "prune_scope_to_live_accounts", return_value=False):
            service._session_config("user_x")   # must not raise

    def test_curated_mode_unchanged(self) -> None:
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(service, "prune_scope_to_live_accounts", return_value=False):
            os.environ.pop("COMPOSIO_DYNAMIC_CONNECTORS", None)
            with self.assertRaises(service.NoToolkitsEnabled):
                service._session_config("user_x")


class ToolkitsDisplayTests(unittest.IsolatedAsyncioTestCase, _ScopeTemp):
    async def test_dynamic_mode_shows_a_connected_toolkit_as_enabled_here(self) -> None:
        import json
        from routers.cowork_agent.connectors import composio as router
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE"}]
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}), \
                patch.object(service, "kick_gateway_sweep"), \
                patch.object(service, "list_connections", return_value=rows):
            resp = await router.list_toolkits(user_id="user_x")
        gmail = next(t for t in json.loads(resp.body)["toolkits"] if t["id"] == "gmail")
        self.assertTrue(gmail["workspace_enabled"])   # derived from the live connection

    async def test_curated_mode_needs_an_explicit_scope_opt_in(self) -> None:
        import json
        from routers.cowork_agent.connectors import composio as router
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE"}]
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(service, "kick_gateway_sweep"), \
                patch.object(service, "list_connections", return_value=rows):
            os.environ.pop("COMPOSIO_DYNAMIC_CONNECTORS", None)
            resp = await router.list_toolkits(user_id="user_x")
        gmail = next(t for t in json.loads(resp.body)["toolkits"] if t["id"] == "gmail")
        self.assertFalse(gmail["workspace_enabled"])   # connected on account, off here


class PolicyTests(_ScopeTemp):
    def test_allowlist_policy_bounds_dynamic_session(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1",
                                     "COMPOSIO_CONNECT_ALLOW": "gmail,slack"}), \
                patch.object(service, "prune_scope_to_live_accounts", return_value=False):
            cfg = service._session_config("user_x")
        self.assertEqual(cfg["toolkits"], {"enable": ["gmail", "slack"]})
        self.assertIn("manage_connections", cfg)

    def test_no_policy_means_no_allowlist(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}, clear=False), \
                patch.object(service, "prune_scope_to_live_accounts", return_value=False):
            os.environ.pop("COMPOSIO_CONNECT_ALLOW", None)
            cfg = service._session_config("user_x")
        self.assertNotIn("toolkits", cfg)


class ProxyMetaToolsTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    """The MCP proxy needs no dynamic-mode logic: it forwards whatever entry
    build_mcp_server_entry returns, so a dynamic session's meta-tools reach the
    agent unchanged."""

    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")
        import tempfile
        from pathlib import Path
        self._sp = tempfile.TemporaryDirectory()
        self.addCleanup(self._sp.cleanup)
        p = patch.object(service, "_SESSIONS_PATH", Path(self._sp.name) / "sessions.json")
        p.start(); self.addCleanup(p.stop)
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()

    async def test_proxy_builds_the_entry_for_the_local_user(self) -> None:
        from routers.cowork_agent.connectors import composio_mcp_proxy as mcp_proxy
        from tests.test_composio import _make_request
        token = service.proxy_token()
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1"}), \
                patch.object(service, "build_mcp_server_entry",
                             return_value={"type": "http", "url": "https://mcp/s"}) as build:
            await mcp_proxy._proxy(_make_request(), "POST", token)
        build.assert_called_once_with(byo_key.user_id())


class ArbitraryToolkitDegradationTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    """Phase 3 contract: a toolkit outside the curated set connects for the agent but
    degrades cleanly — no Inbox collectors, no read/write tags, no per-action prefs.
    These assert existing behaviour so it cannot silently regress at 1500-toolkit scale.
    """

    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_unknown_toolkit_has_no_collectors(self) -> None:
        from services.connections import collectors
        self.assertEqual(collectors.catalog("nosuchtoolkit"), [])
        self.assertEqual(collectors.default_ids("nosuchtoolkit"), [])
        self.assertIsNone(collectors.identity_spec("nosuchtoolkit"))

    def test_unknown_toolkit_is_untagged(self) -> None:
        from services.cowork_agent.connectors.composio import categories
        self.assertIsNone(categories.classify("nosuchtoolkit", "NOSUCH_ACTION"))
        self.assertNotIn("nosuchtoolkit", categories.classified_toolkits())

    async def test_action_prefs_put_404_for_unknown_toolkit(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as router
        with self.assertRaises(HTTPException) as raised:
            await router.put_toolkit_prefs(
                "nosuchtoolkit", router.PrefsBody(actions={}), user_id="u")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
