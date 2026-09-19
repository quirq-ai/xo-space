"""Setup verifies identity without minting sessions or exposing credentials."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from routers.errors import install_service_errors
from routers.space import router
from modules.settings import setup_status
from services.cowork_agent.connectors import token_store
from services.cowork_agent.xo_projects_sync import github
from services.swarm_api._http import SwarmResult

_RESOLVE_AUTH = github.resolve_auth
_DISCOVER_OWNER = github.discover_owner
_SECRET = "test-private-credential-do-not-return"


class SetupStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "XO_SPACE_ID": "space-test-id", "CODER_WORKSPACE_NAME": "Example workspace",
            "CODER_WORKSPACE_OWNER_NAME": "configured-owner",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.auth = github.GitHubAuth(token=_SECRET, source="connector")
        self.xo = AsyncMock(return_value=SwarmResult(ok=True, status=200, data={"user_id": "xo-user"}))
        self.resolve = AsyncMock(return_value=self.auth)
        self.owner = AsyncMock(return_value="github-user")
        for target, name, replacement in (
            (setup_status.swarm_auth, "get_user_id", self.xo),
            (github, "resolve_auth", self.resolve),
            (github, "discover_owner", self.owner),
        ):
            replacement_patch = patch.object(target, name, replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)

    async def test_snapshot_separates_configured_owner_from_verified_accounts(self):
        result = await setup_status.snapshot()
        self.assertEqual(result["space"], {
            "status": "configured", "id": "space-test-id", "label": "Example workspace",
            "owner": "configured-owner",
        })
        self.assertEqual(result["xo"], {"status": "connected", "user_id": "xo-user"})
        self.assertEqual(result["github"], {
            "status": "connected", "username": "github-user", "source": "connector",
        })
        self.assertTrue(result["checked_at"])
        self.assertNotIn(_SECRET, json.dumps(result))
        self.xo.assert_awaited_once_with()
        self.owner.assert_awaited_once_with(self.auth)

    async def test_no_credentials_is_not_configured_without_inventing_a_local_user(self):
        self.xo.return_value = SwarmResult(ok=False, unauthenticated=True)
        self.resolve.side_effect = github.AuthMissingError(_SECRET)
        with patch.dict(os.environ, {"XO_SPACE_ID": "", "CODER_WORKSPACE_NAME": "", "CODER_WORKSPACE_OWNER_NAME": ""}):
            result = await setup_status.snapshot()
        self.assertEqual(result["space"], {"status": "not_configured", "id": None, "label": None, "owner": None})
        self.assertEqual(result["xo"], {"status": "not_configured", "user_id": None})
        self.assertEqual(result["github"], {"status": "not_configured", "username": None, "source": None})
        self.owner.assert_not_awaited()
        self.assertNotIn(_SECRET, json.dumps(result))

    async def test_xo_rejection_is_distinct_from_offline_and_bad_responses(self):
        for response, status in (
            (SwarmResult(ok=False, status=401, text=_SECRET), "rejected"),
            (SwarmResult(ok=False, status=403, detail=_SECRET), "rejected"),
            (SwarmResult(ok=False, status=429, detail=_SECRET), "unavailable"),
            (SwarmResult(ok=False, status=500, text=_SECRET), "unavailable"),
            (SwarmResult(ok=False, offline=True, detail=_SECRET), "unavailable"),
            (SwarmResult(ok=True, status=200, data={"user_id": ""}), "unavailable"),
            (SwarmResult(ok=True, status=200, data={"user_id": True}), "unavailable"),
            (SwarmResult(ok=True, status=200, data=[]), "unavailable"),
        ):
            with self.subTest(response_status=response.status, status=status):
                self.xo.return_value = response
                result = await setup_status.snapshot()
                self.assertEqual(result["xo"], {"status": status, "user_id": None})
                self.assertEqual(result["github"]["status"], "connected")
                self.assertNotIn(_SECRET, json.dumps(result))

    async def test_github_403_cannot_prove_credential_rejection(self):
        for code, status in ((401, "rejected"), (403, "unavailable"), (429, "unavailable"), (500, "unavailable")):
            with self.subTest(code=code):
                self.owner.side_effect = github.GitHubAPIError(code, _SECRET)
                result = await setup_status.snapshot()
                self.assertEqual(result["github"], {"status": status, "username": None, "source": "connector"})
                self.assertEqual(result["xo"]["status"], "connected")
                self.assertNotIn(_SECRET, json.dumps(result))

    async def test_unexpected_errors_never_escape_as_raw_provider_details(self):
        self.xo.side_effect = RuntimeError(_SECRET)
        self.resolve.side_effect = OSError(_SECRET)
        with patch.object(setup_status.coder_identity, "workspace_name", side_effect=ValueError(_SECRET)):
            result = await setup_status.snapshot()
        for name in ("space", "xo", "github"):
            self.assertEqual(result[name]["status"], "unavailable")
        self.assertNotIn(_SECRET, json.dumps(result))

    async def test_both_network_checks_start_in_parallel_and_timeout_independently(self):
        started = set()
        overlap = set()
        both_started = asyncio.Event()

        async def stalled(name):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            overlap.add(name)
            await asyncio.Event().wait()

        async def xo_stalled():
            await stalled("xo")

        async def github_stalled(_auth):
            await stalled("github")

        self.xo.side_effect = xo_stalled
        self.owner.side_effect = github_stalled
        with patch.object(setup_status, "CHECK_TIMEOUT_SECONDS", 0.03):
            result = await asyncio.wait_for(setup_status.snapshot(), 0.5)
        self.assertEqual(started, {"xo", "github"})
        self.assertEqual(overlap, {"xo", "github"})
        self.assertEqual(result["xo"]["status"], "unavailable")
        self.assertEqual(result["github"]["status"], "unavailable")
        self.assertEqual(result["space"]["id"], "space-test-id")

    async def test_one_timeout_preserves_the_other_verified_identity(self):
        async def stalled():
            await asyncio.Event().wait()

        self.xo.side_effect = stalled
        with patch.object(setup_status, "CHECK_TIMEOUT_SECONDS", 0.01):
            result = await setup_status.snapshot()
        self.assertEqual(result["xo"]["status"], "unavailable")
        self.assertEqual(result["github"]["username"], "github-user")

    async def test_uses_real_backup_auth_precedence_and_only_user_lookup(self):
        for connector_token, env_token, expected_source in (
            ("connector-private", "environment-private", "connector"),
            (None, "environment-private", "env"),
        ):
            with self.subTest(source=expected_source), \
                    patch.dict(os.environ, {"GITHUB_PAT": env_token}), \
                    patch.object(github.github_connector, "get_github_token", return_value=connector_token), \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH), \
                    patch.object(github, "discover_owner", _DISCOVER_OWNER), \
                    patch.object(github, "_get", AsyncMock(return_value={"login": "verified-login"})) as lookup:
                result = await setup_status.snapshot()
                self.assertEqual(result["github"], {
                    "status": "connected", "username": "verified-login", "source": expected_source,
                })
                self.assertEqual(lookup.await_count, 1)
                auth, path = lookup.await_args.args
                self.assertEqual(path, "/user")
                self.assertEqual(auth.token, connector_token or env_token)
                self.assertNotIn("-private", json.dumps(result))

    async def test_github_success_without_a_username_is_not_connected(self):
        self.owner.return_value = " "
        result = await setup_status.snapshot()
        self.assertEqual(result["github"]["status"], "unavailable")
        self.assertIsNone(result["github"]["username"])

    async def test_current_store_is_read_without_modifying_it_or_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "token.json"
            legacy = Path(directory) / "mcp-tokens.json"
            current.write_text(json.dumps({"github": {"access_token": _SECRET}}))
            legacy.write_text(json.dumps({"github": {"access_token": "old-private-token"}}))
            before = (current.read_bytes(), legacy.read_bytes(), current.stat().st_mtime_ns)
            with patch.object(token_store, "TOKEN_FILE", current), \
                    patch.object(token_store, "_LEGACY_TOKEN_FILES", (legacy,)), \
                    patch.object(token_store, "_migrate_legacy_file") as migrate, \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
            self.assertEqual(result["github"]["status"], "connected")
            self.owner.assert_awaited_once_with(github.GitHubAuth(token=_SECRET, source="connector"))
            migrate.assert_not_called()
            self.assertEqual(before, (current.read_bytes(), legacy.read_bytes(), current.stat().st_mtime_ns))
            self.assertNotIn(_SECRET, json.dumps(result))

    async def test_legacy_store_is_checked_in_place_but_default_read_still_migrates(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "new-location" / "token.json"
            legacy = Path(directory) / "mcp-tokens.json"
            legacy.write_text(json.dumps({"github": {"access_token": _SECRET}}))
            before = legacy.read_bytes()
            with patch.object(token_store, "TOKEN_FILE", current), \
                    patch.object(token_store, "_LEGACY_TOKEN_FILES", (legacy,)), \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
                self.assertEqual(result["github"]["status"], "connected")
                self.assertEqual(legacy.read_bytes(), before)
                self.assertFalse(current.parent.exists())
                self.assertEqual(github.github_connector.get_github_token(), _SECRET)
                self.assertFalse(legacy.exists())
                self.assertEqual(current.read_bytes(), before)

    async def test_bad_current_or_legacy_store_is_unavailable_and_never_rewritten(self):
        for use_legacy in (False, True):
            for body in ("{invalid", "[]", '{"github": "invalid"}', '{"github":{"access_token":17}}'):
                with self.subTest(legacy=use_legacy, body=body), tempfile.TemporaryDirectory() as directory:
                    current = Path(directory) / "new" / "token.json"
                    legacy = Path(directory) / "mcp-tokens.json"
                    source = legacy if use_legacy else current
                    source.parent.mkdir(exist_ok=True)
                    source.write_text(body)
                    with patch.object(token_store, "TOKEN_FILE", current), \
                            patch.object(token_store, "_LEGACY_TOKEN_FILES", (legacy,)), \
                            patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                        result = await setup_status.snapshot()
                    self.assertEqual(result["github"]["status"], "unavailable")
                    self.assertEqual(source.read_text(), body)
                    if use_legacy:
                        self.assertFalse(current.parent.exists())
                    self.assertEqual(result["xo"]["status"], "connected")

    async def test_unreadable_store_is_unavailable_instead_of_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "token.json"
            current.mkdir()  # A real read failure on every platform, including privileged test users.
            with patch.object(token_store, "TOKEN_FILE", current), \
                    patch.object(token_store, "_LEGACY_TOKEN_FILES", ()), \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
            self.assertEqual(result["github"]["status"], "unavailable")
            self.assertTrue(current.is_dir())

    async def test_get_route_keeps_partial_provider_failure_in_a_safe_snapshot(self):
        self.owner.side_effect = github.GitHubAPIError(403, _SECRET)
        app = FastAPI()
        app.include_router(router)
        install_service_errors(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/space/setup/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["xo"]["status"], "connected")
        self.assertEqual(response.json()["github"]["status"], "unavailable")
        self.assertNotIn(_SECRET, response.text)


if __name__ == "__main__":
    unittest.main()
