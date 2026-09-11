"""Tests for the Composio connector subpackage.

One TestCase per source module, per the suite convention. There is no conftest,
so isolation is explicit: `_ComposioBase.setUp` resets the module-level caches and
redirects every on-disk store into a temp dir.

Three traps this file works around, all easy to reintroduce:

- `service._write_store` and `action_prefs.bulk_set` take a lock via
  `visualizer.flock.locked`, which places its sentinel under
  `quirq_state_dir()/watcher/locks/` — the developer's real `~/.quirq` unless
  `QUIRQ_STATE_ROOT` points elsewhere. It is patched below.
- `identity._TOKEN_TTL_SECONDS` and `session_identity._SESSION_TTL` are evaluated
  at import, so `patch.dict(os.environ, ...)` cannot move them. Patch the
  attributes instead. Everything in `service.py` reads env at call time.
- Both stores migrate themselves out of the old in-checkout `data/` location on
  first access. Redirecting the store path alone is NOT enough: the legacy tuples
  would still point at the developer's real `data/composio_sessions.json`, and the
  first read in the suite would `shutil.move` their live proxy tokens into a temp
  dir that is then deleted. The tuples are emptied below; `MigrationTests` is the
  only place they hold (temp) paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import secrets
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from routers.cowork_agent.connectors import composio as router_mod
from routers.cowork_agent.connectors import composio_mcp_proxy as mcp_proxy
from services.cowork_agent.connectors.composio import action_prefs, categories
from services.cowork_agent.connectors.composio import identity as identity_mod
from services.cowork_agent.connectors.composio import paths
from services.cowork_agent.connectors.composio import service, session_identity, state
from services.cowork_agent.connectors.composio import workspace_scope
from services.swarm_api import composio as swarm_client

WORKSPACE = "ws-test"
ACCOUNT = "user_abc123"
# The retired tenant key, kept as a fixture standing in for "some user_id that is not
# ours". A literal, not composed: nothing in either repo composes this any more.
LEGACY_PRINCIPAL = "user_abc123__ws__ws-test"
PROXY_URL = "http://127.0.0.1:5002/mcp/composio-proxy/u/tok-test"


def _enable(toolkit: str = "gmail", *accounts: str) -> None:
    """Turn a toolkit on for this workspace, since nothing is enabled by default.

    Connections are account-wide but reach is not: a workspace opts in. Most tests below
    care about something else and just need a session to be mintable.
    """
    workspace_scope.set_toolkit(
        toolkit, enabled=True, connected_account_ids=list(accounts) or None,
        max_accounts=max(len(accounts), 1),
    )


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
        self.scope_path = tmp / "data" / "composio_workspace_scope.json"

        env = patch.dict(
            os.environ,
            {
                state.WORKSPACE_ENV: WORKSPACE,
                "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
                # Required with no default since the loopback fallback was
                # dropped, and patch.dict does not clear the ambient env — pinned
                # here so a developer's .env cannot decide whether these pass.
                "COMPOSIO_CALLBACK_URL": (
                    "https://test.example/api/connectors/composio/callback"
                ),
            },
        )
        env.start()
        self.addCleanup(env.stop)

        for patcher in (
            patch.object(service, "_SESSIONS_PATH", self.sessions_path),
            patch.object(action_prefs, "_store_path", return_value=self.prefs_path),
            patch.object(workspace_scope, "_store_path", return_value=self.scope_path),
            # Without these two, migration would move the developer's REAL
            # data/composio_*.json into this temp dir and delete it on cleanup —
            # see the third trap in the module docstring.
            patch.object(service, "_LEGACY_SESSIONS_PATHS", ()),
            patch.object(action_prefs, "_LEGACY_PREFS_PATHS", ()),
            # The developer's real XO_API_KEY is in this shell, and the tenant-state
            # client and the account-mismatch guard both reach for it. Without this the
            # suite would make live calls to xo-swarm-api. Tests that exercise those
            # paths patch get_auth_token themselves.
            patch("routers.auth.auth.XO_API_KEY", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self._reset_caches()
        self.addCleanup(self._reset_caches)
        # The account id now comes from xo-swarm-api. Seed the fetched-value cache so
        # the suite stays hermetic; the tests that exercise the fetch itself call
        # state.invalidate() first and patch the transport.
        _now = time.monotonic()
        state._IDENTITY = (
            ACCOUNT, _now + 3600, _now,
            {
                "account_id": ACCOUNT,
                "workspace_id": WORKSPACE,
            },
        )
        state.adopt_account_id(ACCOUNT)

    @staticmethod
    def _reset_caches() -> None:
        state.invalidate()
        service._SESSION_ID = None
        service._session_mcp_cache = None
        service._STORE_ACCOUNT = None
        service._PROXY_TOKENS.clear()
        service._ORPHANED_SESSION_IDS.clear()
        service._SESSIONS_LOADED = False
        # The reconcile sweep's single-flight state. The lock binds to the loop that
        # first contends it, and IsolatedAsyncioTestCase gives every test a new loop.
        service._SWEEP_LOCK = None
        service._SWEEP_TASK = None
        service._LAST_SWEEP_AT = 0.0
        service._LAST_ERRORS.clear()
        session_identity._SESSIONS.clear()


class ToolkitRegistryTests(_ComposioBase):
    def test_unknown_toolkit_is_rejected_by_name(self) -> None:
        with self.assertRaises(ValueError) as raised:
            service.toolkit_meta("nosuch")
        self.assertIn("nosuch", str(raised.exception))

    def test_toolkit_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(service.toolkit_meta("GMAIL").slug, "GMAIL")

    def test_unsupported_scheme_is_a_value_error_before_any_network_call(self) -> None:
        # Auth-config resolution moved to xo-swarm-api entirely, but the toolkit/scheme
        # check still fails fast, locally, before swarm_client is ever touched.
        with patch.object(swarm_client, "connect") as connect:
            with self.assertRaises(ValueError):
                service.initiate_connection(ACCOUNT, "notion", auth_scheme="API_KEY")
        connect.assert_not_called()

    def test_every_registered_toolkit_has_action_categories(self) -> None:
        # Pins the `supports_action_prefs` flag the /toolkits route emits: a
        # toolkit added to one table and not the other silently loses prefs.
        self.assertEqual(categories.classified_toolkits(), frozenset(service.TOOLKITS))


class AccountIdentityTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """Fetching this pod's Composio user id from xo-swarm-api.

    Composio is addressed by the bare account id now. This repo asserts it passes the
    string through untouched and never composes an identity of its own.
    """

    def test_the_local_composer_has_not_come_back(self) -> None:
        # `alegacy_principal` joins the list now that the migration probe is retired:
        # its return would mean the swarm had started composing the key again.
        for gone in ("SEPARATOR", "scoped_principal", "is_scoped", "aprincipal",
                     "alegacy_principal"):
            self.assertFalse(
                hasattr(state, gone),
                f"state.{gone} is back — workspaces are separated by Composio session "
                "config now, not by carving the user_id namespace.",
            )
        self.assertFalse(
            hasattr(service, "legacy_connections"),
            "service.legacy_connections is back. Nothing may read the retired "
            "workspace-scoped user id; a connection under it is unreachable from an "
            "account-scoped session by construction.",
        )

    async def test_the_account_id_is_passed_through_byte_for_byte(self) -> None:
        # No strip, no case folding, no normalisation: Composio stores these bytes
        # against every connected account.
        weird = "user_AbC123-_9"
        state.invalidate()
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", return_value={"account_id": weird}):
            self.assertEqual(await state.aaccount_id(), weird)

    async def test_an_extra_field_from_an_older_swarm_is_ignored(self) -> None:
        # A swarm that has not been redeployed still ships `legacy_principal`. It must be
        # inert: the user id is the account id and nothing else reads the payload.
        state.invalidate()
        payload = {"account_id": ACCOUNT, "legacy_principal": LEGACY_PRINCIPAL}
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", return_value=payload):
            self.assertEqual(await state.aaccount_id(), ACCOUNT)

    async def test_it_is_fetched_once_and_cached(self) -> None:
        state.invalidate()
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", return_value={"account_id": ACCOUNT}
                ) as request:
            self.assertEqual(await state.aaccount_id(), ACCOUNT)
            self.assertEqual(await state.aaccount_id(), ACCOUNT)
        self.assertEqual(request.call_count, 1)

    async def test_an_unreachable_swarm_falls_back_to_the_store_owner(self) -> None:
        # A pod that booted once knows whose rows it holds, so it rides out an outage.
        state.invalidate()
        state.adopt_account_id(ACCOUNT)
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=state.StateUnavailable("down")
                ):
            self.assertEqual(await state.aaccount_id(), ACCOUNT)

    async def test_a_revoked_credential_does_not_fall_back_to_the_store(self) -> None:
        # Authoritative means XO said no. A revoked key must stop working, not linger.
        state.invalidate()
        state.adopt_account_id(ACCOUNT)
        rejected = state.StateUnavailable("rejected", authoritative=True)
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=rejected):
            with self.assertRaises(state.StateUnavailable):
                await state.aaccount_id()

    async def test_a_swarm_without_the_route_falls_back_to_the_store(self) -> None:
        # 404 here is a deploy-ordering slip, not a refusal — it must not take Composio
        # down when this pod's own store already names its owner.
        state.invalidate()
        state.adopt_account_id(ACCOUNT)
        missing = state.StateUnavailable("nf", authoritative=True, not_found=True)
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=missing):
            with self.assertLogs(state.log, level="ERROR"):
                self.assertEqual(await state.aaccount_id(), ACCOUNT)


class ProxyTokenTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    def test_an_unstamped_store_is_never_written(self) -> None:
        # Without a workspace id the document could not be told apart from one restored
        # out of another workspace, so it must not be written at all.
        with patch.dict(os.environ, {state.WORKSPACE_ENV: ""}):
            service.proxy_token()
        self.assertFalse(self.sessions_path.exists())

    def test_token_is_stable_across_calls(self) -> None:
        first = service.proxy_token()
        second = service.proxy_token()
        self.assertEqual(first, second)

    async def test_token_survives_a_process_restart(self) -> None:
        token = service.proxy_token()
        self._reset_caches()
        self.assertEqual(await service.account_for_proxy_token(token), ACCOUNT)

    def _write_store(self, doc: dict) -> None:
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(json.dumps(doc), encoding="utf-8")
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()
        service._SESSION_ID = None
        service._STORE_ACCOUNT = None

    async def test_a_pre_v4_document_is_discarded_not_upgraded(self) -> None:
        # v3 rows are keyed by the retired `<account>__ws__<workspace>` tenant key and
        # their sessions were minted against it, so every one addresses a Composio user
        # that is no longer ours.
        self._write_store({
            "version": 3,
            "principal": LEGACY_PRINCIPAL,
            "sessions": {LEGACY_PRINCIPAL: "trs_old"},
            "proxy_tokens": {"legacy-token": LEGACY_PRINCIPAL},
        })
        self.assertIsNone(service.account_for_proxy_token_local("legacy-token"))

    async def test_a_discarded_store_s_session_is_queued_for_deletion(self) -> None:
        # Composio sessions never expire, so an abandoned one lingers server-side
        # forever unless something deletes it.
        self._write_store({
            "version": 3,
            "principal": LEGACY_PRINCIPAL,
            "sessions": {LEGACY_PRINCIPAL: "trs_old"},
            "proxy_tokens": {},
        })
        service.account_for_proxy_token_local("anything")
        self.assertIn("trs_old", service._ORPHANED_SESSION_IDS)

        deleted: list[str] = []
        with patch.object(swarm_client, "delete_session", side_effect=deleted.append):
            self.assertEqual(service.drain_orphaned_sessions(), 1)
        self.assertEqual(deleted, ["trs_old"])
        # Drained, not retried forever — a session that cannot be deleted must not
        # re-block every boot.
        self.assertEqual(service.drain_orphaned_sessions(), 0)

    async def test_another_workspace_s_store_is_not_adopted(self) -> None:
        # A correctly formed, current-version document — refused purely because it was
        # stamped by a sibling workspace. This is what a restored backup looks like, and
        # adopting it would mean inheriting that workspace's connector scope.
        self._write_store({
            "version": 4,
            "workspace_id": "ws-somewhere-else",
            "account_id": ACCOUNT,
            "session": "trs_theirs",
            "proxy_tokens": ["theirs"],
        })
        self.assertIsNone(service.account_for_proxy_token_local("theirs"))
        # Left on disk rather than deleted — it is somebody's data — but its session is
        # still queued for cleanup.
        self.assertTrue(self.sessions_path.exists())
        self.assertIn("trs_theirs", service._ORPHANED_SESSION_IDS)

    async def test_this_workspace_s_own_store_is_adopted(self) -> None:
        self._write_store({
            "version": 4,
            "workspace_id": WORKSPACE,
            "account_id": ACCOUNT,
            "session": "trs_ours",
            "proxy_tokens": ["ours"],
        })
        self.assertEqual(service.account_for_proxy_token_local("ours"), ACCOUNT)

    async def test_empty_token_resolves_to_nobody(self) -> None:
        self.assertIsNone(await service.account_for_proxy_token(""))

    def test_minting_never_leaves_the_pod(self) -> None:
        # The token is local state: it is written to this pod's 0600 store and nowhere
        # else, so minting one must make no network call at all.
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request") as request:
            token = service.proxy_token()
        self.assertTrue(token)
        request.assert_not_called()

    async def test_a_local_hit_never_touches_the_network_with_a_cold_account(self) -> None:
        # Constraint, executable: the MCP proxy calls this on every tool call. A pod
        # that restarts during a swarm outage must still serve tokens it physically
        # holds — the store records the account and stamps the workspace, so classifying
        # it needs no network.
        token = service.proxy_token()
        service._SESSIONS_LOADED = False
        service._PROXY_TOKENS.clear()
        service._STORE_ACCOUNT = None
        state.invalidate()                     # account unknown; store still knows
        with patch.object(state, "_request") as request:
            self.assertEqual(await service.account_for_proxy_token(token), ACCOUNT)
        request.assert_not_called()

    async def test_a_local_hit_never_touches_the_network(self) -> None:
        # The hot path: the MCP proxy calls this on every tool call, so the steady
        # state must stay a dict lookup.
        token = service.proxy_token()
        with patch.object(state, "_request") as request:
            self.assertEqual(await service.account_for_proxy_token(token), ACCOUNT)
        request.assert_not_called()

    def test_store_is_written_private_stamped_and_versioned(self) -> None:
        token = service.proxy_token()
        self.assertEqual(
            stat.S_IMODE(self.sessions_path.stat().st_mode), 0o600
        )
        data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 4)
        # The stamp is what lets the pod tell its own store from a restored one, with
        # no network — and the account is what keeps token resolution offline.
        self.assertEqual(data["workspace_id"], WORKSPACE)
        self.assertEqual(data["account_id"], ACCOUNT)
        self.assertEqual(data["proxy_tokens"], [token])

    def test_proxy_url_carries_the_token_and_configured_port(self) -> None:
        with patch.dict(os.environ, {"PORT": "5010"}):
            url = service._composio_proxy_url()
        self.assertIn("http://127.0.0.1:5010/mcp/composio-proxy/u/", url)


class MigrationTests(_ComposioBase):
    """Both stores move themselves out of the old in-checkout `data/` location.

    The only place in this file where the legacy tuples are non-empty — and they point
    at a temp dir, never the real checkout. See the third trap in the module docstring.
    """

    def setUp(self) -> None:
        super().setUp()
        self.legacy_dir = Path(self._tmp.name) / "checkout" / "data"
        self.legacy_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_sessions = self.legacy_dir / "composio_sessions.json"
        self.legacy_prefs = self.legacy_dir / "composio_action_prefs.json"

    def _arm(self) -> None:
        """Point the legacy tuples at this test's temp checkout."""
        for patcher in (
            patch.object(service, "_LEGACY_SESSIONS_PATHS", (self.legacy_sessions,)),
            patch.object(action_prefs, "_LEGACY_PREFS_PATHS", (self.legacy_prefs,)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_legacy_store(self) -> dict:
        doc = {
            "version": 4,
            "workspace_id": WORKSPACE,
            "account_id": ACCOUNT,
            "session": "trs_legacy",
            "proxy_tokens": ["tok-from-the-checkout"],
        }
        self.legacy_sessions.write_text(json.dumps(doc), encoding="utf-8")
        return doc

    def test_a_store_left_in_the_checkout_is_moved_once(self) -> None:
        self._write_legacy_store()
        self._arm()

        workspace, account, session_id, tokens = service._load_store()

        self.assertEqual(workspace, WORKSPACE)
        self.assertEqual(account, ACCOUNT)
        self.assertEqual(session_id, "trs_legacy")
        self.assertEqual(tokens, {"tok-from-the-checkout"})
        self.assertTrue(self.sessions_path.exists())
        # Moved, not copied: a store left behind in the checkout is exactly the thing
        # this change exists to stop shipping around.
        self.assertFalse(self.legacy_sessions.exists())
        # It carries live proxy tokens, so the move must land it owner-only.
        self.assertEqual(stat.S_IMODE(self.sessions_path.stat().st_mode), 0o600)

    def test_a_store_already_in_place_is_not_clobbered(self) -> None:
        self._write_legacy_store()
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(
            json.dumps({
                "version": 4,
                "workspace_id": WORKSPACE,
                "account_id": ACCOUNT,
                "session": "trs_current",
                "proxy_tokens": [],
            }),
            encoding="utf-8",
        )
        self._arm()

        _ws, _account, session_id, _tokens = service._load_store()

        self.assertEqual(session_id, "trs_current")
        # The legacy file is left alone rather than deleted: nothing read it, so
        # nothing should destroy it either.
        self.assertTrue(self.legacy_sessions.exists())

    def test_a_migration_failure_is_not_fatal(self) -> None:
        self._write_legacy_store()
        self._arm()

        with patch.object(paths.shutil, "move", side_effect=OSError("read-only")):
            workspace, account, session_id, tokens = service._load_store()

        # The same degradation as a store that was never written — not a crash on the
        # MCP hot path, which runs this on every tools/call.
        self.assertEqual((workspace, account, session_id, tokens), (None, None, None, set()))
        self.assertFalse(self.sessions_path.exists())

    def test_prefs_migrate_through_the_patched_store_path(self) -> None:
        self.legacy_prefs.write_text(
            json.dumps({"version": 2, "users": {ACCOUNT: {"gmail": {"SEND": False}}}}),
            encoding="utf-8",
        )
        self._arm()

        self.assertEqual(action_prefs.load_prefs(), {"gmail": {"SEND": False}})
        self.assertTrue(self.prefs_path.exists())
        self.assertFalse(self.legacy_prefs.exists())


class ServiceDegradationTests(_ComposioBase):
    """The read paths swallow a transient swarm fault; an authoritative one (no key
    configured, credential rejected) still propagates. The MCP entry deliberately
    does not swallow anything."""

    def test_check_connection_passes_through_the_swarm_s_own_degraded_answer(self) -> None:
        # xo-swarm-api's own /connection-requests/{id} route already turns an internal
        # Composio failure into this FAILED shape; check_connection is a pure
        # passthrough now, so there is nothing left for it to catch.
        failed = {"status": "FAILED", "connected_account_id": None, "error": "composio is down"}
        with patch.object(swarm_client, "connection_status", return_value=failed):
            result = service.check_connection("cr_1")
        self.assertEqual(result, failed)

    def test_list_connections_degrades_to_empty_on_a_transient_failure(self) -> None:
        with patch.object(
            swarm_client, "list_connections",
            side_effect=swarm_client.SwarmComposioError("composio is down"),
        ):
            self.assertEqual(service.list_connections(ACCOUNT), [])

    def test_list_connections_propagates_an_authoritative_failure(self) -> None:
        # The security-critical case: a caller must never see "no connections" when
        # the real answer is "Composio is not configured" or "credential rejected".
        with patch.object(
            swarm_client, "list_connections",
            side_effect=swarm_client.SwarmComposioError("no key", authoritative=True),
        ):
            with self.assertRaises(swarm_client.SwarmComposioError):
                service.list_connections(ACCOUNT)

    def test_disconnect_reports_false_instead_of_raising(self) -> None:
        with patch.object(
            swarm_client, "disconnect",
            side_effect=swarm_client.SwarmComposioError("composio is down"),
        ):
            self.assertFalse(service.disconnect("ca_1"))

    def test_disconnect_not_owned_raises_value_error(self) -> None:
        # Ownership now lives on xo-swarm-api; a 404 from there is the only thing
        # that can tell "not yours" apart from "gone", and it must not be swallowed
        # into a plain False the way a generic failure is.
        with patch.object(
            swarm_client, "disconnect",
            side_effect=swarm_client.SwarmComposioNotFound("no such account"),
        ):
            with self.assertRaises(ValueError):
                service.disconnect("ca_1")

    def test_list_tools_degrades_to_empty(self) -> None:
        with patch.object(
            swarm_client, "list_tools",
            side_effect=swarm_client.SwarmComposioError("composio is down"),
        ):
            self.assertEqual(service.list_tools(ACCOUNT, "gmail"), [])

    def test_listing_many_tools_reads_the_prefs_once(self) -> None:
        # This ran per-tool, so a 200-tool toolkit meant 200 reads of the whole prefs
        # store. Tolerable against a local file; not once the store is remote.
        tools = [
            {"slug": f"GMAIL_ACTION_{i}", "name": "", "description": "", "parameters": {}}
            for i in range(200)
        ]
        with patch.object(swarm_client, "list_tools", return_value=tools), \
                patch.object(
                    action_prefs, "load_prefs", return_value={}
                ) as load_prefs:
            out = service.list_tools(ACCOUNT, "gmail")
        self.assertEqual(len(out), 200)
        self.assertEqual(load_prefs.call_count, 1)

    def test_disabled_actions_are_hidden_unless_explicitly_included(self) -> None:
        tools = [{
            "slug": "GMAIL_SEND_EMAIL", "name": "Send", "description": "", "parameters": {},
        }]
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False})
        with patch.object(swarm_client, "list_tools", return_value=tools):
            self.assertEqual(service.list_tools(ACCOUNT, "gmail"), [])
            shown = service.list_tools(ACCOUNT, "gmail", include_disabled=True)
        self.assertEqual(len(shown), 1)
        self.assertFalse(shown[0]["enabled"])

    def test_mcp_entry_refuses_a_session_with_no_url(self) -> None:
        # Without the guard the entry would carry the literal string "None", which
        # is truthy and fails much later as an opaque connection error.
        _enable("gmail")
        with patch.object(
            swarm_client, "create_session",
            return_value={"session_id": "s1", "mcp": {"url": None}},
        ):
            with self.assertRaises(RuntimeError) as raised:
                service.build_mcp_server_entry(ACCOUNT)
        self.assertIn("no MCP url", str(raised.exception))

    def test_mcp_entry_carries_url_and_headers(self) -> None:
        _enable("gmail")
        with patch.object(
            swarm_client, "create_session",
            return_value={
                "session_id": "sess_1",
                "mcp": {"url": "https://mcp.example/s", "headers": {"x-a": "b"}},
            },
        ):
            entry = service.build_mcp_server_entry(ACCOUNT)
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "https://mcp.example/s")
        self.assertEqual(entry["headers"], {"x-a": "b"})


def _row(cid, slug="gmail", *, status="ACTIVE", alias=None, created_at=None,
         is_disabled=False):
    """A connected-account row in the shape swarm_client.list_connections returns —
    already flat JSON, matching what xo-swarm-api's /connections route answers."""
    return {
        "toolkit": slug.upper(),
        "connected_account_id": cid,
        "status": status,
        "scheme": "OAUTH2",
        "alias": alias,
        "created_at": created_at,
        "is_disabled": is_disabled,
    }


class MultiAccountTests(_ComposioBase):
    """Several accounts of one toolkit per principal (aliases, pinning, session).

    Two independent switches, and the tests keep them apart: `allow_multiple` on
    /connect decides whether Composio *stores* a second account, while
    COMPOSIO_MULTI_ACCOUNT decides whether more than one of them can reach a
    session at the same time.
    """

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
        with patch.object(
            swarm_client, "list_connections",
            return_value=[_row("ca_1", alias="Work-Gmail")],
        ):
            with self.assertRaises(service.AliasInUseError) as raised:
                # Composio's uniqueness is per user and toolkit; casing must not
                # be a way around it.
                service.assert_alias_free(ACCOUNT, "gmail", "work-gmail")
        self.assertIn("ca_1", str(raised.exception))

    def test_renaming_an_account_to_its_own_alias_is_not_a_collision(self) -> None:
        with patch.object(
            swarm_client, "list_connections",
            return_value=[_row("ca_1", alias="work-gmail")],
        ):
            service.assert_alias_free(
                ACCOUNT, "gmail", "work-gmail", except_account_id="ca_1",
            )

    def test_alias_only_collides_within_the_same_toolkit(self) -> None:
        captured: dict = {}

        def _list(**kw):
            captured.update(kw)
            return []

        with patch.object(swarm_client, "list_connections", side_effect=_list):
            service.assert_alias_free(ACCOUNT, "notion", "shared-name")
        # toolkit_meta("notion").slug is uppercase; lowercasing for Composio now
        # happens on xo-swarm-api's side of this call, not here.
        self.assertEqual(captured["toolkit_slugs"], ["NOTION"])

    def test_set_alias_normalizes_before_calling_the_swarm(self) -> None:
        # Clearing sends None, not "" — turning that into the empty string
        # Composio's update() actually needs to clear an alias is now
        # xo-swarm-api's job (routes/composio_connections.py), not this
        # module's.
        seen: list[tuple[str, object]] = []
        with patch.object(
            swarm_client, "set_alias",
            side_effect=lambda cid, alias: seen.append((cid, alias)),
        ):
            self.assertIsNone(service.set_alias("ca_1", "  "))
            self.assertEqual(service.set_alias("ca_1", "  work  "), "work")
        self.assertEqual(seen, [("ca_1", None), ("ca_1", "work")])

    def test_failed_alias_write_raises_rather_than_reporting_success(self) -> None:
        with patch.object(
            swarm_client, "set_alias",
            side_effect=swarm_client.SwarmComposioError("composio is down"),
        ):
            with self.assertRaises(RuntimeError):
                service.set_alias("ca_1", "work")

    # ---- connecting a second account ----

    def test_connect_forwards_alias_and_allow_multiple(self) -> None:
        seen: list[dict] = []

        def _connect(toolkit_id, **kw):
            seen.append(kw)
            return {
                "auth_url": "https://composio.example/auth",
                "connection_request_id": "cr_1",
                "alias": kw.get("alias"),
            }

        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "connect", side_effect=_connect):
            result = service.initiate_connection(
                ACCOUNT, "gmail", alias=" work-gmail ", allow_multiple=True,
            )
        self.assertEqual(seen[0]["alias"], "work-gmail")
        self.assertTrue(seen[0]["allow_multiple"])
        self.assertEqual(result["alias"], "work-gmail")

    def test_a_plain_connect_sends_no_alias_or_allow_multiple(self) -> None:
        seen: list[dict] = []

        def _connect(toolkit_id, **kw):
            seen.append(kw)
            return {
                "auth_url": "https://composio.example/auth",
                "connection_request_id": "cr_1",
                "alias": None,
            }

        with patch.object(swarm_client, "connect", side_effect=_connect):
            service.initiate_connection(ACCOUNT, "gmail")
        self.assertIsNone(seen[0]["alias"])
        self.assertFalse(seen[0]["allow_multiple"])

    # ---- the callback url is required ----

    def test_a_missing_callback_url_raises_before_composio_is_called(self) -> None:
        # No loopback guess: minting an auth_url against a callback this
        # deployment does not own only fails later, in the popup.
        with patch.dict(os.environ, {"COMPOSIO_CALLBACK_URL": ""}), \
                patch.object(swarm_client, "connect") as connect:
            with self.assertRaises(RuntimeError) as raised:
                service.initiate_connection(ACCOUNT, "gmail")
        self.assertIn("COMPOSIO_CALLBACK_URL", str(raised.exception))
        connect.assert_not_called()

    def test_an_explicit_redirect_uri_does_not_need_the_env_var(self) -> None:
        seen: list[dict] = []

        def _connect(toolkit_id, **kw):
            seen.append(kw)
            return {
                "auth_url": "https://composio.example/auth",
                "connection_request_id": "cr_1",
                "alias": None,
            }

        with patch.dict(os.environ, {"COMPOSIO_CALLBACK_URL": ""}), \
                patch.object(swarm_client, "connect", side_effect=_connect):
            service.initiate_connection(
                ACCOUNT, "gmail", redirect_uri="https://caller.example/cb",
            )
        self.assertEqual(seen[0]["redirect_uri"], "https://caller.example/cb")

    # ---- listing ----

    def test_accounts_are_listed_newest_first(self) -> None:
        with patch.object(swarm_client, "list_connections", return_value=[
            _row("ca_old", created_at="2026-01-01T00:00:00Z"),
            _row("ca_new", created_at="2026-06-01T00:00:00Z"),
        ]):
            rows = service.list_toolkit_accounts(ACCOUNT, "gmail")
        self.assertEqual(
            [r["connected_account_id"] for r in rows], ["ca_new", "ca_old"]
        )

    def test_a_row_with_no_timestamp_sorts_last_instead_of_crashing(self) -> None:
        with patch.object(swarm_client, "list_connections", return_value=[
            _row("ca_undated"),
            _row("ca_dated", created_at="2026-01-01T00:00:00Z"),
        ]):
            rows = service.list_toolkit_accounts(ACCOUNT, "gmail")
        self.assertEqual(
            [r["connected_account_id"] for r in rows], ["ca_dated", "ca_undated"]
        )

    def test_foreign_toolkit_rows_are_dropped_even_if_the_api_ignores_the_filter(self) -> None:
        with patch.object(swarm_client, "list_connections", return_value=[
            _row("ca_1", "gmail"), _row("ca_2", "notion"),
        ]):
            rows = service.list_toolkit_accounts(ACCOUNT, "gmail")
        self.assertEqual([r["connected_account_id"] for r in rows], ["ca_1"])

    def test_alias_and_created_at_reach_the_caller(self) -> None:
        with patch.object(swarm_client, "list_connections", return_value=[
            _row("ca_1", alias="work", created_at="2026-01-01T00:00:00Z"),
        ]):
            row = service.list_connections(ACCOUNT)[0]
        self.assertEqual(row["alias"], "work")
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00Z")
        self.assertFalse(row["is_disabled"])

    # ---- pinning ----
    #
    # Pins are now the workspace's explicit choice, not a heuristic. The old behaviour —
    # "pin whatever is newest and active" — is precisely what this replaces: with
    # account-wide connections it would let a connect performed in a sibling workspace
    # silently repoint this one. workspace_scope.pins()/enabled_toolkits() are purely
    # local, so most of these need no swarm_client patch at all.

    def test_a_pin_is_the_workspace_s_choice_not_the_newest_account(self) -> None:
        _enable("gmail", "ca_old")
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_old"]})

    def test_an_unenabled_toolkit_is_never_pinned_even_when_connected(self) -> None:
        # The account holds a Gmail connection; this workspace has not opted in.
        self.assertEqual(workspace_scope.pins(), {})
        self.assertEqual(workspace_scope.enabled_toolkits(), [])

    def test_pinning_never_exceeds_the_configured_maximum(self) -> None:
        # Composio rejects a session pinning more than the cap, and that failure would
        # take every other toolkit down with it — so the excess is dropped here.
        workspace_scope.set_toolkit(
            "gmail", enabled=True,
            connected_account_ids=["ca_1", "ca_2", "ca_3"], max_accounts=2,
        )
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_1", "ca_2"]})

    def test_a_non_multi_account_session_pins_exactly_one(self) -> None:
        self.assertEqual(service.max_accounts_per_toolkit(), 1)
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}):
            self.assertEqual(service.max_accounts_per_toolkit(), 5)

    def test_a_deleted_account_is_pruned_rather_than_failing_the_session(self) -> None:
        # The connection was deleted from a sibling workspace, which cannot reach this
        # pod's store. One stale id fails the WHOLE session, so this must self-heal.
        _enable("gmail", "ca_gone")
        with patch.object(swarm_client, "list_connections", return_value=[_row("ca_live")]):
            self.assertTrue(service.prune_scope_to_live_accounts(ACCOUNT))
        self.assertEqual(workspace_scope.pins(), {})
        # And with nothing left pinned the toolkit goes off, rather than falling back
        # to Composio's most-recently-connected default.
        self.assertEqual(workspace_scope.enabled_toolkits(), [])

    def test_a_disabled_account_counts_as_gone_for_pruning(self) -> None:
        _enable("gmail", "ca_off")
        with patch.object(swarm_client, "list_connections", return_value=[
            _row("ca_off", is_disabled=True), _row("ca_on"),
        ]):
            service.prune_scope_to_live_accounts(ACCOUNT)
        self.assertEqual(workspace_scope.pins(), {})

    def test_pruning_leaves_a_healthy_scope_untouched(self) -> None:
        _enable("gmail", "ca_1")
        with patch.object(swarm_client, "list_connections", return_value=[_row("ca_1")]):
            self.assertFalse(service.prune_scope_to_live_accounts(ACCOUNT))
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_1"]})

    # ---- the session ----

    @staticmethod
    def _capture_create(seen: list[dict]):
        def _create(config):
            seen.append(config)
            return {
                "session_id": "sess_1",
                "mcp": {"url": "https://mcp.example/s", "headers": {}},
            }
        return _create

    def test_a_session_is_addressed_by_the_bare_account_id(self) -> None:
        # The whole point of the change: xo-swarm-api resolves user_id from this
        # backend's own bearer token, so the session config never carries one at
        # all any more — not even the bare account id, let alone a "__ws__" suffix.
        seen: list[dict] = []
        _enable("gmail")
        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "create_session", side_effect=self._capture_create(seen)):
            service.get_session(ACCOUNT)
        self.assertNotIn("user_id", seen[0])

    def test_a_session_carries_this_workspace_s_toolkit_allowlist(self) -> None:
        seen: list[dict] = []
        _enable("gmail")
        _enable("notion")
        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "create_session", side_effect=self._capture_create(seen)):
            service.get_session(ACCOUNT)
        # Composio checks the allowlist before it looks up a connection, so this is the
        # outer boundary of what the workspace can reach.
        self.assertEqual(seen[0]["toolkits"], {"enable": ["gmail", "notion"]})

    def test_a_session_carries_the_workspace_s_pins(self) -> None:
        seen: list[dict] = []
        _enable("gmail", "ca_chosen")
        with patch.object(swarm_client, "list_connections", return_value=[_row("ca_chosen")]), \
                patch.object(swarm_client, "create_session", side_effect=self._capture_create(seen)):
            service.get_session(ACCOUNT)
        self.assertEqual(seen[0]["connected_accounts"], {"gmail": ["ca_chosen"]})

    def test_a_workspace_with_nothing_enabled_gets_no_session_at_all(self) -> None:
        # Composio's behaviour for an empty allowlist is unspecified and "everything"
        # would be the catastrophic reading, so the session is never created.
        with patch.object(swarm_client, "create_session") as create:
            with self.assertRaises(service.NoToolkitsEnabled):
                service.get_session(ACCOUNT)
        create.assert_not_called()

    def test_session_creation_omits_multi_account_when_the_flag_is_off(self) -> None:
        seen: list[dict] = []
        _enable("gmail")
        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "create_session", side_effect=self._capture_create(seen)):
            service.get_session(ACCOUNT)
        self.assertNotIn("multi_account", seen[0])

    def test_session_creation_carries_the_multi_account_block(self) -> None:
        seen: list[dict] = []
        _enable("gmail")
        with patch.dict(os.environ, {"COMPOSIO_MULTI_ACCOUNT": "1"}), \
                patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "create_session", side_effect=self._capture_create(seen)):
            service.get_session(ACCOUNT)
        self.assertTrue(seen[0]["multi_account"]["enable"])

    def test_an_existing_session_converges_when_the_flag_is_turned_off(self) -> None:
        # multi_account is sent as None rather than omitted: a session minted
        # while the flag was on must stop being a multi-account session.
        seen: list[dict] = []

        def _update(sid, config):
            seen.append(config)
            return {"session_id": sid, "mcp": {"url": "https://mcp.example/s", "headers": {}}}

        _enable("gmail")
        service._SESSIONS_LOADED = True
        service._SESSION_ID = "sess_1"
        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(swarm_client, "update_session", side_effect=_update):
            service.sync_session(ACCOUNT)
        self.assertIsNone(seen[0]["multi_account"])
        self.assertEqual(seen[0]["toolkits"], {"enable": ["gmail"]})

    def test_a_rejected_session_update_falls_back_to_a_re_mint(self) -> None:
        _enable("gmail")
        service._SESSIONS_LOADED = True
        service._SESSION_ID = "sess_1"
        with patch.object(swarm_client, "list_connections", return_value=[]), \
                patch.object(
                    swarm_client, "update_session",
                    side_effect=swarm_client.SwarmComposioError("unsupported field"),
                ), \
                patch.object(swarm_client, "delete_session"):
            service.sync_session(ACCOUNT)
        self.assertIsNone(service._SESSION_ID)

    def test_disabling_the_last_toolkit_drops_the_session(self) -> None:
        # Leaving a live session behind would keep it reaching whatever it was last
        # configured with, which is exactly what turning everything off must prevent.
        _enable("gmail")
        service._SESSIONS_LOADED = True
        service._SESSION_ID = "sess_1"
        workspace_scope.set_toolkit("gmail", enabled=False)
        with patch.object(swarm_client, "delete_session"):
            service.sync_session(ACCOUNT)
        self.assertIsNone(service._SESSION_ID)


class ActionPrefsTests(_ComposioBase):
    def test_actions_are_enabled_by_default(self) -> None:
        # Only disabled slugs are stored, so absence is what "enabled" means.
        self.assertNotIn("GMAIL_ANY", action_prefs.disabled_slugs("gmail"))

    def test_only_disabled_actions_are_persisted(self) -> None:
        action_prefs.bulk_set(
            "gmail", {"GMAIL_SEND_EMAIL": False, "GMAIL_FETCH_EMAILS": True})
        stored = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["toolkits"]["gmail"], {"GMAIL_SEND_EMAIL": False})

    def test_re_enabling_the_last_action_prunes_the_toolkit(self) -> None:
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False})
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": True})
        stored = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["toolkits"], {})

    def test_prefs_are_this_pod_s_and_carry_no_user_level(self) -> None:
        # A pod is one workspace, so the v2 `users` map only ever held one row. v3
        # drops it; a document still carrying one would mean the key came back.
        action_prefs.bulk_set("gmail", {"GMAIL_SEND_EMAIL": False})
        stored = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertNotIn("users", stored)
        self.assertIn("GMAIL_SEND_EMAIL", action_prefs.disabled_slugs("gmail"))

    def test_a_v2_document_is_read_by_collapsing_its_users_map(self) -> None:
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(
            json.dumps({
                "version": 2,
                "users": {LEGACY_PRINCIPAL: {"gmail": {"GMAIL_SEND_EMAIL": False}}},
            }),
            encoding="utf-8",
        )
        # Losing these on the rename would silently re-enable actions a user had
        # deliberately switched off.
        self.assertIn("GMAIL_SEND_EMAIL", action_prefs.disabled_slugs("gmail"))

    def test_pre_v2_document_is_ignored_rather_than_misread(self) -> None:
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(
            json.dumps({"gmail": {"GMAIL_SEND_EMAIL": False}}), encoding="utf-8",
        )
        self.assertEqual(action_prefs.load_prefs(), {})


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
    """The xo-auth surface never mints a session for another account.

    ``POST /xo-auth/session`` minted a session for *another* account. Since xo-swarm-api
    composes the tenant key from the credential this backend presents, such a session
    would silently receive this backend's principal — and its Composio connections with
    it. It must not come back. The session pass-through the shipped UI calls lives alone
    in ``composio_session``; the browser-flow proxy (``routers/auth/auth.py``) holds no
    state of its own and only ever presents this backend's credential.
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

    def test_the_auth_router_never_mints_for_another_account(self) -> None:
        # xo-swarm-api owns authentication. routers/auth/auth.py proxies its browser
        # flow for this backend's own credential and must never mint a session from a
        # token the caller presents, nor shadow the UI's session/self pass-through.
        import routers.auth.auth as auth_mod

        self.assertFalse(hasattr(auth_mod, "xo_auth_session"))
        registered = {
            (method, route.path)
            for route in auth_mod.router.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("POST", "/xo-auth/session"), registered)
        self.assertNotIn(("GET", "/xo-auth/session/self"), registered)
        self.assertEqual(
            registered,
            {
                ("POST", "/xo-auth/start"),
                ("GET", "/xo-auth/status/{auth_session_id}"),
                ("POST", "/xo-auth/consume"),
                ("GET", "/xo-auth/whoami"),
                ("GET", "/xo-auth/state"),
                ("POST", "/xo-auth/logout"),
            },
        )

    def test_the_account_matching_guard_is_gone_with_it(self) -> None:
        # The guard existed only to refuse those sessions. Keeping it without the
        # endpoint would be dead code; removing the endpoint without it would be a
        # silent cross-account read.
        self.assertFalse(hasattr(identity_mod, "_account_matches_backend"))

    def test_the_refresh_gateway_route_is_gone(self) -> None:
        # The gateway is installed by the reconcile sweep alone — at boot, on a timer,
        # and when the Connectors tab loads. A manual route would be a second install
        # path with its own failure modes to document and a button to explain.
        self.assertFalse(hasattr(router_mod, "refresh_gateway"))
        registered = {route.path for route in router_mod.router.routes}
        self.assertNotIn("/api/connectors/composio/refresh-gateway", registered)
        self.assertFalse(hasattr(service, "install_gateways_at_startup"))


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

    async def test_a_missing_workspace_no_longer_refuses_the_request(self) -> None:
        # This gate used to 401 to avoid "falling back to an account-wide Composio
        # bucket". That bucket is now the intended design, so the check had inverted
        # from a protection into an outage. The workspace id still matters — it stamps
        # the session store — but that is the store's problem to report.
        sid = session_identity.remember(secrets.token_urlsafe(32))
        request = _make_request({"x-xo-session": sid})
        with patch.dict(os.environ, {state.WORKSPACE_ENV: ""}):
            self.assertEqual(
                await identity_mod.get_composio_user(request), ACCOUNT
            )

    async def test_an_unrecognised_session_is_a_401(self) -> None:
        request = _make_request({"x-xo-session": "sid-1"})
        with self.assertRaises(HTTPException) as raised:
            await identity_mod.get_composio_user(request)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("Invalid or expired session", raised.exception.detail)

    async def test_a_valid_session_yields_this_pod_s_account_id(self) -> None:
        sid = session_identity.remember(secrets.token_urlsafe(32))
        request = _make_request({"x-xo-session": sid})
        self.assertEqual(
            await identity_mod.resolve_user_from_bearer(request), ACCOUNT
        )

    async def test_an_unknown_session_yields_nothing(self) -> None:
        # The bearer is a gate now: an id this pod did not mint buys nothing.
        request = _make_request({"x-xo-session": "not-a-real-session"})
        self.assertIsNone(await identity_mod.resolve_user_from_bearer(request))

    async def test_an_unreachable_swarm_yields_nothing_rather_than_a_guess(self) -> None:
        # The soft paths (chat, /api/tools) read None as "run without Composio tools".
        state.invalidate()
        sid = session_identity.remember(secrets.token_urlsafe(32))
        request = _make_request({"x-xo-session": sid})
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=state.StateUnavailable("down")
                ):
            self.assertIsNone(await identity_mod.resolve_user_from_bearer(request))


class McpProxyTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    async def test_unknown_token_is_rejected_as_identity_required(self) -> None:
        response = await mcp_proxy._proxy(_make_request(), "POST", "no-such-token")
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "composio_identity_required")
        # There is no manual install to point at any more: the reconcile sweep
        # rewrites the config, so the remedy the agent is told is a restart.
        self.assertNotIn("refresh-gateway", body["detail"])
        self.assertIn("restart the agent", body["detail"])

    async def test_absent_token_is_rejected_the_same_way(self) -> None:
        response = await mcp_proxy._proxy(_make_request(), "POST", None)
        self.assertEqual(response.status_code, 401)

    async def test_resolution_makes_no_network_call(self) -> None:
        # Token ownership is answered from this pod's own store, so the proxy's hot path
        # cannot be taken down by an unreachable swarm — there is nothing to reach.
        token = service.proxy_token()
        with patch.object(state, "_request") as request, \
                patch.object(service, "build_mcp_server_entry", return_value={}):
            await mcp_proxy._proxy(_make_request(), "POST", token)
        request.assert_not_called()

    async def test_session_build_failure_is_a_502(self) -> None:
        token = service.proxy_token()
        with patch.object(
            service, "build_mcp_server_entry", side_effect=RuntimeError("no session")
        ):
            response = await mcp_proxy._proxy(_make_request(), "POST", token)
        self.assertEqual(response.status_code, 502)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "composio_session_unavailable")
        self.assertIn("no session", body["detail"])

    async def test_entry_without_a_url_is_a_502(self) -> None:
        token = service.proxy_token()
        with patch.object(
            service, "build_mcp_server_entry", return_value={"type": "http"}
        ):
            response = await mcp_proxy._proxy(_make_request(), "POST", token)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["detail"], "no upstream url")

    async def test_unreachable_upstream_is_a_502(self) -> None:
        token = service.proxy_token()

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
    def setUp(self) -> None:
        super().setUp()
        # /toolkits starts a background gateway sweep. Stubbed so no task outlives a
        # test's loop; test_listing_toolkits_kicks_a_gateway_sweep asserts the call.
        kick = patch.object(service, "kick_gateway_sweep", return_value=True)
        self.kick = kick.start()
        self.addCleanup(kick.stop)

    async def test_listing_toolkits_kicks_a_gateway_sweep(self) -> None:
        # Opening the Connectors tab (or pressing Refresh) is where the "Reinstall MCP
        # gateway" button used to be; the sweep now starts itself, without blocking.
        with patch.object(service, "list_connections", return_value=[]):
            await router_mod.list_toolkits(user_id=ACCOUNT)
        self.kick.assert_called_once_with()

    async def test_toolkits_default_to_needs_auth(self) -> None:
        with patch.object(service, "list_connections", return_value=[]):
            response = await router_mod.list_toolkits(user_id=ACCOUNT)
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
            response = await router_mod.list_toolkits(user_id=ACCOUNT)
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
        by_slug = router_mod._status_map_from_rows(rows)
        self.assertEqual(by_slug["GMAIL"]["connected_account_id"], "ca_1")

    async def test_unconfigured_toolkit_is_a_422_not_a_500(self) -> None:
        # Auth-config resolution now happens entirely on xo-swarm-api; this pins the
        # router's mapping of that RuntimeError to a 422 naming the missing env var.
        body = router_mod.ConnectBody()
        with patch.object(
            swarm_client, "connect",
            side_effect=swarm_client.SwarmComposioError(
                "Composio auth config for NOTION/OAUTH2 is not configured. Set "
                "COMPOSIO_AUTH_CONFIG_NOTION in xo-swarm-api's environment."
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("notion", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("COMPOSIO_AUTH_CONFIG_NOTION", raised.exception.detail)

    async def test_a_missing_callback_url_is_a_422_naming_the_var(self) -> None:
        # The Connectors tab matches the detail on COMPOSIO_CALLBACK_URL to tell
        # this apart from the missing-auth-config 422; keep the literal in it. This
        # check is local and fires before swarm_client.connect is ever called.
        body = router_mod.ConnectBody()
        with patch.dict(os.environ, {"COMPOSIO_CALLBACK_URL": ""}), \
                patch.object(swarm_client, "connect") as connect:
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("notion", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("COMPOSIO_CALLBACK_URL", raised.exception.detail)
        connect.assert_not_called()

    async def test_an_unreachable_swarm_still_yields_a_422_on_connect(self) -> None:
        # Pins the degradation contract end to end: whatever fails inside
        # xo-swarm-api's Composio call, initiate_connection surfaces it as a
        # RuntimeError and the router still lands on the same 422 shape, carrying a
        # detail the Connectors tab can match.
        body = router_mod.ConnectBody()
        with patch.object(
            swarm_client, "connect",
            side_effect=swarm_client.SwarmComposioError(
                "COMPOSIO_API_KEY could not be reached at .../connect: refused."
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("notion", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("COMPOSIO_API_KEY", raised.exception.detail)

    async def test_disconnecting_an_account_you_do_not_own_is_a_404(self) -> None:
        # Ownership is checked by xo-swarm-api now, not by a pre-list here.
        body = router_mod.DisconnectBody(connected_account_id="ca_someone_else")
        with patch.object(
            swarm_client, "disconnect",
            side_effect=swarm_client.SwarmComposioNotFound("no such account"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.disconnect("gmail", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_failed_disconnect_is_a_502(self) -> None:
        body = router_mod.DisconnectBody(connected_account_id="ca_1")
        with patch.object(service, "disconnect", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.disconnect("gmail", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 502)

    async def test_unknown_toolkit_tools_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await router_mod.list_toolkit_tools("nosuch", user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_prefs_for_an_unclassified_toolkit_are_a_404(self) -> None:
        body = router_mod.PrefsBody(actions={"X": False})
        with patch.object(categories, "classified_toolkits", return_value=frozenset()):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_toolkit_prefs("gmail", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_prefs_round_trip_through_the_router(self) -> None:
        body = router_mod.PrefsBody(actions={"GMAIL_SEND_EMAIL": False})
        with patch.object(service, "sync_session"):
            await router_mod.put_toolkit_prefs("gmail", body, user_id=ACCOUNT)
            response = await router_mod.get_toolkit_prefs("gmail", user_id=ACCOUNT)
        self.assertEqual(
            json.loads(response.body)["actions"], {"GMAIL_SEND_EMAIL": False}
        )

    async def test_toolkits_report_the_account_count_and_multi_account_state(self) -> None:
        rows = [
            {"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE",
             "alias": "work", "created_at": "2026-06-01T00:00:00Z"},
            {"toolkit": "GMAIL", "connected_account_id": "ca_2", "status": "ACTIVE",
             "alias": "personal", "created_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(service, "list_connections", return_value=rows):
            response = await router_mod.list_toolkits(user_id=ACCOUNT)
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
        # This workspace picked the OLDER account. The account list is account-wide,
        # so ca_new is visible here — but visible is not reachable, and the newest-wins
        # heuristic that used to decide this is gone.
        _enable("gmail", "ca_old")
        with patch.object(service, "list_connections", return_value=rows):
            response = await router_mod.list_toolkit_accounts(
                "gmail", user_id=ACCOUNT
            )
        body = json.loads(response.body)
        accounts = body["accounts"]
        self.assertEqual(
            [a["connected_account_id"] for a in accounts], ["ca_new", "ca_old"]
        )
        self.assertEqual([a["pinned"] for a in accounts], [False, True])
        self.assertEqual([a["is_default"] for a in accounts], [False, True])
        self.assertTrue(body["workspace_enabled"])

    async def test_accounts_route_rejects_an_unknown_toolkit(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await router_mod.list_toolkit_accounts("nosuch", user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_connect_with_a_taken_alias_is_a_409_not_a_422(self) -> None:
        body = router_mod.ConnectBody(alias="work", allow_multiple=True)
        with patch.object(
            service, "initiate_connection",
            side_effect=service.AliasInUseError("taken by ca_1"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.connect("gmail", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_alias_on_an_account_you_do_not_own_is_a_404(self) -> None:
        body = router_mod.AliasBody(alias="work")
        with patch.object(service, "list_connections", return_value=[]):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_account_alias(
                    "gmail", "ca_someone_else", body, user_id=ACCOUNT,
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
                    "gmail", "ca_1", body, user_id=ACCOUNT,
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
                    "gmail", "ca_1", body, user_id=ACCOUNT,
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
                "gmail", "ca_1", body, user_id=ACCOUNT,
            )
        set_alias.assert_called_once_with("ca_1", None)
        sync.assert_called_once_with(ACCOUNT)
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


class GatewaySweepTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """Every gate fails closed: nothing installed beats the wrong tenant. The sweep
    also reports which gate closed and whether waiting can open it, which is what the
    reconcile loop decides its next delay from."""

    async def test_no_capable_agent_installs_nothing(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=[]):
            sweep = await service.install_gateways()
        self.assertEqual(sweep.results, {})
        self.assertEqual(sweep.skipped, "no_agents")
        self.assertFalse(sweep.retryable)

    async def test_no_credential_installs_nothing_and_is_final(self) -> None:
        # The XO credential is XO_API_KEY or the session consumed at boot — it cannot
        # appear later in the process, so retrying would only burn round trips.
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value=""):
            sweep = await service.install_gateways()
        self.assertEqual(sweep.results, {})
        self.assertEqual(sweep.skipped, "no_credential")
        self.assertFalse(sweep.retryable)

    async def test_an_unreachable_swarm_installs_nothing_but_is_retryable(self) -> None:
        state.invalidate()
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    state, "_request", side_effect=RuntimeError("network down"),
                ):
            sweep = await service.install_gateways()
        self.assertEqual(sweep.results, {})
        self.assertEqual(sweep.skipped, "account_unavailable")
        self.assertTrue(sweep.retryable)

    async def test_rejected_credential_installs_nothing_and_is_final(self) -> None:
        rejected = state.StateUnavailable("XO rejected it", authoritative=True)
        state.invalidate()
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "_request", side_effect=rejected):
            sweep = await service.install_gateways()
        self.assertEqual(sweep.results, {})
        self.assertEqual(sweep.skipped, "account_unavailable")
        self.assertFalse(sweep.retryable)

    async def test_missing_workspace_installs_nothing_and_is_final(self) -> None:
        with patch.dict(os.environ, {state.WORKSPACE_ENV: ""}), \
                patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value="tok"):
            sweep = await service.install_gateways()
        self.assertEqual(sweep.results, {})
        self.assertEqual(sweep.skipped, "no_workspace")
        self.assertFalse(sweep.retryable)

    async def test_a_failing_agent_does_not_stop_the_others(self) -> None:
        def _install(agent: str, **_kw: object) -> dict:
            if agent == "hermes":
                raise RuntimeError("no config file")
            return {"ok": True, "config_path": "/tmp/x"}

        with patch.object(
            service, "gateway_install_agents", return_value=["claude_code", "hermes"]
        ), patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(service, "_composio_proxy_url", return_value=PROXY_URL), \
                patch.object(service, "install_into_gateway", side_effect=_install):
            sweep = await service.install_gateways()

        self.assertTrue(sweep.ran)
        self.assertTrue(sweep.results["claude_code"]["ok"])
        self.assertFalse(sweep.results["hermes"]["ok"])
        self.assertIn("RuntimeError", sweep.results["hermes"]["error"])

    async def test_every_agent_receives_one_proxy_url_minted_once(self) -> None:
        seen: list[tuple[str, object]] = []

        with patch.object(
            service, "gateway_install_agents", return_value=["claude_code", "codex"]
        ), patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(service, "_composio_proxy_url", return_value=PROXY_URL) as minted, \
                patch.object(
                    service, "install_into_gateway",
                    side_effect=lambda a, **kw: seen.append((a, kw.get("proxy_url")))
                    or {"ok": True},
                ):
            await service.install_gateways()

        self.assertEqual(seen, [("claude_code", PROXY_URL), ("codex", PROXY_URL)])
        # Minting reads and may rewrite the token store: once per sweep, not once
        # per agent. The URL carries no identity — only an opaque token.
        minted.assert_called_once_with()

    async def test_a_failed_mint_fails_every_agent_and_still_runs(self) -> None:
        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    service, "_composio_proxy_url", side_effect=ValueError("no user"),
                ), contextlib.redirect_stdout(io.StringIO()):
            sweep = await service.install_gateways()
        self.assertTrue(sweep.ran)
        self.assertFalse(sweep.results["claude_code"]["ok"])
        self.assertIn("no user", sweep.results["claude_code"]["error"])

    async def test_concurrent_sweeps_are_serialised(self) -> None:
        # The boot loop and a Connectors-tab kick share one lock, so two sweeps never
        # interleave their writes: the second starts only after the first finished.
        order: list[str] = []

        async def _slow_principal() -> str:
            order.append("start")
            await asyncio.sleep(0.01)
            order.append("end")
            return ACCOUNT

        with patch.object(service, "gateway_install_agents", return_value=["claude_code"]), \
                patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(state, "aaccount_id", side_effect=_slow_principal), \
                patch.object(service, "_composio_proxy_url", return_value=PROXY_URL), \
                patch.object(service, "install_into_gateway", return_value={"ok": True}):
            await asyncio.gather(service.install_gateways(), service.install_gateways())

        self.assertEqual(order, ["start", "end", "start", "end"])
        self.assertGreater(service._LAST_SWEEP_AT, 0.0)

    async def test_later_sweeps_print_only_changes(self) -> None:
        current = {"ok": True, "changed": False, "config_path": "/tmp/x"}
        patches = (
            patch.object(service, "gateway_install_agents", return_value=["claude_code"]),
            patch("routers.auth.auth.get_auth_token", return_value="tok"),
            patch.object(service, "_composio_proxy_url", return_value=PROXY_URL),
            patch.object(service, "install_into_gateway", return_value=current),
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            boot, later = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(boot):
                await service.install_gateways(announce=True)
            with contextlib.redirect_stdout(later):
                await service.install_gateways(announce=False)

        self.assertIn("already current", boot.getvalue())
        self.assertEqual(later.getvalue(), "")


class GatewayReconcileLoopTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    """The lifespan task: back off while XO is unreachable, stop on a gate a restart
    must open, then tick on a timer — and a page load may start a sweep, never a
    stampede."""

    _TRANSIENT = service.GatewaySweep(skipped="account_unavailable", retryable=True)
    _FINAL = service.GatewaySweep(skipped="no_credential")
    _RAN = service.GatewaySweep(results={"claude_code": {"ok": True}})

    async def _drive(self, outcomes, *, interval: str) -> tuple[list[float], int, list[bool]]:
        """Run the loop through ``outcomes``; returns (sleeps, outcomes left, announce flags).

        The loop either returns on its own or is cancelled when the outcomes run out.
        """
        delays: list[float] = []
        announced: list[bool] = []
        remaining = list(outcomes)

        async def _sweep(*, announce: bool = True) -> service.GatewaySweep:
            if not remaining:
                raise asyncio.CancelledError
            announced.append(announce)
            return remaining.pop(0)

        async def _sleep(delay: float) -> None:
            delays.append(delay)

        with patch.dict(os.environ, {"COMPOSIO_MCP_RECONCILE_INTERVAL": interval}), \
                patch.object(service, "install_gateways", side_effect=_sweep), \
                patch.object(service, "_sleep", side_effect=_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                await service.gateway_reconcile_loop()
            except asyncio.CancelledError:
                pass
        return delays, len(remaining), announced

    async def test_backs_off_while_the_swarm_is_unreachable(self) -> None:
        delays, _left, _ = await self._drive([self._TRANSIENT] * 8, interval="600")
        self.assertEqual(delays, [5, 15, 30, 60, 120, 300, 300, 300])

    async def test_a_final_gate_ends_the_loop_without_sleeping(self) -> None:
        delays, left, _ = await self._drive([self._FINAL, self._RAN], interval="600")
        self.assertEqual(delays, [])
        self.assertEqual(left, 1)   # returned; never asked for the second sweep

    async def test_a_successful_sweep_waits_the_reconcile_interval(self) -> None:
        delays, _left, _ = await self._drive([self._RAN, self._RAN], interval="45")
        self.assertEqual(delays, [45.0, 45.0])

    async def test_backoff_resets_after_a_successful_sweep(self) -> None:
        outcomes = [self._TRANSIENT, self._TRANSIENT, self._RAN, self._TRANSIENT]
        delays, _left, _ = await self._drive(outcomes, interval="600")
        self.assertEqual(delays, [5, 15, 600.0, 5])

    async def test_interval_zero_means_boot_only(self) -> None:
        delays, left, _ = await self._drive([self._RAN, self._RAN], interval="0")
        self.assertEqual(delays, [])
        self.assertEqual(left, 1)

    async def test_only_the_first_sweep_announces(self) -> None:
        _delays, _left, announced = await self._drive([self._RAN, self._RAN], interval="600")
        self.assertEqual(announced, [True, False])

    async def test_a_crashing_sweep_does_not_kill_the_timer(self) -> None:
        calls = 0

        async def _sweep(*, announce: bool = True) -> service.GatewaySweep:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("bug")
            raise asyncio.CancelledError

        delays: list[float] = []

        async def _sleep(delay: float) -> None:
            delays.append(delay)

        with patch.object(service, "install_gateways", side_effect=_sweep), \
                patch.object(service, "_sleep", side_effect=_sleep), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertLogs("services.cowork_agent.connectors.composio.service", "ERROR"):
            with contextlib.suppress(asyncio.CancelledError):
                await service.gateway_reconcile_loop()
        self.assertEqual(delays, [5])   # treated as transient; tried again

    def test_the_interval_knob_falls_back_on_garbage(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_MCP_RECONCILE_INTERVAL": "soon"}), \
                self.assertLogs("services.cowork_agent.connectors.composio.service", "WARNING"):
            self.assertEqual(service.reconcile_interval(), 600.0)
        with patch.dict(os.environ, {"COMPOSIO_MCP_RECONCILE_INTERVAL": ""}):
            self.assertEqual(service.reconcile_interval(), 600.0)
        with patch.dict(os.environ, {"COMPOSIO_MCP_RECONCILE_INTERVAL": "30"}):
            self.assertEqual(service.reconcile_interval(), 30.0)

    async def test_a_page_load_starts_one_sweep_not_a_stampede(self) -> None:
        started = 0
        release = asyncio.Event()

        async def _sweep(*, announce: bool = True) -> service.GatewaySweep:
            nonlocal started
            started += 1
            await release.wait()
            return self._FINAL

        with patch.object(service, "install_gateways", side_effect=_sweep):
            first = service.kick_gateway_sweep()
            second = service.kick_gateway_sweep()   # the first is still pending
            release.set()
            await service._SWEEP_TASK

        self.assertEqual((first, second), (True, False))
        self.assertEqual(started, 1)

    async def test_a_page_load_within_the_window_starts_nothing(self) -> None:
        with patch.object(service, "install_gateways", return_value=self._FINAL) as sweep:
            service._LAST_SWEEP_AT = time.monotonic()
            self.assertFalse(service.kick_gateway_sweep())
            service._LAST_SWEEP_AT = time.monotonic() - service._KICK_MIN_INTERVAL - 1
            self.assertTrue(service.kick_gateway_sweep())
            await service._SWEEP_TASK
        sweep.assert_called_once_with(announce=False)

    async def test_a_page_load_never_announces(self) -> None:
        # The console summary belongs to the boot pass; a sweep a tab started prints
        # only changes, otherwise every Refresh would re-list four agents.
        with patch.object(service, "install_gateways", return_value=self._FINAL) as sweep:
            self.assertTrue(service.kick_gateway_sweep())
            await service._SWEEP_TASK
        sweep.assert_called_once_with(announce=False)


class GatewayKickWithoutLoopTests(_ComposioBase):
    def test_a_kick_outside_the_event_loop_is_a_noop(self) -> None:
        with patch.object(service, "install_gateways") as sweep:
            self.assertFalse(service.kick_gateway_sweep())
        sweep.assert_not_called()


class WorkspaceScopeTests(_ComposioBase):
    """The per-workspace half of connector isolation.

    Connections are account-wide; this store is what keeps a workspace from reaching
    every one of them. Its default therefore has to be "nothing".
    """

    def test_a_toolkit_with_no_entry_is_off(self) -> None:
        self.assertFalse(workspace_scope.is_enabled("gmail"))
        self.assertEqual(workspace_scope.enabled_toolkits(), [])
        self.assertEqual(workspace_scope.pins(), {})

    def test_enabling_is_partial_and_leaves_other_fields_alone(self) -> None:
        workspace_scope.set_toolkit("gmail", enabled=True,
                                    connected_account_ids=["ca_1"])
        workspace_scope.set_toolkit("gmail", enabled=False)
        entry = workspace_scope.load()["gmail"]
        self.assertFalse(entry["enabled"])
        self.assertEqual(entry["connected_account_ids"], ["ca_1"])

    def test_an_enabled_toolkit_with_no_pin_is_omitted_from_the_pin_map(self) -> None:
        # Composio reads an empty pin list as "no account is permitted", which would
        # surface as a confusing execution-time failure rather than a default.
        workspace_scope.set_toolkit("gmail", enabled=True)
        self.assertEqual(workspace_scope.enabled_toolkits(), ["gmail"])
        self.assertEqual(workspace_scope.pins(), {})

    def test_duplicate_pins_are_collapsed(self) -> None:
        workspace_scope.set_toolkit(
            "gmail", enabled=True,
            connected_account_ids=["ca_1", "ca_1", "ca_2"], max_accounts=5,
        )
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_1", "ca_2"]})

    def test_unlinking_the_last_account_switches_the_toolkit_off(self) -> None:
        _enable("gmail", "ca_1")
        workspace_scope.unlink_account("gmail", "ca_1")
        self.assertFalse(workspace_scope.is_enabled("gmail"))
        self.assertEqual(workspace_scope.pins(), {})

    def test_unlinking_one_of_several_leaves_the_toolkit_on(self) -> None:
        workspace_scope.set_toolkit(
            "gmail", enabled=True,
            connected_account_ids=["ca_1", "ca_2"], max_accounts=5,
        )
        workspace_scope.unlink_account("gmail", "ca_1")
        self.assertTrue(workspace_scope.is_enabled("gmail"))
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_2"]})

    def test_a_connect_here_enables_it_here(self) -> None:
        workspace_scope.adopt_connection("gmail", "ca_new")
        self.assertTrue(workspace_scope.is_enabled("gmail"))
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_new"]})

    def test_adopting_replaces_the_pin_when_only_one_is_allowed(self) -> None:
        _enable("gmail", "ca_old")
        workspace_scope.adopt_connection("gmail", "ca_new", max_accounts=1)
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_new"]})

    def test_adopting_twice_does_not_duplicate(self) -> None:
        workspace_scope.adopt_connection("gmail", "ca_1", max_accounts=5)
        workspace_scope.adopt_connection("gmail", "ca_1", max_accounts=5)
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_1"]})

    def test_the_store_survives_a_process_restart(self) -> None:
        _enable("notion", "ca_n")
        self.assertEqual(workspace_scope.pins(), {"notion": ["ca_n"]})
        stored = json.loads(self.scope_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)
        # Flat: a pod is one workspace, so there is no workspace level to key on.
        self.assertNotIn("workspaces", stored)

    def test_an_unreadable_document_reads_as_nothing_enabled(self) -> None:
        # Fail closed. A corrupt store must not be read as "everything on".
        self.scope_path.parent.mkdir(parents=True, exist_ok=True)
        self.scope_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(workspace_scope.load(), {})
        self.assertEqual(workspace_scope.enabled_toolkits(), [])


class WorkspaceScopeRouteTests(unittest.IsolatedAsyncioTestCase, _ComposioBase):
    async def test_scope_route_reports_this_workspace_s_choice(self) -> None:
        _enable("gmail", "ca_1")
        response = await router_mod.get_toolkit_scope("gmail", user_id=ACCOUNT)
        body = json.loads(response.body)
        self.assertTrue(body["workspace_enabled"])
        self.assertEqual(body["pinned_account_ids"], ["ca_1"])

    async def test_pinning_an_account_the_user_does_not_hold_is_a_422(self) -> None:
        # Caught here rather than at session creation, where one bad id fails every
        # toolkit at once.
        body = router_mod.ScopeBody(enabled=True, connected_account_ids=["ca_nope"])
        with patch.object(service, "list_connections", return_value=[]):
            with self.assertRaises(HTTPException) as raised:
                await router_mod.put_toolkit_scope("gmail", body, user_id=ACCOUNT)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("ca_nope", raised.exception.detail)

    async def test_an_unknown_toolkit_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await router_mod.put_toolkit_scope(
                "nosuch", router_mod.ScopeBody(enabled=True), user_id=ACCOUNT,
            )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_enabling_a_toolkit_re_syncs_the_session(self) -> None:
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "status": "ACTIVE", "alias": None, "created_at": None}]
        body = router_mod.ScopeBody(enabled=True, connected_account_ids=["ca_1"])
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "sync_session") as synced:
            response = await router_mod.put_toolkit_scope("gmail", body, user_id=ACCOUNT)
        self.assertTrue(json.loads(response.body)["workspace_enabled"])
        synced.assert_called_once_with(ACCOUNT)

    async def test_unlink_touches_the_scope_and_never_composio(self) -> None:
        _enable("gmail", "ca_1")
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "status": "ACTIVE", "alias": None, "created_at": None}]
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "disconnect") as deleted, \
                patch.object(service, "sync_session"):
            response = await router_mod.unlink_account("gmail", "ca_1", user_id=ACCOUNT)
        # "Not here", not "delete": the account stays connected for every other
        # workspace of this XO account.
        deleted.assert_not_called()
        self.assertFalse(json.loads(response.body)["workspace_enabled"])

    async def test_disconnect_deletes_account_wide_and_clears_the_local_pin(self) -> None:
        _enable("gmail", "ca_1")
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "status": "ACTIVE", "alias": None, "created_at": None}]
        body = router_mod.DisconnectBody(connected_account_id="ca_1")
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "disconnect", return_value=True) as deleted, \
                patch.object(service, "sync_session"):
            await router_mod.disconnect("gmail", body, user_id=ACCOUNT)
        deleted.assert_called_once_with("ca_1")
        self.assertEqual(workspace_scope.pins(), {})

    async def test_a_completed_connect_enables_the_toolkit_in_this_workspace(self) -> None:
        # The OAuth callback carries no account id; the status poll is what learns it.
        result = {"status": "ACTIVE", "connected_account_id": "ca_fresh"}
        with patch.object(service, "check_connection", return_value=result), \
                patch.object(service, "sync_session"):
            await router_mod.connect_status(
                "gmail", connection_request_id="cr_1", user_id=ACCOUNT,
            )
        self.assertTrue(workspace_scope.is_enabled("gmail"))
        self.assertEqual(workspace_scope.pins(), {"gmail": ["ca_fresh"]})

    async def test_toolkits_route_separates_connected_from_enabled_here(self) -> None:
        # The distinction the whole change rests on: a sibling workspace's connection
        # is visible on the account but must not be reachable from this one.
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1",
                 "status": "ACTIVE", "alias": None, "created_at": None}]
        with patch.object(service, "list_connections", return_value=rows), \
                patch.object(service, "kick_gateway_sweep"):
            response = await router_mod.list_toolkits(user_id=ACCOUNT)
        gmail = next(t for t in json.loads(response.body)["toolkits"]
                     if t["id"] == "gmail")
        self.assertEqual(gmail["status"], "ACTIVE")
        self.assertFalse(gmail["workspace_enabled"])

    async def test_the_route_never_looks_up_a_foreign_user_id(self) -> None:
        # The reconnect prompt is retired. Listing toolkits must query this account and
        # nothing else — an extra round trip under the retired key is the regression.
        seen: list[str] = []

        def _list(user_id, **kw):
            seen.append(user_id)
            return []

        with patch.object(service, "list_connections", side_effect=_list), \
                patch.object(service, "kick_gateway_sweep"):
            response = await router_mod.list_toolkits(user_id=ACCOUNT)
        self.assertEqual(set(seen), {ACCOUNT})
        self.assertNotIn("legacy_connections", json.loads(response.body))

    async def test_a_legacy_connection_is_never_pinned(self) -> None:
        # Composio requires a pinned account to belong to the session's user_id, so a
        # legacy row is unreachable by construction. Listing must not imply otherwise.
        _enable("gmail")
        legacy_rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_legacy",
                        "status": "ACTIVE", "created_at": None}]

        def _list(user_id, **kw):
            return legacy_rows if user_id == LEGACY_PRINCIPAL else []

        with patch.object(service, "list_connections", side_effect=_list):
            service.prune_scope_to_live_accounts(ACCOUNT)
        self.assertNotIn("ca_legacy", str(workspace_scope.pins()))


if __name__ == "__main__":
    unittest.main()
