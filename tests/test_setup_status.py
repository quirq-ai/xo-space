"""Setup verifies identity without minting sessions or exposing credentials."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

from routers.space import router
from services import setup_status
from services.cowork_agent.connectors.github import common
from services.cowork_agent.xo_projects_sync import github
from services.swarm_api._http import SwarmResult
from utils.commands import CommandResult

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

    def _gh_store(self, hosts_file: Path, answer: CommandResult):
        """Point the connector at a gh whose `gh auth token` gives ``answer``."""
        common._forget_gh_token()
        self.addCleanup(common._forget_gh_token)
        self.gh_token = Mock(return_value=answer)
        return patch.multiple(common, gh_hosts_file=lambda: hosts_file, run_sync=self.gh_token)

    async def test_connector_token_is_read_from_gh(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "hosts.yml"
            hosts.write_text("github.com: {}\n")
            answer = CommandResult(argv=["gh"], returncode=0, output=_SECRET + "\n", duration_seconds=0.0)
            with self._gh_store(hosts, answer), patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
        self.assertEqual(result["github"]["status"], "connected")
        self.owner.assert_awaited_once_with(github.GitHubAuth(token=_SECRET, source="connector"))
        self.assertNotIn(_SECRET, json.dumps(result))

    async def test_signed_out_gh_is_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            answer = CommandResult(argv=["gh"], returncode=0, output=_SECRET, duration_seconds=0.0)
            with self._gh_store(Path(directory) / "hosts.yml", answer), \
                    patch.dict(os.environ, {"GITHUB_PAT": ""}), \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
        self.assertEqual(result["github"]["status"], "not_configured")
        self.gh_token.assert_not_called()  # no hosts.yml: gh was never signed in

    async def test_gh_that_cannot_be_asked_is_unavailable_instead_of_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "hosts.yml"
            hosts.write_text("github.com: {}\n")
            answer = CommandResult(argv=["gh"], returncode=-9, output="[timed out after 5s]",
                                   duration_seconds=5.0, timed_out=True)
            with self._gh_store(hosts, answer), \
                    patch.dict(os.environ, {"GITHUB_PAT": ""}), \
                    patch.object(github, "resolve_auth", _RESOLVE_AUTH):
                result = await setup_status.snapshot()
        self.assertEqual(result["github"]["status"], "unavailable")
        self.assertEqual(result["xo"]["status"], "connected")

    async def test_get_route_keeps_partial_provider_failure_in_a_safe_snapshot(self):
        self.owner.side_effect = github.GitHubAPIError(403, _SECRET)
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/space/setup/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["xo"]["status"], "connected")
        self.assertEqual(response.json()["github"]["status"], "unavailable")
        self.assertNotIn(_SECRET, response.text)


if __name__ == "__main__":
    unittest.main()
