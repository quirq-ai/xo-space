"""Bring-your-own-key Composio: local key store, XO account identity, and SDK client.

Two gates open Composio and both are exercised here: the key (which Composio project)
and the XO account id (whose connections inside it). Composio is addressed by the
account, never by the Space, which is what lets one sign-in serve every Space.

Hermetic: the key file *and* the identity cache are redirected into a temp dir, the lock
root points at the temp dir, and no ambient COMPOSIO_API_KEY or XO_ACCOUNT_ID from
the developer's shell leaks in. Nothing here may reach xo-swarm-api: the account id is
seeded with ``sign_in()``, which is what a resolved lookup would have cached.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.cowork_agent.connectors.composio import account_identity, byo_key
from services.cowork_agent.connectors.composio import client as byo_client

#: A stand-in for the id xo-swarm-api answers ``GET /get-user-id`` with.
ACCOUNT = "user_3TESTACCOUNT"


def _sdk_stub(**resources) -> SimpleNamespace:
    return SimpleNamespace(**resources)


class _KeyBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.key_path = tmp / "composio" / "api_key.json"
        self.identity_path = tmp / "composio" / "identity.json"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(tmp / "quirq")}, clear=False)
        env.start(); self.addCleanup(env.stop)
        for var in (byo_key.ENV_VAR, account_identity.ENV_VAR):
            os.environ.pop(var, None)
            self.addCleanup(lambda v=var: os.environ.pop(v, None))
        for p in (patch.object(byo_key, "_KEY_PATH", self.key_path),
                  patch.object(account_identity, "_PATH", self.identity_path)):
            p.start(); self.addCleanup(p.stop)
        # Start signed out, whatever a previous test left in the module cache.
        self._forget_account()
        self.addCleanup(self._forget_account)
        # A key change must not be masked by the memoised SDK client.
        byo_client._sdk_client = None
        byo_client._sdk_key = ""
        self.addCleanup(lambda: setattr(byo_client, "_sdk_client", None))

    @staticmethod
    def _forget_account() -> None:
        """Signed out for real: the module cache *and* the redirected disk cache."""
        account_identity.forget()

    def sign_in(self, account: str = ACCOUNT) -> str:
        """Seed the account id the way a resolved lookup (or a consumed token) would."""
        account_identity.remember(account)
        return account


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

    def test_user_id_is_the_xo_account_never_the_space(self) -> None:
        # Fails closed: no account id, no Composio user id — and XO_SPACE_ID is never
        # a substitute, or a Space could file connections where no other Space sees them.
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            with self.assertRaises(account_identity.XOAccountRequired):
                byo_key.user_id()
            self.sign_in()
            self.assertEqual(byo_key.user_id(), ACCOUNT)


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
        self.sign_in()

    def test_an_unknown_account_raises_before_touching_the_sdk(self) -> None:
        self._forget_account()
        with patch.object(byo_client, "_sdk") as sdk:
            # Resolved outside the try, so it surfaces as itself rather than as a
            # ComposioError blamed on Composio.
            with self.assertRaises(account_identity.XOAccountRequired):
                byo_client.list_connections()
        sdk.assert_not_called()

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


class ServiceBackendTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    def setUp(self) -> None:
        super().setUp()
        from services.cowork_agent.connectors.composio import service
        self.service = service
        byo_key.save("sk_live")
        self._sp = tempfile.TemporaryDirectory(); self.addCleanup(self._sp.cleanup)
        self.sessions_path = Path(self._sp.name) / "sessions.json"
        self.sign_in()
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

    def test_store_records_all_three_stamps(self) -> None:
        self.service.proxy_token()
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 5)
        self.assertEqual(data["backend"], "local:" + byo_key.fingerprint("sk_live"))
        # Composio is addressed by the account; the Space is recorded beside it because
        # the *session* is per-Space, not because Composio ever sees it.
        self.assertEqual(data["account_id"], ACCOUNT)
        self.assertEqual(data["space_id"], "space-42")

    def _write_store(self, **overrides) -> None:
        doc = {
            "version": 5, "backend": "local:" + byo_key.fingerprint("sk_live"),
            "account_id": ACCOUNT, "space_id": "space-42",
            "session": "trs_existing", "proxy_tokens": ["keep-me"],
        }
        doc.update(overrides)
        self.sessions_path.write_text(json.dumps(doc), encoding="utf-8")
        self._reset()

    def test_a_store_from_another_space_keeps_its_tokens_but_drops_its_session(self):
        # The session carries that Space's toolkit allowlist and account pins, so
        # adopting it would hand this Space reach it was never granted.
        self._write_store(space_id="space-99", session="trs_theirs")
        self.service._ensure_sessions_loaded()
        self.assertIsNone(self.service._SESSION_ID)
        self.assertIn("keep-me", self.service._PROXY_TOKENS)
        self.assertIn("trs_theirs", self.service._ORPHANED_SESSION_IDS)
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["space_id"], "space-42")

    def test_a_store_written_before_the_space_stamp_is_adopted_then_stamped(self) -> None:
        # An unstamped store is almost certainly this pod's own, and discarding it would
        # churn a session for no reason. Unknown is not a mismatch.
        self._write_store(space_id=None)
        self.service._ensure_sessions_loaded()
        self.assertEqual(self.service._SESSION_ID, "trs_existing")
        # Adoption itself writes nothing (no churn); the next write is what stamps it.
        self.service._persist_session_id("trs_existing")
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["space_id"], "space-42")

    def test_a_pod_that_cannot_name_its_space_adopts_rather_than_discards(self) -> None:
        self._write_store(space_id="space-99")
        with patch.dict(os.environ, {"XO_SPACE_ID": ""}):
            self.service._ensure_sessions_loaded()
        self.assertEqual(self.service._SESSION_ID, "trs_existing")

    def test_a_store_from_another_account_keeps_its_tokens_but_drops_its_session(self):
        self.sessions_path.write_text(json.dumps({
            "version": 5, "backend": "local:" + byo_key.fingerprint("sk_live"),
            "account_id": "user_SOMEONEELSE", "session": "trs_theirs",
            "proxy_tokens": ["keep-me"],
        }), encoding="utf-8")
        self._reset()
        self.service._ensure_sessions_loaded()
        self.assertIsNone(self.service._SESSION_ID)          # minted for another account
        self.assertIn("keep-me", self.service._PROXY_TOKENS)  # local; agents keep their URL
        self.assertIn("trs_theirs", self.service._ORPHANED_SESSION_IDS)
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["account_id"], ACCOUNT)

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

    def test_proxy_token_is_stable_and_0600(self) -> None:
        first = self.service.proxy_token()
        self.assertEqual(first, self.service.proxy_token())
        self.assertEqual(stat.S_IMODE(self.sessions_path.stat().st_mode), 0o600)

    async def test_proxy_token_survives_a_restart_and_resolves_offline(self) -> None:
        token = self.service.proxy_token()
        self._reset()
        self.assertEqual(
            await self.service.account_for_proxy_token(token), byo_key.user_id())

    def test_empty_proxy_token_resolves_to_nobody(self) -> None:
        self.assertIsNone(self.service.account_for_proxy_token_local(""))

    def test_proxy_url_carries_the_token_and_port(self) -> None:
        with patch.dict(os.environ, {"PORT": "5010"}):
            url = self.service._composio_proxy_url()
        self.assertIn("http://127.0.0.1:5010/mcp/composio-proxy/u/", url)


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
        self.sign_in()
        self.assertEqual(await identity_mod.get_composio_user(_req()), ACCOUNT)

    async def test_unknown_account_is_409_but_the_optional_gate_reports_it(self) -> None:
        from fastapi import HTTPException
        from services.cowork_agent.connectors.composio import identity as identity_mod
        byo_key.save("sk_live")
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(_req())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["error"], "composio_identity_required")
        # The routes that must render the state cannot do it from behind the 409.
        self.assertIsNone(await identity_mod.get_composio_user_optional(_req()))

    async def test_cross_site_origin_is_403(self) -> None:
        from fastapi import HTTPException
        from services.cowork_agent.connectors.composio import identity as identity_mod
        byo_key.save("sk_live")
        req = _req({"origin": "https://evil.example", "sec-fetch-site": "cross-site"})
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(req)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_resolve_user_none_without_a_key_or_an_account(self) -> None:
        from services.cowork_agent.connectors.composio import identity as identity_mod
        self.assertIsNone(await identity_mod.resolve_user(_req()))
        byo_key.save("sk_live")
        self.assertIsNone(await identity_mod.resolve_user(_req()))   # key, no account
        self.sign_in()
        self.assertEqual(await identity_mod.resolve_user(_req()), ACCOUNT)


class StaleGatingErrorTests(_KeyBase):
    def test_stale_gating_errors_are_suppressed_but_real_ones_kept(self) -> None:
        from services.connections import service as conn_service
        from services.connections import poller
        from services.cowork_agent.connectors.composio import space_scope
        byo_key.save("sk_live")
        not_on = "googlecalendar is not turned on in this workspace"
        with patch.object(space_scope, "is_enabled", return_value=True):
            self.assertIsNone(conn_service._live_last_error("googlecalendar", not_on))
        with patch.object(space_scope, "is_enabled", return_value=False):
            self.assertEqual(conn_service._live_last_error("googlecalendar", not_on), not_on)
        # no-key error clears once a key is present
        self.assertIsNone(conn_service._live_last_error("gmail", poller.NOT_SIGNED_IN))
        byo_key.clear()
        self.assertEqual(conn_service._live_last_error("gmail", poller.NOT_SIGNED_IN),
                         poller.NOT_SIGNED_IN)
        # a real provider/collector error is never suppressed
        byo_key.save("sk_live")
        with patch.object(space_scope, "is_enabled", return_value=True):
            self.assertEqual(conn_service._live_last_error("gmail", "unread: quota exceeded"),
                             "unread: quota exceeded")


class ConnectionsSignedInTests(_KeyBase):
    def test_signed_in_tracks_the_composio_key_not_the_xo_token(self) -> None:
        from services.connections import service as conn_service
        self.assertFalse(conn_service.signed_in())      # no key configured
        byo_key.save("sk_live")
        self.assertTrue(conn_service.signed_in())        # key present, no XO token needed


class PollerUserTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    async def test_no_key_resolves_to_none(self) -> None:
        from services.connections import poller
        self.assertIsNone(await poller.resolve_user_id())

    async def test_key_alone_is_not_enough_then_resolves_to_the_account(self) -> None:
        from services.connections import poller
        byo_key.save("sk_live")
        self.assertIsNone(await poller.resolve_user_id())
        self.sign_in()
        self.assertEqual(await poller.resolve_user_id(), ACCOUNT)


class RouteTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    async def test_backend_route_reports_inactive_without_a_key(self) -> None:
        from routers.cowork_agent.connectors import composio as r
        resp = await r.get_backend(_req())
        self.assertEqual(json.loads(resp.body), {
            "mode": "inactive", "key_source": None, "signed_in": False})

    async def test_backend_route_reports_local_with_a_key(self) -> None:
        from routers.cowork_agent.connectors import composio as r
        byo_key.save("sk_live")
        self.sign_in()
        resp = await r.get_backend(_req())
        self.assertEqual(json.loads(resp.body), {
            "mode": "local", "key_source": "file", "signed_in": True})

    async def test_put_key_validates_and_saves(self) -> None:
        from routers.cowork_agent.connectors import composio as r
        from services.cowork_agent.connectors.composio import client as c
        from unittest.mock import AsyncMock
        with patch.object(c, "_sdk") as sdk, \
                patch.object(r.composio_service, "install_gateways", new=AsyncMock()), \
                patch.object(r.composio_service, "invalidate_session"):
            sdk.return_value.auth_configs.list.return_value = SimpleNamespace(items=[])
            resp = await r.put_api_key(r.ApiKeyBody(api_key="sk_live"), _req())
        self.assertEqual(json.loads(resp.body)["key_configured"], True)
        self.assertEqual(byo_key.api_key(), "sk_live")

    async def test_put_key_rejects_a_bad_key(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as r
        from services.cowork_agent.connectors.composio import client as c
        with patch.object(c, "_sdk") as sdk, \
                patch.object(r.composio_service, "invalidate_session"):
            sdk.return_value.auth_configs.list.side_effect = c.ComposioError(
                "rejected", authoritative=True)
            with self.assertRaises(HTTPException) as raised:
                await r.put_api_key(r.ApiKeyBody(api_key="bad"), _req())
        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(byo_key.configured())   # rolled back

    async def test_put_key_409_when_env(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as r
        with patch.dict(os.environ, {byo_key.ENV_VAR: "sk_env"}):
            with self.assertRaises(HTTPException) as raised:
                await r.put_api_key(r.ApiKeyBody(api_key="x"), _req())
        self.assertEqual(raised.exception.status_code, 409)

    async def test_connect_without_a_key_is_409(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as r
        with self.assertRaises(HTTPException) as raised:
            await r.connect("gmail", r.ConnectBody(), user_id=self.sign_in())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["error"], "composio_key_required")

    async def test_toolkits_without_a_key_are_needs_key(self) -> None:
        from routers.cowork_agent.connectors import composio as r
        resp = await r.list_toolkits(user_id=self.sign_in())
        body = json.loads(resp.body)
        self.assertFalse(body["key_configured"])
        self.assertTrue(body["signed_in"])
        self.assertTrue(all(t["status"] == "NEEDS_KEY" for t in body["toolkits"]))

    async def test_toolkits_without_an_account_are_needs_signin(self) -> None:
        from routers.cowork_agent.connectors import composio as r
        byo_key.save("sk_live")
        resp = await r.list_toolkits(user_id=None)
        body = json.loads(resp.body)
        self.assertTrue(body["key_configured"])     # the key is not the missing half
        self.assertFalse(body["signed_in"])
        self.assertTrue(all(t["status"] == "NEEDS_SIGNIN" for t in body["toolkits"]))


def _swarm_result(**kwargs):
    from services.swarm_api._http import SwarmResult
    kwargs.setdefault("ok", True)
    return SwarmResult(**kwargs)


class AccountIdentityTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    """The XO account id: how it is learned, cached and fallen back on."""

    async def test_unknown_until_resolved_and_never_guessed(self) -> None:
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-42"}):
            self.assertIsNone(account_identity.account_id())
            self.assertFalse(account_identity.known())
            with self.assertRaises(account_identity.XOAccountRequired):
                account_identity.require()

    async def test_remember_caches_on_disk_and_survives_a_restart(self) -> None:
        account_identity.remember(ACCOUNT)
        self.assertEqual(
            json.loads(self.identity_path.read_text(encoding="utf-8"))["account_id"],
            ACCOUNT)
        # A fresh process: memory empty, disk intact. This is the read the MCP hot path
        # and the boot sweep make before any swarm call has happened.
        account_identity._CACHED = None
        account_identity._LOADED = False
        self.assertEqual(account_identity.account_id(), ACCOUNT)

    async def test_the_env_override_pins_it_without_a_swarm(self) -> None:
        with patch.dict(os.environ, {account_identity.ENV_VAR: "user_env"}):
            self.assertEqual(account_identity.account_id(), "user_env")

    async def test_resolve_asks_get_user_id_once_and_caches(self) -> None:
        from services.swarm_api import auth as swarm_auth
        call = AsyncMock(return_value=_swarm_result(data={"user_id": ACCOUNT}))
        with patch.object(swarm_auth, "get_user_id", call):
            self.assertEqual(await account_identity.resolve(), ACCOUNT)
            self.assertEqual(await account_identity.resolve(), ACCOUNT)
        self.assertEqual(call.await_count, 1, "the second read came from the cache")
        self.assertTrue(self.identity_path.exists())

    async def test_an_unreachable_swarm_keeps_what_is_cached(self) -> None:
        from services.swarm_api import auth as swarm_auth
        account_identity.remember(ACCOUNT)
        failed = AsyncMock(return_value=_swarm_result(
            ok=False, offline=True, detail="connection refused"))
        with patch.object(swarm_auth, "get_user_id", failed):
            self.assertEqual(await account_identity.resolve(force=True), ACCOUNT)
        # Nothing was overwritten: an outage must not sign this Space out.
        self.assertEqual(
            json.loads(self.identity_path.read_text(encoding="utf-8"))["account_id"],
            ACCOUNT)

    async def test_an_unauthenticated_backend_resolves_to_nothing(self) -> None:
        from services.swarm_api import auth as swarm_auth
        none = AsyncMock(return_value=_swarm_result(ok=False, unauthenticated=True))
        with patch.object(swarm_auth, "get_user_id", none):
            self.assertIsNone(await account_identity.resolve())

    async def test_the_boot_sweep_waits_for_an_account_rather_than_installing(self):
        from services.cowork_agent.connectors.composio import service
        byo_key.save("sk_live")
        with patch.object(service, "gateway_install_agents", return_value=["codex"]), \
                patch.object(account_identity, "resolve", AsyncMock(return_value=None)), \
                patch.object(service, "_apply_to_agents") as apply_:
            sweep = await service.install_gateways(announce=False)
        apply_.assert_not_called()
        self.assertEqual(sweep.skipped, "no_account")
        # Signing in opens this gate without a restart, so the loop must keep trying.
        self.assertTrue(sweep.retryable)


if __name__ == "__main__":
    unittest.main()
