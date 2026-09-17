"""Bring-your-own-key Composio: local key store and SDK client.

Hermetic: the key file is redirected into a temp dir, the lock root points at the
temp dir, and no ambient COMPOSIO_BYO_API_KEY from the developer's shell leaks in.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.cowork_agent.connectors.composio import byo_key
from services.cowork_agent.connectors.composio import client as byo_client


def _sdk_stub(**resources) -> SimpleNamespace:
    return SimpleNamespace(**resources)


class _KeyBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.key_path = tmp / "composio" / "api_key.json"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(tmp / "quirq")}, clear=False)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop(byo_key.ENV_VAR, None)
        self.addCleanup(lambda: os.environ.pop(byo_key.ENV_VAR, None))
        p = patch.object(byo_key, "_KEY_PATH", self.key_path)
        p.start(); self.addCleanup(p.stop)
        # A key change must not be masked by the memoised SDK client.
        byo_client._sdk_client = None
        byo_client._sdk_key = ""
        self.addCleanup(lambda: setattr(byo_client, "_sdk_client", None))


class SourceTests(_KeyBase):
    def test_unset_by_default(self) -> None:
        self.assertEqual(byo_key.api_key(), "")
        self.assertIsNone(byo_key.source())
        self.assertFalse(byo_key.configured())

    def test_file_key_is_read(self) -> None:
        byo_key.save("sk_file")
        self.assertEqual(byo_key.api_key(), "sk_file")
        self.assertEqual(byo_key.source(), "file")

    def test_env_beats_file(self) -> None:
        byo_key.save("sk_file")
        with patch.dict(os.environ, {byo_key.ENV_VAR: "sk_env"}):
            self.assertEqual(byo_key.api_key(), "sk_env")
            self.assertEqual(byo_key.source(), "env")

    def test_require_raises_when_unset(self) -> None:
        with self.assertRaises(byo_key.ComposioKeyRequired):
            byo_key.require()

    def test_user_id_defaults_then_reads_space_env(self) -> None:
        with patch.dict(os.environ, {"XO_SPACE_ID": ""}):
            self.assertEqual(byo_key.user_id(), byo_key.DEFAULT_USER_ID)
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            self.assertEqual(byo_key.user_id(), "space-42")


class FileTests(_KeyBase):
    def test_saved_file_is_0600_and_has_a_fingerprint(self) -> None:
        byo_key.save("sk_secret")
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        data = json.loads(self.key_path.read_text(encoding="utf-8"))
        self.assertEqual(data["api_key"], "sk_secret")
        self.assertEqual(data["key_fingerprint"], byo_key.fingerprint("sk_secret"))
        self.assertEqual(data["auth_configs"], {})

    def test_changing_the_key_resets_the_auth_config_cache(self) -> None:
        byo_key.save("sk_one")
        byo_key.save_auth_config("gmail", "ac_1")
        self.assertEqual(byo_key.load_auth_configs(), {"gmail": "ac_1"})
        byo_key.save("sk_two")
        self.assertEqual(byo_key.load_auth_configs(), {})

    def test_auth_config_cache_ignored_when_fingerprint_mismatches_env(self) -> None:
        byo_key.save("sk_file")
        byo_key.save_auth_config("gmail", "ac_file")
        with patch.dict(os.environ, {byo_key.ENV_VAR: "sk_env"}):
            self.assertEqual(byo_key.load_auth_configs(), {})

    def test_clear_removes_the_file(self) -> None:
        byo_key.save("sk_x")
        byo_key.clear()
        self.assertFalse(self.key_path.exists())
        self.assertEqual(byo_key.api_key(), "")


class ClientTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_no_key_raises_before_touching_the_sdk(self) -> None:
        byo_key.clear()
        with patch.object(byo_client, "_sdk") as sdk:
            with self.assertRaises(byo_client.ComposioKeyRequired):
                byo_client.list_connections()
        sdk.assert_not_called()

    def test_list_connections_scopes_to_this_user_and_shapes_rows(self) -> None:
        row = SimpleNamespace(
            id="ca_1", toolkit=SimpleNamespace(slug="gmail"), status="ACTIVE",
            auth_scheme="OAUTH2", alias=None, created_at="2026-01-01T00:00:00Z",
            is_disabled=False,
        )
        ca = MagicMock()
        ca.list.return_value = SimpleNamespace(items=[row])
        with patch.object(byo_client, "_sdk", return_value=_sdk_stub(connected_accounts=ca)):
            out = byo_client.list_connections(statuses=["ACTIVE"])
        self.assertEqual(ca.list.call_args.kwargs["user_ids"], [byo_key.user_id()])
        self.assertEqual(out, [{
            "toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE",
            "scheme": "OAUTH2", "alias": None, "created_at": "2026-01-01T00:00:00Z",
            "is_disabled": False,
        }])

    def test_a_rejected_key_is_authoritative(self) -> None:
        import composio_client
        ca = MagicMock()
        ca.list.side_effect = composio_client.AuthenticationError.__new__(
            composio_client.AuthenticationError
        )
        with patch.object(byo_client, "_sdk", return_value=_sdk_stub(connected_accounts=ca)):
            with self.assertRaises(byo_client.ComposioError) as raised:
                byo_client.list_connections()
        self.assertTrue(raised.exception.authoritative)

    def test_disconnect_not_owned_is_not_found(self) -> None:
        ca = MagicMock()
        ca.list.return_value = SimpleNamespace(items=[])
        with patch.object(byo_client, "_sdk", return_value=_sdk_stub(connected_accounts=ca)):
            with self.assertRaises(byo_client.ComposioNotFound):
                byo_client.disconnect("ca_other")
        ca.delete.assert_not_called()

    def test_auth_config_for_reuses_a_cached_enabled_config(self) -> None:
        byo_key.save_auth_config("gmail", "ac_cached")
        with patch.object(byo_client, "_sdk") as sdk:
            self.assertEqual(byo_client.auth_config_for("gmail"), "ac_cached")
        sdk.assert_not_called()

    def test_auth_config_for_lists_then_creates_managed(self) -> None:
        acfg = MagicMock()
        acfg.list.return_value = SimpleNamespace(items=[])
        acfg.create.return_value = SimpleNamespace(id="ac_new")
        with patch.object(byo_client, "_sdk", return_value=_sdk_stub(auth_configs=acfg)):
            self.assertEqual(byo_client.auth_config_for("gmail"), "ac_new")
        acfg.create.assert_called_once()
        self.assertEqual(byo_key.load_auth_configs()["gmail"], "ac_new")

    def test_create_session_addresses_this_user_and_returns_url_and_headers(self) -> None:
        session = SimpleNamespace(
            session_id="trs_1",
            mcp=SimpleNamespace(url="https://mcp.example/s",
                                headers={"x-api-key": "sk_live"}),
        )
        sdk = MagicMock()
        sdk.create.return_value = session
        with patch.object(byo_client, "_sdk", return_value=sdk):
            out = byo_client.create_session({"toolkits": {"enable": ["gmail"]}, "tools": {}})
        self.assertEqual(sdk.create.call_args.kwargs["user_id"], byo_key.user_id())
        self.assertTrue(sdk.create.call_args.kwargs["mcp"])
        self.assertEqual(out["session_id"], "trs_1")
        self.assertEqual(out["mcp"], {"url": "https://mcp.example/s",
                                      "headers": {"x-api-key": "sk_live"}})


class ServiceBackendTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        from services.cowork_agent.connectors.composio import service
        self.service = service
        byo_key.save("sk_live")
        self._sp = tempfile.TemporaryDirectory(); self.addCleanup(self._sp.cleanup)
        self.sessions_path = Path(self._sp.name) / "sessions.json"
        for p in (patch.object(service, "_SESSIONS_PATH", self.sessions_path),
                  patch.object(service, "_LEGACY_SESSIONS_PATHS", ()),
                  patch.dict(os.environ, {"XO_SPACE_ID": "space-42"})):
            p.start(); self.addCleanup(p.stop)
        self._reset()
        self.addCleanup(self._reset)

    def _reset(self) -> None:
        s = self.service
        s._SESSIONS_LOADED = False
        s._PROXY_TOKENS.clear()
        s._SESSION_ID = None
        s._STORE_ACCOUNT = None
        s._ORPHANED_SESSION_IDS.clear()

    def test_store_records_the_backend_stamp_and_the_local_user(self) -> None:
        self.service.proxy_token()
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 5)
        self.assertEqual(data["backend"], "local:" + byo_key.fingerprint("sk_live"))
        self.assertEqual(data["account_id"], "space-42")

    def test_a_v4_store_is_discarded_but_keeps_proxy_tokens(self) -> None:
        self.sessions_path.write_text(json.dumps({
            "version": 4, "space_id": "space-42", "account_id": "old",
            "session": "trs_old", "proxy_tokens": ["keep-me"],
        }), encoding="utf-8")
        self._reset()
        # The token survives; the org-project session does not.
        self.assertEqual(self.service.proxy_token(), "keep-me")
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 5)
        self.assertIsNone(data["session"])
        self.assertEqual(data["proxy_tokens"], ["keep-me"])
        self.assertIn("trs_old", self.service._ORPHANED_SESSION_IDS)

    def test_a_session_from_another_key_is_dropped_tokens_kept(self) -> None:
        self.sessions_path.write_text(json.dumps({
            "version": 5, "backend": "local:deadbeefdeadbeef", "account_id": "space-42",
            "session": "trs_otherkey", "proxy_tokens": ["tok-a"],
        }), encoding="utf-8")
        self._reset()
        self.service._ensure_sessions_loaded()
        self.assertIn("tok-a", self.service._PROXY_TOKENS)
        self.assertIsNone(self.service._SESSION_ID)
        self.assertIn("trs_otherkey", self.service._ORPHANED_SESSION_IDS)

    def test_no_key_makes_get_session_raise_key_required(self) -> None:
        byo_key.clear()
        with self.assertRaises(byo_client.ComposioKeyRequired):
            self.service.get_session("space-42")


def _req(headers=None):
    from starlette.requests import Request
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/", "raw_path": b"/", "query_string": b"", "server": ("127.0.0.1", 5002),
             "client": ("127.0.0.1", 1),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    from starlette.requests import Request as _R
    return _R(scope, receive)


class IdentityGateTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    async def test_no_session_header_needed(self) -> None:
        from services.cowork_agent.connectors.composio import identity as identity_mod
        byo_key.save("sk_live")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            self.assertEqual(await identity_mod.get_composio_user(_req()), "space-42")

    async def test_cross_site_origin_is_403(self) -> None:
        from fastapi import HTTPException
        from services.cowork_agent.connectors.composio import identity as identity_mod
        byo_key.save("sk_live")
        req = _req({"origin": "https://evil.example", "sec-fetch-site": "cross-site"})
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(req)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_resolve_user_none_without_a_key(self) -> None:
        from services.cowork_agent.connectors.composio import identity as identity_mod
        self.assertIsNone(await identity_mod.resolve_user(_req()))
        byo_key.save("sk_live")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            self.assertEqual(await identity_mod.resolve_user(_req()), "space-42")


class PollerUserTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    async def test_no_key_resolves_to_none(self) -> None:
        from services.connections import poller
        self.assertIsNone(await poller.resolve_user_id())

    async def test_key_resolves_to_local_user(self) -> None:
        from services.connections import poller
        byo_key.save("sk_live")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            self.assertEqual(await poller.resolve_user_id(), "space-42")


if __name__ == "__main__":
    unittest.main()
