"""Tests for the Composio connector subpackage.

One TestCase per source module, per the suite convention. There is no conftest,
so isolation is explicit: `_ComposioBase.setUp` resets the module-level caches and
redirects every on-disk store into a temp dir.

Two traps this file works around, both easy to reintroduce:

- `service._write_store` and `action_prefs.bulk_set` take a lock via
  `visualizer.flock.locked`, which places its sentinel under
  `quirq_state_dir()/watcher/locks/` — the developer's real `~/.quirq` unless
  `QUIRQ_STATE_ROOT` points elsewhere. It is patched below.
- `identity._TOKEN_TTL_SECONDS` and `session_identity._SESSION_TTL` are evaluated
  at import, so `patch.dict(os.environ, ...)` cannot move them. Patch the
  attributes instead. Everything in `service.py` reads env at call time.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from routers.cowork_agent.connectors import composio as router_mod
from routers.cowork_agent.connectors import composio_mcp_proxy as mcp_proxy
from services import tenancy
from services.cowork_agent.connectors.composio import action_prefs, categories
from services.cowork_agent.connectors.composio import credentials
from services.cowork_agent.connectors.composio import identity as identity_mod
from services.cowork_agent.connectors.composio import service, session_identity, state

WORKSPACE = "ws-test"
ACCOUNT = "user_abc123"
# A literal, not composed: this repo no longer owns the format. xo-swarm-api does,
# and its tests/test_auth_tenancy.py is what pins it. This value only has to be a
# realistic string for the pod-side tests to pass around.
PRINCIPAL = "user_abc123__ws__ws-test"


def _make_request(headers: dict[str, str] | None = None, body: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp/composio-proxy/",
        "raw_path": b"/mcp/composio-proxy/",
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 5002),
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _ComposioBase(unittest.TestCase):
    """Temp stores, a known workspace, and a clean set of module caches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.sessions_path = tmp / "data" / "composio_sessions.json"
        self.prefs_path = tmp / "data" / "composio_action_prefs.json"

        env = patch.dict(
            os.environ,
            {
                tenancy.WORKSPACE_ENV: WORKSPACE,
                "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
                "COMPOSIO_API_KEY": "test-key",
                # Hermetic: the credentials provider must never reach for
                # xo-swarm-api here. Pinning `env` also keeps every existing
                # `patch.dict(os.environ, ...)` test in this file meaningful,
                # since env mode is deliberately uncached and read per call.
                # CredentialsTests below covers the swarm path on its own.
                "COMPOSIO_CREDENTIALS_SOURCE": "env",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        for patcher in (
            patch.object(service, "_SESSIONS_PATH", self.sessions_path),
            patch.object(action_prefs, "_store_path", return_value=self.prefs_path),
            # The developer's real XO_API_KEY is in this shell, and the tenant-state
            # client and the account-mismatch guard both reach for it. Without this the
            # suite would make live calls to xo-swarm-api. Tests that exercise those
            # paths patch get_auth_token themselves.
            patch("services.xo_credential.XO_API_KEY", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self._reset_caches()
        self.addCleanup(self._reset_caches)
        # The principal now comes from xo-swarm-api. Seed the fetched-value cache so
        # the suite stays hermetic; the tests that exercise the fetch itself call
        # state.invalidate() first and patch the transport.
        _now = time.monotonic()
        state._PRINCIPAL = (
            PRINCIPAL, _now + 3600, _now,
            {"principal": PRINCIPAL, "account_id": ACCOUNT, "workspace_id": WORKSPACE},
        )
        state.adopt_principal(PRINCIPAL)

    @staticmethod
    def _reset_caches() -> None:
        service._client = None
        service._client_key = ""
        credentials.invalidate()
        state.invalidate()
        service._SESSION_IDS.clear()
        service._PROXY_TOKENS.clear()
        service._SESSIONS_LOADED = False
        session_identity._SESSIONS.clear()

    @staticmethod
    def _fake_client(**overrides):
        """A Composio SDK stand-in. `service._attr` walks attributes or dicts."""
        base = SimpleNamespace(
            connected_accounts=SimpleNamespace(
                link=lambda **kw: SimpleNamespace(
                    redirect_url="https://composio.example/auth", id="cr_1"
                ),
                get=lambda cid: SimpleNamespace(status="ACTIVE", id=cid),
                list=lambda **kw: SimpleNamespace(items=[]),
                delete=lambda cid: None,
                update=lambda cid, **kw: SimpleNamespace(id=cid),
            ),
            tools=SimpleNamespace(get_raw_composio_tools=lambda **kw: []),
            sessions=SimpleNamespace(delete=lambda sid: None),
            use=lambda sid: SimpleNamespace(
                update=lambda **kw: None,
                mcp=SimpleNamespace(url="https://mcp.example/s", headers={}),
            ),
            create=lambda **kw: SimpleNamespace(
                session_id="sess_1",
                mcp=SimpleNamespace(url="https://mcp.example/s", headers={"x-a": "b"}),
            ),
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base


class ToolkitRegistryTests(_ComposioBase):
    def test_unknown_toolkit_is_rejected_by_name(self) -> None:
        with self.assertRaises(ValueError) as raised:
            service.toolkit_meta("nosuch")
        self.assertIn("nosuch", str(raised.exception))

    def test_toolkit_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(service.toolkit_meta("GMAIL").slug, "GMAIL")

    def test_missing_auth_config_names_the_env_key_to_set(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_AUTH_CONFIG_NOTION": ""}):
            with self.assertRaises(RuntimeError) as raised:
                service._auth_config_id_for("notion", "OAUTH2")
        self.assertIn("COMPOSIO_AUTH_CONFIG_NOTION", str(raised.exception))

    def test_unsupported_scheme_is_a_value_error_not_a_runtime_error(self) -> None:
        # The router maps both to 422, but only RuntimeError means "operator must
        # configure something" — keep them distinguishable.
        with self.assertRaises(ValueError):
            service._auth_config_id_for("notion", "API_KEY")

    def test_every_registered_toolkit_has_action_categories(self) -> None:
        # Pins the `supports_action_prefs` flag the /toolkits route emits: a
        # toolkit added to one table and not the other silently loses prefs.
        self.assertEqual(categories.classified_toolkits(), frozenset(service.TOOLKITS))

    def test_missing_api_key_is_reported_as_such(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_API_KEY": ""}):
            with self.assertRaises(RuntimeError) as raised:
                service._composio()
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))


class CredentialsTests(_ComposioBase):
    """The provider that fetches the Composio credentials from xo-swarm-api.

    `_ComposioBase` pins COMPOSIO_CREDENTIALS_SOURCE=env so the rest of the file
    never touches the network; every test here that exercises the swarm path
    flips it back explicitly with `_swarm()`.
    """

    SECRET = "ak_do_not_log_me"

    @staticmethod
    def _swarm():
        return patch.dict(os.environ, {"COMPOSIO_CREDENTIALS_SOURCE": "swarm"})

    @staticmethod
    def _response(status: int, payload: dict | None = None) -> httpx.Response:
        if payload is None:
            return httpx.Response(status, text="")
        return httpx.Response(status, json=payload)

    def _ok(self, api_key: str | None = None) -> httpx.Response:
        return self._response(200, {
            "api_key": api_key or self.SECRET,
            "auth_configs": {"COMPOSIO_AUTH_CONFIG_NOTION": "ac_notion"},
        })

    def test_env_mode_makes_no_http_call(self) -> None:
        # The escape hatch has to be genuinely offline, or a self-hosted install
        # would still need XO to be reachable.
        with patch.object(credentials, "_get") as get:
            self.assertEqual(credentials.api_key(), "test-key")
        get.assert_not_called()

    def test_the_bundle_is_fetched_once_and_cached(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()) as get:
            self.assertEqual(credentials.api_key(), self.SECRET)
            self.assertEqual(credentials.auth_config_id(
                "COMPOSIO_AUTH_CONFIG_NOTION"), "ac_notion")
        self.assertEqual(get.call_count, 1)

    def test_it_calls_the_same_channel_usage_sync_uses(self) -> None:
        from services.xo_credential import CHAT_API_BASE_URL

        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()) as get:
            credentials.api_key()

        url, headers = get.call_args[0]
        self.assertEqual(
            url,
            f"{CHAT_API_BASE_URL.rstrip('/')}/connectors/composio/credentials",
        )
        self.assertEqual(headers, {"Authorization": "Bearer tok"})

    def test_an_unknown_auth_config_is_none_not_an_error(self) -> None:
        # service._auth_config_id_for owns that message: it is the only caller
        # that knows the toolkit slug and the auth scheme.
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            self.assertIsNone(
                credentials.auth_config_id("COMPOSIO_AUTH_CONFIG_FIGMA"))

    def test_no_xo_credential_is_reported_against_the_key_name(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value=None):
            with self.assertRaises(RuntimeError) as raised:
                credentials.api_key()
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_503_is_authoritative_and_a_local_key_cannot_override_it(self) -> None:
        # The security regression guard. COMPOSIO_API_KEY is set in this process
        # (setUp does it, and the Setup tab can too), so a fallback here would let
        # anyone who can write this environment point the connector at their own
        # Composio project and harvest every subsequent OAuth grant.
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._response(503)):
            with self.assertRaises(credentials.CredentialsUnavailable) as raised:
                credentials.api_key()
        self.assertTrue(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_401_drops_the_cache_rather_than_serving_stale(self) -> None:
        # A revoked credential must stop working, not linger for an hour.
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            credentials.api_key()
        with self._swarm(), \
                patch.object(credentials, "_NEXT_ATTEMPT", 0.0), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._response(401)):
            with self.assertRaises(credentials.CredentialsUnavailable) as raised:
                credentials.api_key()
        self.assertTrue(raised.exception.authoritative)

    def test_an_unreachable_swarm_serves_the_cached_bundle(self) -> None:
        # A swarm restart must not take every connector down with it.
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            credentials.api_key()
        with self._swarm(), \
                patch.object(credentials, "_NEXT_ATTEMPT", 0.0), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get",
                             side_effect=httpx.ConnectError("refused")):
            self.assertEqual(credentials.api_key(), self.SECRET)

    def test_a_stale_bundle_eventually_expires(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            credentials.api_key()
        with self._swarm(), \
                patch.object(credentials, "_NEXT_ATTEMPT", 0.0), \
                patch.object(credentials, "_STALE_MAX", 0.0), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get",
                             side_effect=httpx.ConnectError("refused")):
            with self.assertRaises(credentials.CredentialsUnavailable):
                credentials.api_key()

    def test_an_empty_key_from_the_swarm_is_not_accepted(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._response(
                    200, {"api_key": "  ", "auth_configs": {}})):
            with self.assertRaises(credentials.CredentialsUnavailable) as raised:
                credentials.api_key()
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_rotated_key_rebuilds_the_sdk_client(self) -> None:
        built: list[str] = []

        class _Fake:
            def __init__(self, api_key: str) -> None:
                built.append(api_key)

        with patch.object(credentials, "api_key", side_effect=["k1", "k1", "k2"]), \
                patch("composio.Composio", _Fake):
            first = service._composio()
            second = service._composio()
            third = service._composio()

        # Same key => the memoized client is reused; a new key rebuilds it.
        self.assertIs(first, second)
        self.assertIsNot(second, third)
        self.assertEqual(built, ["k1", "k2"])

    def test_the_credential_never_reaches_the_log(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            with self.assertLogs(credentials.log, level="INFO") as captured:
                credentials.api_key()
        joined = "\n".join(captured.output)
        self.assertNotIn(self.SECRET, joined)
        # The names are safe and are what an operator actually needs.
        self.assertIn("COMPOSIO_AUTH_CONFIG_NOTION", joined)

    def test_status_reports_without_exposing_the_key(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get", return_value=self._ok()):
            snapshot = credentials.status()
        self.assertEqual(snapshot["source"], "swarm")
        self.assertEqual(snapshot["configured"], ["COMPOSIO_AUTH_CONFIG_NOTION"])
        self.assertNotIn(self.SECRET, repr(snapshot))


class StateClientTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """The xo-swarm-api tenant-state client."""

    @staticmethod
    def _response(status: int, payload: dict | None = None) -> httpx.Response:
        if payload is None:
            return httpx.Response(status, text="")
        return httpx.Response(status, json=payload)

    def _swarm(self):
        return patch.dict(os.environ, {"COMPOSIO_STATE_SOURCE": "swarm"})

    async def test_only_a_digest_ever_crosses_the_wire(self) -> None:
        # The swarm never needs the token itself, so it must never receive one.
        seen: list[dict] = []

        async def _fake(method, suffix="", *, params=None, json=None):
            seen.append(json or {})
            return {"principal": PRINCIPAL}

        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_arequest", side_effect=_fake):
            await state.resolve_proxy_token("plaintext-token-value-aaaaaaaaaaaa")

        self.assertNotIn("plaintext-token-value-aaaaaaaaaaaa", str(seen))
        digest = seen[0]["token_sha256"]
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, state.token_fingerprint("plaintext-token-value-aaaaaaaaaaaa"))

    async def test_a_resolved_token_is_cached(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_arequest",
                    new=AsyncMock(return_value={"principal": PRINCIPAL}),
                ) as arequest:
            self.assertEqual(await state.resolve_proxy_token("tok-aaaaaaaaaaaaaaaa"), PRINCIPAL)
            self.assertEqual(await state.resolve_proxy_token("tok-aaaaaaaaaaaaaaaa"), PRINCIPAL)
        self.assertEqual(arequest.await_count, 1)

    async def test_an_unknown_token_is_negatively_cached(self) -> None:
        # Without this a stale agent config retries forever over HTTP; locally it only
        # ever cost a dict miss.
        unavailable = state.StateUnavailable("not found", authoritative=True)
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_arequest", new=AsyncMock(side_effect=unavailable)) as arequest:
            self.assertIsNone(await state.resolve_proxy_token("tok-bbbbbbbbbbbbbbbb"))
            self.assertIsNone(await state.resolve_proxy_token("tok-bbbbbbbbbbbbbbbb"))
        self.assertEqual(arequest.await_count, 1)

    async def test_a_malformed_token_costs_nothing(self) -> None:
        with self._swarm(), patch.object(state, "_arequest") as arequest:
            self.assertIsNone(await state.resolve_proxy_token("has spaces!"))
        arequest.assert_not_called()

    async def test_concurrent_resolves_make_one_round_trip(self) -> None:
        import asyncio as _asyncio

        calls = 0

        async def _fake(*a, **kw):
            nonlocal calls
            calls += 1
            await _asyncio.sleep(0.01)
            return {"principal": PRINCIPAL}

        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_arequest", side_effect=_fake):
            results = await _asyncio.gather(
                *[state.resolve_proxy_token("tok-cccccccccccccccc") for _ in range(5)]
            )
        self.assertEqual(results, [PRINCIPAL] * 5)
        self.assertEqual(calls, 1)

    async def test_a_transient_failure_serves_the_stale_principal(self) -> None:
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_arequest",
                    new=AsyncMock(return_value={"principal": PRINCIPAL}),
                ):
            await state.resolve_proxy_token("tok-dddddddddddddddd")

        transient = state.StateUnavailable("swarm down")
        with self._swarm(), \
                patch.object(state, "_TOKEN_CACHE", dict(state._TOKEN_CACHE)), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_arequest", new=AsyncMock(side_effect=transient)):
            state._TOKEN_CACHE["tok-dddddddddddddddd"] = (
                PRINCIPAL, 0.0, __import__("time").monotonic()
            )
            self.assertEqual(
                await state.resolve_proxy_token("tok-dddddddddddddddd"), PRINCIPAL
            )

    async def test_a_transient_failure_with_a_cold_cache_raises(self) -> None:
        # The proxy turns this into a retryable 503, not a 401 — see McpProxyTests.
        transient = state.StateUnavailable("swarm down")
        with self._swarm(), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_arequest", new=AsyncMock(side_effect=transient)):
            with self.assertRaises(state.StateUnavailable) as raised:
                await state.resolve_proxy_token("tok-eeeeeeeeeeeeeeee")
        self.assertFalse(raised.exception.authoritative)

    def test_no_xo_credential_is_authoritative(self) -> None:
        with self._swarm(), patch("services.xo_credential.get_auth_token", return_value=None):
            with self.assertRaises(state.StateUnavailable) as raised:
                state._endpoint()
        self.assertTrue(raised.exception.authoritative)

    def test_a_token_from_another_workspace_is_refused(self) -> None:
        # The swarm filters by account, not workspace, so a resolve can legitimately
        # return a sibling workspace's principal. Adopting it would re-home that token
        # into this workspace's tenant — exactly what the workspace half prevents.
        with self.assertLogs(state.log, level="ERROR"):
            self.assertIsNone(
                state.assert_principal_is_ours("user_abc123__ws__someone-elses-ws")
            )

    def test_our_own_principal_passes_silently(self) -> None:
        self.assertEqual(state.assert_principal_is_ours(PRINCIPAL), PRINCIPAL)

    def test_it_accepts_when_this_pod_does_not_know_its_principal_yet(self) -> None:
        # A cold cache must not turn token recovery into a dead end.
        state.invalidate()
        self.assertEqual(state.assert_principal_is_ours("user_x__ws__ws-9"), "user_x__ws__ws-9")


class PrincipalTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """Fetching this pod's tenant key from xo-swarm-api.

    The format itself is pinned on the swarm (tests/test_auth_tenancy.py); this repo can
    only assert that it passes the string through untouched and does not re-grow a
    composer of its own.
    """

    def test_the_local_composer_has_not_come_back(self) -> None:
        for gone in ("SEPARATOR", "scoped_principal", "is_scoped"):
            self.assertFalse(
                hasattr(tenancy, gone),
                f"tenancy.{gone} is back — the tenant key is composed by xo-swarm-api "
                "and a second composer is what silently orphans connected accounts.",
            )

    async def test_the_swarm_s_principal_is_passed_through_byte_for_byte(self) -> None:
        # No strip, no case folding, no normalisation: Composio stores these bytes
        # against every connected account.
        weird = "user_AbC123__ws__Ws-Test_-9"
        state.invalidate()
        with patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", return_value={"principal": weird}):
            self.assertEqual(await state.aprincipal(), weird)

    async def test_it_is_fetched_once_and_cached(self) -> None:
        state.invalidate()
        with patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", return_value={"principal": PRINCIPAL}
                ) as request:
            self.assertEqual(await state.aprincipal(), PRINCIPAL)
            self.assertEqual(await state.aprincipal(), PRINCIPAL)
        self.assertEqual(request.call_count, 1)

    async def test_an_unreachable_swarm_falls_back_to_the_store_owner(self) -> None:
        # A pod that booted once knows whose rows it holds, so it rides out an outage.
        state.invalidate()
        state.adopt_principal(PRINCIPAL)
        with patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=state.StateUnavailable("down")
                ):
            self.assertEqual(await state.aprincipal(), PRINCIPAL)

    async def test_a_revoked_credential_does_not_fall_back_to_the_store(self) -> None:
        # Authoritative means XO said no. A revoked key must stop working, not linger.
        state.invalidate()
        state.adopt_principal(PRINCIPAL)
        rejected = state.StateUnavailable("rejected", authoritative=True)
        with patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=rejected):
            with self.assertRaises(state.StateUnavailable):
                await state.aprincipal()

    async def test_a_swarm_without_the_route_falls_back_to_the_store(self) -> None:
        # 404 here is a deploy-ordering slip, not a refusal — it must not take Composio
        # down when this pod's own store already names its owner.
        state.invalidate()
        state.adopt_principal(PRINCIPAL)
        missing = state.StateUnavailable("nf", authoritative=True, not_found=True)
        with patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=missing):
            with self.assertLogs(state.log, level="ERROR"):
                self.assertEqual(await state.aprincipal(), PRINCIPAL)


class ProxyTokenTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    def test_blank_user_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            service.proxy_token_for_user("   ")

    def test_token_is_stable_for_the_same_principal(self) -> None:
        first = service.proxy_token_for_user(PRINCIPAL)
        second = service.proxy_token_for_user(PRINCIPAL)
        self.assertEqual(first, second)

    async def test_token_survives_a_process_restart(self) -> None:
        token = service.proxy_token_for_user(PRINCIPAL)
        self._reset_caches()
        self.assertEqual(await service.user_for_proxy_token(token), PRINCIPAL)

    async def test_a_pre_ownership_document_is_ignored_not_upgraded(self) -> None:
        # v1 rows predate workspace scoping and address the account-wide Composio
        # bucket every workspace of the account would share. Ignored, never upgraded.
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(
            json.dumps({"version": 1, "proxy_tokens": {"legacy-token": ACCOUNT}}),
            encoding="utf-8",
        )
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()
        self.assertIsNone(service.user_for_proxy_token_local("legacy-token"))

    async def test_another_workspace_s_rows_are_dropped_at_load(self) -> None:
        # Strictly stronger than the old shape check: this row is correctly formed and
        # still refused, because it belongs to a sibling workspace.
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(
            json.dumps({
                "version": 3,
                "principal": PRINCIPAL,
                "sessions": {},
                "proxy_tokens": {"ours": PRINCIPAL, "theirs": "user_abc123__ws__other"},
            }),
            encoding="utf-8",
        )
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()
        self.assertEqual(service.user_for_proxy_token_local("ours"), PRINCIPAL)
        self.assertIsNone(service.user_for_proxy_token_local("theirs"))

    async def test_empty_token_resolves_to_nobody(self) -> None:
        self.assertIsNone(await service.user_for_proxy_token(""))

    def test_a_token_the_swarm_did_not_record_is_reported_undurable(self) -> None:
        # Minting must not silently proceed as if durable: a token the swarm never saw
        # dies with this pod's store, and refresh-gateway would fail too.
        with patch.dict(os.environ, {"COMPOSIO_STATE_SOURCE": "swarm"}), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=state.StateUnavailable("down")
                ):
            token = service.proxy_token_for_user(PRINCIPAL)
        self.assertTrue(token)
        self.assertFalse(service.last_token_was_durable())

    def test_a_recorded_token_is_reported_durable(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_STATE_SOURCE": "swarm"}), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", return_value={"principal": PRINCIPAL}):
            service.proxy_token_for_user(PRINCIPAL)
        self.assertTrue(service.last_token_was_durable())

    def test_minting_sends_only_a_digest(self) -> None:
        seen: list[dict] = []
        with patch.dict(os.environ, {"COMPOSIO_STATE_SOURCE": "swarm"}), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request",
                    side_effect=lambda *a, **kw: seen.append(kw.get("json") or {}) or {},
                ):
            token = service.proxy_token_for_user(PRINCIPAL)
        self.assertNotIn(token, str(seen))
        self.assertEqual(seen[0]["token_sha256"], state.token_fingerprint(token))

    async def test_a_local_hit_never_touches_the_network_with_a_cold_principal(self) -> None:
        # Constraint, executable: the MCP proxy calls this on every tool call. A pod
        # that restarts during a swarm outage must still serve tokens it physically
        # holds — the store names its owner, so no network is needed to classify them.
        token = service.proxy_token_for_user(PRINCIPAL)
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()
        state.invalidate()                     # principal unknown; store still owns
        with patch.object(state, "_arequest") as arequest, \
                patch.object(state, "_request") as request:
            self.assertEqual(await service.user_for_proxy_token(token), PRINCIPAL)
        arequest.assert_not_called()
        request.assert_not_called()

    async def test_a_local_hit_never_touches_the_network(self) -> None:
        # The hot path: the MCP proxy calls this on every tool call, so the steady
        # state must stay a dict lookup.
        token = service.proxy_token_for_user(PRINCIPAL)
        with patch.object(state, "_arequest") as arequest:
            self.assertEqual(await service.user_for_proxy_token(token), PRINCIPAL)
        arequest.assert_not_called()

    def test_store_is_written_private_and_versioned(self) -> None:
        service.proxy_token_for_user(PRINCIPAL)
        self.assertEqual(
            stat.S_IMODE(self.sessions_path.stat().st_mode), 0o600
        )
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 3)
        # The document names its owner, which is what lets the pod classify its own
        # rows with no network — and refuse another workspace's.
        self.assertEqual(data["principal"], PRINCIPAL)
        self.assertEqual(list(data["proxy_tokens"].values()), [PRINCIPAL])

    def test_proxy_url_carries_the_token_and_configured_port(self) -> None:
        with patch.dict(os.environ, {"PORT": "5010"}):
            url = service._composio_proxy_url(PRINCIPAL)
        self.assertIn("http://127.0.0.1:5010/mcp/composio-proxy/u/", url)


class ServiceDegradationTests(_ComposioBase):
    """The read paths swallow SDK faults; the MCP entry deliberately does not."""

    def _boom(self, *_a, **_kw):
        raise RuntimeError("composio is down")

    def test_check_connection_reports_failure_instead_of_raising(self) -> None:
        client = self._fake_client(
            connected_accounts=SimpleNamespace(get=self._boom)
        )
        with patch.object(service, "_composio", return_value=client):
            result = service.check_connection("cr_1")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("composio is down", result["error"])

    def test_list_connections_degrades_to_empty(self) -> None:
        client = self._fake_client(
            connected_accounts=SimpleNamespace(list=self._boom)
        )
        with patch.object(service, "_composio", return_value=client):
            self.assertEqual(service.list_connections(PRINCIPAL), [])

    def test_disconnect_reports_false_instead_of_raising(self) -> None:
        client = self._fake_client(
            connected_accounts=SimpleNamespace(delete=self._boom)
        )
        with patch.object(service, "_composio", return_value=client):
            self.assertFalse(service.disconnect("ca_1"))

    def test_list_tools_degrades_to_empty(self) -> None:
        client = self._fake_client(
            tools=SimpleNamespace(get_raw_composio_tools=self._boom)
        )
        with patch.object(service, "_composio", return_value=client):
            self.assertEqual(service.list_tools(PRINCIPAL, "gmail"), [])

    def test_listing_many_tools_reads_the_prefs_once(self) -> None:
        # This ran per-tool, so a 200-tool toolkit meant 200 reads of the whole prefs
        # store. Tolerable against a local file; not once the store is remote.
        tools = [SimpleNamespace(slug=f"GMAIL_ACTION_{i}", name="") for i in range(200)]
        client = self._fake_client(
            tools=SimpleNamespace(get_raw_composio_tools=lambda **kw: tools)
        )
        with patch.object(service, "_composio", return_value=client), \
                patch.object(
                    action_prefs, "load_prefs", return_value={}
                ) as load_prefs:
            out = service.list_tools(PRINCIPAL, "gmail")
        self.assertEqual(len(out), 200)
        self.assertEqual(load_prefs.call_count, 1)

    def test_connection_rows_are_normalised_across_sdk_shapes(self) -> None:
        rows = SimpleNamespace(items=[
            SimpleNamespace(
                toolkit=SimpleNamespace(slug="gmail"),
                id="ca_1", status="ACTIVE", auth_scheme="OAUTH2",
            ),
            {"toolkit_slug": "notion", "id": "ca_2", "status": "INITIATED"},
        ])
        client = self._fake_client(
            connected_accounts=SimpleNamespace(list=lambda **kw: rows)
        )
        with patch.object(service, "_composio", return_value=client):
            out = service.list_connections(PRINCIPAL)
        self.assertEqual([r["toolkit"] for r in out], ["GMAIL", "NOTION"])
        self.assertEqual(out[0]["connected_account_id"], "ca_1")

    def test_disabled_actions_are_hidden_unless_explicitly_included(self) -> None:
        tools = [SimpleNamespace(
            slug="GMAIL_SEND_EMAIL", name="Send", description="", input_parameters={},
        )]
        client = self._fake_client(
            tools=SimpleNamespace(get_raw_composio_tools=lambda **kw: tools)
        )
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False}, PRINCIPAL)
        with patch.object(service, "_composio", return_value=client):
            self.assertEqual(service.list_tools(PRINCIPAL, "gmail"), [])
            shown = service.list_tools(PRINCIPAL, "gmail", include_disabled=True)
        self.assertEqual(len(shown), 1)
        self.assertFalse(shown[0]["enabled"])

    def test_mcp_entry_refuses_a_session_with_no_url(self) -> None:
        # Without the guard the entry would carry the literal string "None", which
        # is truthy and fails much later as an opaque connection error.
        client = self._fake_client(
            create=lambda **kw: SimpleNamespace(
                session_id="s1", mcp=SimpleNamespace(url=None, headers=None)
            )
        )
        with patch.object(service, "_composio", return_value=client):
            with self.assertRaises(RuntimeError) as raised:
                service.build_mcp_server_entry(PRINCIPAL)
        self.assertIn("no MCP url", str(raised.exception))

    def test_mcp_entry_carries_url_and_headers(self) -> None:
        with patch.object(service, "_composio", return_value=self._fake_client()):
            entry = service.build_mcp_server_entry(PRINCIPAL)
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "https://mcp.example/s")
        self.assertEqual(entry["headers"], {"x-a": "b"})


def _account(cid, slug="gmail", *, status="ACTIVE", alias=None, created_at=None,
             is_disabled=False):
    """A connected-account row in the SDK's list shape."""
    return SimpleNamespace(
        id=cid,
        toolkit=SimpleNamespace(slug=slug),
        status=status,
        alias=alias,
        created_at=created_at,
        is_disabled=is_disabled,
        auth_scheme="OAUTH2",
    )


class MultiAccountTests(_ComposioBase):
    """Several accounts of one toolkit per principal (aliases, pinning, session).

    Two independent switches, and the tests keep them apart: `allow_multiple` on
    /connect decides whether Composio *stores* a second account, while
    COMPOSIO_MULTI_ACCOUNT decides whether more than one of them can reach a
    session at the same time.
    """

    def _client_listing(self, items, *, capture=None):
        def _list(**kw):
            if capture is not None:
                capture.append(kw)
            return SimpleNamespace(items=items)

        return self._fake_client(
            connected_accounts=SimpleNamespace(
                list=_list,
                link=lambda **kw: SimpleNamespace(
                    redirect_url="https://composio.example/auth", id="cr_1"
                ),
                update=lambda cid, **kw: SimpleNamespace(id=cid),
                get=lambda cid: SimpleNamespace(status="ACTIVE", id=cid),
                delete=lambda cid: None,
            )
        )

    # ---- configuration ----

    def test_multi_account_is_off_unless_asked_for(self) -> None:
        self.assertIsNone(service.multi_account_config())
        self.assertFalse(service.multi_account_enabled())

    def test_enabling_yields_composio_defaults(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}):
            config = service.multi_account_config()
        self.assertEqual(config, {
            "enable": True,
            "max_accounts_per_toolkit": service.MULTI_ACCOUNT_DEFAULT_MAX,
            "require_explicit_selection": False,
        })

    def test_max_outside_the_supported_range_is_clamped_not_forwarded(self) -> None:
        # Composio rejects a max outside 2-10, and a session that cannot be
        # created costs the user every tool, so an operator typo is clamped.
        for raw, expected in (("99", 10), ("1", 2), ("notanumber", 5)):
            with patch.dict(os.environ, {
                "COMPOSIO_MULTI_ACCOUNT": "true",
                "COMPOSIO_MULTI_ACCOUNT_MAX": raw,
            }):
                config = service.multi_account_config()
            self.assertEqual(config["max_accounts_per_toolkit"], expected, raw)

    def test_explicit_selection_is_passed_through(self) -> None:
        with patch.dict(os.environ, {
            "COMPOSIO_MULTI_ACCOUNT": "yes",
            "COMPOSIO_MULTI_ACCOUNT_REQUIRE_SELECTION": "on",
        }):
            self.assertTrue(
                service.multi_account_config()["require_explicit_selection"]
            )

    # ---- aliases ----

    def test_alias_is_trimmed_and_blank_means_cleared(self) -> None:
        self.assertEqual(service.normalize_alias("  work-gmail "), "work-gmail")
        self.assertIsNone(service.normalize_alias("   "))
        self.assertIsNone(service.normalize_alias(None))

    def test_over_long_alias_is_refused_before_the_api_call(self) -> None:
        with self.assertRaises(ValueError):
            service.normalize_alias("x" * (service.ALIAS_MAX_LENGTH + 1))

    def test_duplicate_alias_is_caught_locally_and_names_the_holder(self) -> None:
        client = self._client_listing([_account("ca_1", alias="Work-Gmail")])
        with patch.object(service, "_composio", return_value=client):
            with self.assertRaises(service.AliasInUseError) as raised:
                # Composio's uniqueness is per user and toolkit; casing must not
                # be a way around it.
                service.assert_alias_free(PRINCIPAL, "gmail", "work-gmail")
        self.assertIn("ca_1", str(raised.exception))

    def test_renaming_an_account_to_its_own_alias_is_not_a_collision(self) -> None:
        client = self._client_listing([_account("ca_1", alias="work-gmail")])
        with patch.object(service, "_composio", return_value=client):
            service.assert_alias_free(
                PRINCIPAL, "gmail", "work-gmail", except_account_id="ca_1",
            )

    def test_alias_only_collides_within_the_same_toolkit(self) -> None:
        capture: list[dict] = []
        client = self._client_listing([], capture=capture)
        with patch.object(service, "_composio", return_value=client):
            service.assert_alias_free(PRINCIPAL, "notion", "shared-name")
        self.assertEqual(capture[0]["toolkit_slugs"], ["notion"])

    def test_set_alias_clears_with_an_empty_string_not_none(self) -> None:
        # connected_accounts.update(alias=None) leaves the alias alone; "" is
        # what actually clears it.
        seen: list[dict] = []
        client = self._fake_client(
            connected_accounts=SimpleNamespace(
                update=lambda cid, **kw: seen.append({"id": cid, **kw}),
            )
        )
        with patch.object(service, "_composio", return_value=client):
            self.assertIsNone(service.set_alias("ca_1", "  "))
            self.assertEqual(service.set_alias("ca_1", "work"), "work")
        self.assertEqual(seen[0], {"id": "ca_1", "alias": ""})
        self.assertEqual(seen[1], {"id": "ca_1", "alias": "work"})

    def test_failed_alias_write_raises_rather_than_reporting_success(self) -> None:
        def _boom(*_a, **_kw):
            raise RuntimeError("composio is down")

        client = self._fake_client(
            connected_accounts=SimpleNamespace(update=_boom)
        )
        with patch.object(service, "_composio", return_value=client):
            with self.assertRaises(RuntimeError):
                service.set_alias("ca_1", "work")

    # ---- connecting a second account ----

    def test_connect_forwards_alias_and_allow_multiple(self) -> None:
        seen: list[dict] = []
        client = self._fake_client(
            connected_accounts=SimpleNamespace(
                list=lambda **kw: SimpleNamespace(items=[]),
                link=lambda **kw: seen.append(kw) or SimpleNamespace(
                    redirect_url="https://composio.example/auth", id="cr_1"
                ),
            )
        )
        with patch.dict(os.environ, {"COMPOSIO_AUTH_CONFIG_GMAIL": "ac_1"}), \
                patch.object(service, "_composio", return_value=client):
            result = service.initiate_connection(
                PRINCIPAL, "gmail", alias=" work-gmail ", allow_multiple=True,
            )
        self.assertEqual(seen[0]["alias"], "work-gmail")
        self.assertTrue(seen[0]["allow_multiple"])
        self.assertEqual(result["alias"], "work-gmail")

    def test_a_plain_connect_sends_neither_alias_nor_allow_multiple(self) -> None:
        # The single-account flow must keep its exact previous request shape.
        seen: list[dict] = []
        client = self._fake_client(
            connected_accounts=SimpleNamespace(
                link=lambda **kw: seen.append(kw) or SimpleNamespace(
                    redirect_url="https://composio.example/auth", id="cr_1"
                ),
            )
        )
        with patch.dict(os.environ, {"COMPOSIO_AUTH_CONFIG_GMAIL": "ac_1"}), \
                patch.object(service, "_composio", return_value=client):
            service.initiate_connection(PRINCIPAL, "gmail")
        self.assertEqual(
            set(seen[0]), {"user_id", "auth_config_id", "callback_url"}
        )

    # ---- listing ----

    def test_accounts_are_listed_newest_first(self) -> None:
        client = self._client_listing([
            _account("ca_old", created_at="2026-01-01T00:00:00Z"),
            _account("ca_new", created_at="2026-06-01T00:00:00Z"),
        ])
        with patch.object(service, "_composio", return_value=client):
            rows = service.list_toolkit_accounts(PRINCIPAL, "gmail")
        self.assertEqual(
            [r["connected_account_id"] for r in rows], ["ca_new", "ca_old"]
        )

    def test_a_row_with_no_timestamp_sorts_last_instead_of_crashing(self) -> None:
        client = self._client_listing([
            _account("ca_undated"),
            _account("ca_dated", created_at="2026-01-01T00:00:00Z"),
        ])
        with patch.object(service, "_composio", return_value=client):
            rows = service.list_toolkit_accounts(PRINCIPAL, "gmail")
        self.assertEqual(
            [r["connected_account_id"] for r in rows], ["ca_dated", "ca_undated"]
        )

    def test_foreign_toolkit_rows_are_dropped_even_if_the_api_ignores_the_filter(self) -> None:
        client = self._client_listing([
            _account("ca_1", "gmail"), _account("ca_2", "notion"),
        ])
        with patch.object(service, "_composio", return_value=client):
            rows = service.list_toolkit_accounts(PRINCIPAL, "gmail")
        self.assertEqual([r["connected_account_id"] for r in rows], ["ca_1"])

    def test_alias_and_created_at_reach_the_caller(self) -> None:
        client = self._client_listing([
            _account("ca_1", alias="work", created_at="2026-01-01T00:00:00Z"),
        ])
        with patch.object(service, "_composio", return_value=client):
            row = service.list_connections(PRINCIPAL)[0]
        self.assertEqual(row["alias"], "work")
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00Z")
        self.assertFalse(row["is_disabled"])

    # ---- pinning ----

    def test_one_account_per_toolkit_reaches_the_session_when_multi_is_off(self) -> None:
        # A session created with two ids for one toolkit is rejected outright, so
        # the newest wins — the same account Composio would have picked.
        client = self._client_listing([
            _account("ca_old", created_at="2026-01-01T00:00:00Z"),
            _account("ca_new", created_at="2026-06-01T00:00:00Z"),
        ])
        with patch.object(service, "_composio", return_value=client):
            pinned = service.pinned_connected_accounts(PRINCIPAL)
        self.assertEqual(pinned, {"gmail": ["ca_new"]})

    def test_several_accounts_are_pinned_when_multi_is_on(self) -> None:
        client = self._client_listing([
            _account("ca_old", created_at="2026-01-01T00:00:00Z"),
            _account("ca_new", created_at="2026-06-01T00:00:00Z"),
        ])
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}), \
                patch.object(service, "_composio", return_value=client):
            pinned = service.pinned_connected_accounts(PRINCIPAL)
        self.assertEqual(pinned, {"gmail": ["ca_new", "ca_old"]})

    def test_pinning_never_exceeds_the_configured_maximum(self) -> None:
        client = self._client_listing([
            _account(f"ca_{i}", created_at=f"2026-0{i}-01T00:00:00Z")
            for i in range(1, 5)
        ])
        with patch.dict(os.environ, {
            "COMPOSIO_MULTI_ACCOUNT": "1", "COMPOSIO_MULTI_ACCOUNT_MAX": "2",
        }), patch.object(service, "_composio", return_value=client):
            pinned = service.pinned_connected_accounts(PRINCIPAL)
        self.assertEqual(pinned, {"gmail": ["ca_4", "ca_3"]})

    def test_a_disabled_account_is_not_pinned(self) -> None:
        client = self._client_listing([
            _account("ca_off", created_at="2026-06-01T00:00:00Z", is_disabled=True),
            _account("ca_on", created_at="2026-01-01T00:00:00Z"),
        ])
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}), \
                patch.object(service, "_composio", return_value=client):
            pinned = service.pinned_connected_accounts(PRINCIPAL)
        self.assertEqual(pinned, {"gmail": ["ca_on"]})

    # ---- the session ----

    def test_session_creation_omits_multi_account_when_the_flag_is_off(self) -> None:
        seen: list[dict] = []
        client = self._fake_client(
            create=lambda **kw: seen.append(kw) or SimpleNamespace(
                session_id="sess_1",
                mcp=SimpleNamespace(url="https://mcp.example/s", headers={}),
            )
        )
        with patch.object(service, "_composio", return_value=client):
            service.get_session(PRINCIPAL)
        self.assertNotIn("multi_account", seen[0])

    def test_session_creation_carries_the_multi_account_block(self) -> None:
        seen: list[dict] = []
        client = self._fake_client(
            create=lambda **kw: seen.append(kw) or SimpleNamespace(
                session_id="sess_1",
                mcp=SimpleNamespace(url="https://mcp.example/s", headers={}),
            )
        )
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}), \
                patch.object(service, "_composio", return_value=client):
            service.get_session(PRINCIPAL)
        self.assertTrue(seen[0]["multi_account"]["enable"])

    def test_an_existing_session_converges_when_the_flag_is_turned_off(self) -> None:
        # multi_account is sent as None rather than omitted: a session minted
        # while the flag was on must stop being a multi-account session.
        seen: list[dict] = []
        client = self._fake_client(
            use=lambda sid: SimpleNamespace(
                update=lambda **kw: seen.append(kw),
                mcp=SimpleNamespace(url="https://mcp.example/s", headers={}),
            )
        )
        service._SESSIONS_LOADED = True
        service._SESSION_IDS[PRINCIPAL] = "sess_1"
        with patch.object(service, "_composio", return_value=client):
            service.sync_session(PRINCIPAL)
        self.assertIsNone(seen[0]["multi_account"])

    def test_a_rejected_session_update_falls_back_to_a_re_mint(self) -> None:
        def _boom(**_kw):
            raise RuntimeError("unsupported field")

        client = self._fake_client(
            use=lambda sid: SimpleNamespace(update=_boom),
        )
        service._SESSIONS_LOADED = True
        service._SESSION_IDS[PRINCIPAL] = "sess_1"
        with patch.object(service, "_composio", return_value=client):
            service.sync_session(PRINCIPAL)
        self.assertNotIn(PRINCIPAL, service._SESSION_IDS)


class ActionPrefsTests(_ComposioBase):
    def test_actions_are_enabled_by_default(self) -> None:
        # Only disabled slugs are stored, so absence is what "enabled" means.
        self.assertNotIn("GMAIL_ANY", action_prefs.disabled_slugs(PRINCIPAL, "gmail"))

    def test_only_disabled_actions_are_persisted(self) -> None:
        action_prefs.bulk_set(
            "gmail", {"GMAIL_SEND_EMAIL": False, "GMAIL_FETCH_EMAILS": True}, PRINCIPAL,
        )
        stored = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["users"][PRINCIPAL]["gmail"], {"GMAIL_SEND_EMAIL": False}
        )

    def test_re_enabling_the_last_action_prunes_the_user(self) -> None:
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False}, PRINCIPAL)
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": True}, PRINCIPAL)
        stored = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["users"], {})

    def test_prefs_are_per_user(self) -> None:
        other = "user_other__ws__ws-test"
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False}, PRINCIPAL)
        self.assertNotIn(
            "GMAIL_SEND_EMAIL", action_prefs.disabled_slugs(other, "gmail")
        )

    def test_pre_v2_document_is_ignored_rather_than_misread(self) -> None:
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(
            json.dumps({"gmail": {"GMAIL_SEND_EMAIL": False}}), encoding="utf-8",
        )
        self.assertEqual(action_prefs.load_all(), {})

    def test_blank_user_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            action_prefs.load_prefs("")


class SessionIdentityTests(_ComposioBase):
    """Session ids are a gate for the browser, not an identity map.

    They are minted by xo-swarm-api now; this module only records what the pass-through
    route was handed, so that checking one on the MCP hot path stays a dict lookup.
    Unguessability is the swarm's property, and the swarm's test.
    """

    def test_a_session_carries_no_account_identity(self) -> None:
        # It used to store an account id. There is nothing to store: this backend has
        # one principal, and the record only says the swarm vouched for this id.
        sid = session_identity.remember(secrets.token_urlsafe(32))
        self.assertTrue(session_identity.is_valid(sid))
        self.assertIsInstance(session_identity._SESSIONS[sid], float)

    def test_expired_session_is_dropped_on_read(self) -> None:
        sid = session_identity.remember(secrets.token_urlsafe(32))
        session_identity._SESSIONS[sid] = time.monotonic() - 1
        self.assertFalse(session_identity.is_valid(sid))
        self.assertNotIn(sid, session_identity._SESSIONS)

    def test_an_unknown_session_is_not_valid(self) -> None:
        self.assertFalse(session_identity.is_valid("nope"))
        self.assertFalse(session_identity.is_valid(""))
        self.assertFalse(session_identity.is_valid(None))

    def test_distinct_ids_are_recorded_separately(self) -> None:
        ids = {session_identity.remember(secrets.token_urlsafe(32)) for _ in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(session_identity.is_valid(i) for i in ids))

    def test_an_unusable_ttl_falls_back_rather_than_expiring_at_once(self) -> None:
        # The swarm's `expires_in` shortens the local record; a missing or nonsense
        # value must not make the id the browser was just handed already dead.
        for ttl in (None, 0, -5, "nonsense"):
            with self.subTest(ttl=ttl):
                sid = session_identity.remember(secrets.token_urlsafe(32), ttl_seconds=ttl)
                self.assertTrue(session_identity.is_valid(sid))

    def test_the_swarm_s_expiry_bounds_the_local_record(self) -> None:
        sid = session_identity.remember(secrets.token_urlsafe(32), ttl_seconds=30)
        self.assertLessEqual(
            session_identity._SESSIONS[sid] - time.monotonic(), 30.0
        )


class RemovedEndpointTests(_ComposioBase):
    """The xo-auth surface is one route, and must stay one route.

    ``POST /xo-auth/session`` minted a session for *another* account. Since xo-swarm-api
    composes the tenant key from the credential this backend presents, such a session
    would silently receive this backend's principal — and its Composio connections with
    it. ``POST /xo-auth/consume`` went with the auth module: consuming is a boot-time
    call into ``services.xo_credential``, not an HTTP surface. Only the pass-through that
    the shipped UI calls survives.
    """

    def test_only_the_session_self_pass_through_is_exposed(self) -> None:
        import routers.cowork_agent.connectors.composio_session as session_mod

        self.assertFalse(hasattr(session_mod, "xo_auth_session"))
        registered = {
            (method, route.path)
            for route in session_mod.router.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("POST", "/xo-auth/session"), registered)
        self.assertNotIn(("POST", "/xo-auth/consume"), registered)
        self.assertEqual(registered, {("GET", "/xo-auth/session/self")})

    def test_the_auth_module_is_gone_from_this_repo(self) -> None:
        # xo-swarm-api owns authentication. What is left here is one credential
        # (services/xo_credential.py), not an auth subsystem.
        import importlib

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("routers.auth.auth")

    def test_the_account_matching_guard_is_gone_with_it(self) -> None:
        # The guard existed only to refuse those sessions. Keeping it without the
        # endpoint would be dead code; removing the endpoint without it would be a
        # silent cross-account read.
        self.assertFalse(hasattr(identity_mod, "_account_matches_backend"))


class IdentityTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    def test_session_header_wins_over_authorization(self) -> None:
        request = _make_request(
            {"x-xo-session": "sid-1", "authorization": "Bearer raw-token"}
        )
        self.assertEqual(identity_mod._extract_bearer(request), "sid-1")

    def test_non_bearer_authorization_is_ignored(self) -> None:
        request = _make_request({"authorization": "Basic abc"})
        self.assertIsNone(identity_mod._extract_bearer(request))

    async def test_missing_bearer_is_a_401_naming_the_header(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(_make_request())
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("X-XO-Session", raised.exception.detail)

    async def test_missing_workspace_reports_itself_not_a_bad_token(self) -> None:
        # The workspace check runs before resolution on purpose: otherwise a
        # deployment with no CODER_WORKSPACE_ID reports "invalid or expired
        # bearer token" and sends the operator hunting the wrong problem.
        request = _make_request({"x-xo-session": "sid-1"})
        with patch.dict(os.environ, {tenancy.WORKSPACE_ENV: ""}):
            with self.assertRaises(HTTPException) as raised:
                await identity_mod.get_composio_user(request)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("Workspace identity unavailable", raised.exception.detail)

    async def test_an_unrecognised_session_is_a_401(self) -> None:
        request = _make_request({"x-xo-session": "sid-1"})
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(request)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("Invalid or expired session", raised.exception.detail)

    async def test_a_valid_session_yields_this_pod_s_principal(self) -> None:
        sid = session_identity.remember(secrets.token_urlsafe(32))
        request = _make_request({"x-xo-session": sid})
        self.assertEqual(
            await identity_mod.resolve_user_from_bearer(request), PRINCIPAL
        )

    async def test_an_unknown_session_yields_nothing(self) -> None:
        # The bearer is a gate now: an id this pod did not mint buys nothing.
        request = _make_request({"x-xo-session": "not-a-real-session"})
        self.assertIsNone(await identity_mod.resolve_user_from_bearer(request))

    async def test_no_workspace_yields_no_principal_never_a_bare_account(self) -> None:
        state.invalidate()
        sid = session_identity.remember(secrets.token_urlsafe(32))
        request = _make_request({"x-xo-session": sid})
        with patch.dict(os.environ, {tenancy.WORKSPACE_ENV: ""}):
            self.assertIsNone(await identity_mod.resolve_user_from_bearer(request))


class McpProxyTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    async def test_unknown_token_is_told_to_reinstall(self) -> None:
        response = await mcp_proxy._proxy(_make_request(), "POST", "no-such-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            json.loads(response.body)["error"], "composio_identity_required"
        )

    async def test_absent_token_is_rejected_the_same_way(self) -> None:
        response = await mcp_proxy._proxy(_make_request(), "POST", None)
        self.assertEqual(response.status_code, 401)

    async def test_an_unreachable_swarm_is_a_retryable_503_not_a_401(self) -> None:
        # A 401 here would tell the operator to run refresh-gateway, which during the
        # same outage also fails — and would rewrite every agent config with a token the
        # swarm never recorded. 503 is truthful and makes the agent back off.
        transient = state.StateUnavailable("swarm down")
        with patch.object(
            service, "user_for_proxy_token", new=AsyncMock(side_effect=transient)
        ):
            response = await mcp_proxy._proxy(_make_request(), "POST", "tok-ffffffffffff")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.body)["error"], "composio_state_unavailable"
        )
        self.assertEqual(response.headers["retry-after"], "30")

    async def test_session_build_failure_is_a_502(self) -> None:
        token = service.proxy_token_for_user(PRINCIPAL)
        with patch.object(
            service, "build_mcp_server_entry", side_effect=RuntimeError("no session")
        ):
            response = await mcp_proxy._proxy(_make_request(), "POST", token)
        self.assertEqual(response.status_code, 502)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "composio_session_unavailable")
        self.assertIn("no session", body["detail"])

    async def test_entry_without_a_url_is_a_502(self) -> None:
        token = service.proxy_token_for_user(PRINCIPAL)
        with patch.object(
            service, "build_mcp_server_entry", return_value={"type": "http"}
        ):
            response = await mcp_proxy._proxy(_make_request(), "POST", token)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["detail"], "no upstream url")

    async def test_unreachable_upstream_is_a_502(self) -> None:
        token = service.proxy_token_for_user(PRINCIPAL)

        class _FailingClient:
            def __init__(self, *a, **kw) -> None:
                pass

            def build_request(self, *a, **kw):
                return object()

            async def send(self, *a, **kw):
                raise httpx.RequestError("connection refused")

            async def aclose(self) -> None:
                pass

        with patch.object(
            service, "build_mcp_server_entry",
            return_value={"type": "http", "url": "https://mcp.example/s"},
        ), patch.object(mcp_proxy.httpx, "AsyncClient", _FailingClient):
            response = await mcp_proxy._proxy(_make_request(), "POST", token)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["error"], "composio_unreachable")

    def test_client_credentials_are_never_forwarded_upstream(self) -> None:
        forwarded = mcp_proxy._forwarded_headers(
            {
                "authorization": "Bearer client-secret",
                "host": "127.0.0.1:5002",
                "content-length": "12",
                "accept": "application/json",
            },
            {"x-api-key": "server-side-key"},
        )
        self.assertNotIn("authorization", forwarded)
        self.assertNotIn("host", forwarded)
        self.assertNotIn("content-length", forwarded)
        self.assertEqual(forwarded["accept"], "application/json")
        self.assertEqual(forwarded["x-api-key"], "server-side-key")

    def test_injected_headers_win_over_client_supplied_ones(self) -> None:
        forwarded = mcp_proxy._forwarded_headers(
            {"x-api-key": "client-attempt"}, {"x-api-key": "server-side-key"},
        )
        self.assertEqual(forwarded["x-api-key"], "server-side-key")


class CallbackHtmlTests(_ComposioBase):
    def test_payload_cannot_close_the_script_element(self) -> None:
        html_out = router_mod._callback_html(
            {
                "type": "connector-auth-error",
                "error": "</script><img src=x onerror=alert(1)>",
            },
            ok=False,
        )
        # The only literal </script> is the real closer; the payload's is escaped.
        self.assertEqual(html_out.count("</script>"), 1)
        self.assertIn("\\u003c", html_out)
        self.assertNotIn("<img src=x", html_out)

    def test_error_text_is_html_escaped_in_the_body(self) -> None:
        html_out = router_mod._callback_html(
            {"type": "connector-auth-error", "error": "<b>boom</b>"}, ok=False,
        )
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", html_out)


class RouterTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    async def test_toolkits_default_to_needs_auth(self) -> None:
        with patch.object(service, "list_connections", return_value=[]):
            response = await router_mod.list_toolkits(user_id=PRINCIPAL)
        toolkits = json.loads(response.body)["toolkits"]
        self.assertEqual(len(toolkits), len(service.TOOLKITS))
        self.assertTrue(all(t["status"] == "NEEDS_AUTH" for t in toolkits))
        self.assertTrue(all(t["supports_action_prefs"] for t in toolkits))

    async def test_connected_toolkit_reports_its_account(self) -> None:
        rows = [{
            "toolkit": "GMAIL", "connected_account_id": "ca_1",
            "status": "ACTIVE", "scheme": "OAUTH2",
        }]
        with patch.object(service, "list_connections", return_value=rows):
            response = await router_mod.list_toolkits(user_id=PRINCIPAL)
        gmail = next(
            t for t in json.loads(response.body)["toolkits"] if t["id"] == "gmail"
        )
        self.assertEqual(gmail["status"], "ACTIVE")
        self.assertEqual(gmail["connected_account_id"], "ca_1")

    def test_an_active_account_outranks_a_stale_duplicate(self) -> None:
        rows = [
            {"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE"},
            {"toolkit": "GMAIL", "connected_account_id": "ca_2", "status": "EXPIRED"},
        ]
        with patch.object(service, "list_connections", return_value=rows):
            by_slug = router_mod._toolkit_status_map(PRINCIPAL)
        self.assertEqual(by_slug["GMAIL"]["connected_account_id"], "ca_1")

    async def test_unconfigured_toolkit_is_a_422_not_a_500(self) -> None:
        body = router_mod.ConnectBody()
        with patch.dict(os.environ, {"COMPOSIO_AUTH_CONFIG_NOTION": ""}):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("notion", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("COMPOSIO_AUTH_CONFIG_NOTION", raised.exception.detail)

    async def test_an_unreachable_swarm_still_yields_a_422_on_connect(self) -> None:
        # Pins the DEVELOPING.md §10.3 degradation contract end to end now that the
        # credentials come over the wire: a swarm outage must land on the same 422
        # shape as a missing key, carrying a detail the Connectors tab can match.
        body = router_mod.ConnectBody()
        with patch.dict(os.environ, {"COMPOSIO_CREDENTIALS_SOURCE": "swarm"}), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(credentials, "_get",
                             side_effect=httpx.ConnectError("refused")):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("notion", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("COMPOSIO_API_KEY", raised.exception.detail)

    async def test_disconnecting_an_account_you_do_not_own_is_a_404(self) -> None:
        body = router_mod.DisconnectBody(connected_account_id="ca_someone_else")
        with patch.object(service, "list_connections", return_value=[]):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.disconnect("gmail", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_failed_disconnect_is_a_502(self) -> None:
        rows = [{"connected_account_id": "ca_1", "toolkit": "GMAIL", "status": "ACTIVE"}]
        body = router_mod.DisconnectBody(connected_account_id="ca_1")
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "disconnect", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.disconnect("gmail", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 502)

    async def test_unknown_toolkit_tools_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await router_mod.list_toolkit_tools("nosuch", user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_prefs_for_an_unclassified_toolkit_are_a_404(self) -> None:
        body = router_mod.PrefsBody(actions={"X": False})
        with patch.object(categories, "classified_toolkits", return_value=frozenset()):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_toolkit_prefs("gmail", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_prefs_round_trip_through_the_router(self) -> None:
        body = router_mod.PrefsBody(actions={"GMAIL_SEND_EMAIL": False})
        with patch.object(service, "sync_session"):
            await router_mod.put_toolkit_prefs("gmail", body, user_id=PRINCIPAL)
            response = await router_mod.get_toolkit_prefs("gmail", user_id=PRINCIPAL)
        self.assertEqual(
            json.loads(response.body)["actions"], {"GMAIL_SEND_EMAIL": False}
        )

    async def test_refresh_gateway_rejects_an_unsupported_agent(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.refresh_gateway(agent="nosuch", user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("claude_code", raised.exception.detail)

    async def test_successful_refresh_warns_that_the_config_is_machine_global(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch.object(service, "install_into_gateway", return_value={"ok": True}):
            response = await router_mod.refresh_gateway(
                agent="claude_code", user_id=PRINCIPAL
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("multi_tenant_warning", json.loads(response.body))

    async def test_failed_refresh_is_reported_as_422(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch.object(
                    service, "install_into_gateway",
                    return_value={"ok": False, "error": "no config file"},
                ):
            response = await router_mod.refresh_gateway(
                agent="claude_code", user_id=PRINCIPAL
            )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("multi_tenant_warning", json.loads(response.body))

    async def test_toolkits_report_the_account_count_and_multi_account_state(self) -> None:
        rows = [
            {"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE",
             "alias": "work", "created_at": "2026-06-01T00:00:00Z"},
            {"toolkit": "GMAIL", "connected_account_id": "ca_2", "status": "ACTIVE",
             "alias": "personal", "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(service, "list_connections", return_value=rows):
            response = await router_mod.list_toolkits(user_id=PRINCIPAL)
        body = json.loads(response.body)
        gmail = next(t for t in body["toolkits"] if t["id"] == "gmail")
        self.assertEqual(gmail["account_count"], 2)
        # Newest first, so the primary shown on the card is the newer account.
        self.assertEqual(gmail["alias"], "work")
        self.assertFalse(body["multi_account"]["enable"])

    async def test_accounts_route_marks_the_default_and_the_pinned_ones(self) -> None:
        rows = [
            {"toolkit": "GMAIL", "connected_account_id": "ca_new", "status": "ACTIVE",
             "alias": "work", "created_at": "2026-06-01T00:00:00Z"},
            {"toolkit": "GMAIL", "connected_account_id": "ca_old", "status": "ACTIVE",
             "alias": None, "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(service, "list_connections", return_value=rows):
            response = await router_mod.list_toolkit_accounts(
                "gmail", user_id=PRINCIPAL
            )
        accounts = json.loads(response.body)["accounts"]
        self.assertEqual(
            [a["connected_account_id"] for a in accounts], ["ca_new", "ca_old"]
        )
        # Multi-account is off here, so only the newest reaches the session.
        self.assertEqual([a["pinned"] for a in accounts], [True, False])
        self.assertEqual([a["is_default"] for a in accounts], [True, False])

    async def test_accounts_route_rejects_an_unknown_toolkit(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await router_mod.list_toolkit_accounts("nosuch", user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_connect_with_a_taken_alias_is_a_409_not_a_422(self) -> None:
        body = router_mod.ConnectBody(alias="work", allow_multiple=True)
        with patch.object(
            service, "initiate_connection",
            side_effect=service.AliasInUseError("taken by ca_1"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("gmail", body, user_id=PRINCIPAL)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_alias_on_an_account_you_do_not_own_is_a_404(self) -> None:
        body = router_mod.AliasBody(alias="work")
        with patch.object(service, "list_connections", return_value=[]):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_account_alias(
                    "gmail", "ca_someone_else", body, user_id=PRINCIPAL,
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_alias_collision_through_the_router_is_a_409(self) -> None:
        rows = [
            {"toolkit": "GMAIL", "connected_account_id": "ca_1", "alias": None,
             "status": "ACTIVE"},
            {"toolkit": "GMAIL", "connected_account_id": "ca_2", "alias": "work",
             "status": "ACTIVE"},
        ]
        body = router_mod.AliasBody(alias="work")
        with patch.object(service, "list_connections", return_value=rows):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_account_alias(
                    "gmail", "ca_1", body, user_id=PRINCIPAL,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("ca_2", raised.exception.detail)

    async def test_alias_write_failure_is_a_502(self) -> None:
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "alias": None, "status": "ACTIVE"}]
        body = router_mod.AliasBody(alias="work")
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(
                    service, "set_alias", side_effect=RuntimeError("composio said no")
                ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_account_alias(
                    "gmail", "ca_1", body, user_id=PRINCIPAL,
                )
        self.assertEqual(raised.exception.status_code, 502)

    async def test_clearing_an_alias_re_syncs_the_session(self) -> None:
        # The alias is resolved inside the session, so a rename the session has
        # not seen would leave the agent naming an account that does not exist.
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "alias": "work", "status": "ACTIVE"}]
        body = router_mod.AliasBody(alias=None)
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "set_alias", return_value=None) as set_alias, \
                patch.object(service, "sync_session") as sync:
            response = await router_mod.put_account_alias(
                "gmail", "ca_1", body, user_id=PRINCIPAL,
            )
        set_alias.assert_called_once_with("ca_1", None)
        sync.assert_called_once_with(PRINCIPAL)
        self.assertIsNone(json.loads(response.body)["alias"])

    async def test_callback_reports_provider_failure_as_400(self) -> None:
        response = await router_mod.composio_callback(
            toolkit="gmail", status=None, error="access_denied", error_description=None,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("connector-auth-error", response.body.decode())

    async def test_callback_success_posts_the_completion_message(self) -> None:
        response = await router_mod.composio_callback(
            toolkit="gmail", status=None, error=None, error_description=None,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("connector-auth-complete", response.body.decode())


class GatewayBootstrapTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """Every branch fails closed: nothing installed beats the wrong tenant."""

    async def test_no_capable_agent_installs_nothing(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=[]):
            self.assertEqual(await service.install_gateways_at_startup(), {})

    async def test_no_credential_installs_nothing(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("services.xo_credential.get_auth_token", return_value=""):
            self.assertEqual(await service.install_gateways_at_startup(), {})

    async def test_an_unreachable_swarm_installs_nothing(self) -> None:
        state.invalidate()
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=RuntimeError("network down"),
                ):
            self.assertEqual(await service.install_gateways_at_startup(), {})

    async def test_rejected_credential_installs_nothing(self) -> None:
        rejected = state.StateUnavailable("XO rejected it", authoritative=True)
        state.invalidate()
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=rejected):
            self.assertEqual(await service.install_gateways_at_startup(), {})

    async def test_missing_workspace_installs_nothing(self) -> None:
        with patch.dict(os.environ, {tenancy.WORKSPACE_ENV: ""}), \
                patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("services.xo_credential.get_auth_token", return_value="tok"):
            self.assertEqual(await service.install_gateways_at_startup(), {})

    async def test_a_failing_agent_does_not_stop_the_others(self) -> None:
        def _install(_principal: str, agent: str) -> dict:
            if agent == "hermes":
                raise RuntimeError("no config file")
            return {"ok": True, "config_path": "/tmp/x"}

        with patch.object(
            service, "gateway_install_agents", return_value=["claude_code", "hermes"]
        ), patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(service, "install_into_gateway", side_effect=_install):
            results = await service.install_gateways_at_startup()

        self.assertTrue(results["claude_code"]["ok"])
        self.assertFalse(results["hermes"]["ok"])
        self.assertIn("RuntimeError", results["hermes"]["error"])

    async def test_install_receives_this_pod_s_principal(self) -> None:
        seen: list[str] = []

        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("services.xo_credential.get_auth_token", return_value="tok"), \
                patch.object(
                    service, "install_into_gateway",
                    side_effect=lambda p, a: seen.append(p) or {"ok": True},
                ):
            await service.install_gateways_at_startup()

        self.assertEqual(seen, [PRINCIPAL])


if __name__ == "__main__":
    unittest.main()
