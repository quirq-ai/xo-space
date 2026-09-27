"""The XO GitHub App connector method (connectors/github/app_auth.py).

The point of this file: an app-backed connection is visible to both GitHub
pollers through the same `get_github_token()` seam a PAT or a `gh` session
uses, it is kept fresh, and it never touches a PAT / `gh` connection.
Swarm calls and GitHub are faked; nothing reaches the network.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.connectors import token_store
from services.cowork_agent.connectors.github import app_auth, common
from services.swarm_api import SwarmResult


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _iso(seconds_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat().replace("+00:00", "Z")


def minted(token: str, ttl: float = 3600) -> SwarmResult:
    return SwarmResult(ok=True, status=200, data={"token": token, "expires_at": _iso(ttl)})


class AppConnectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(token_store, "TOKEN_FILE", Path(self.tmp.name) / "token.json"),
            patch.object(token_store, "_LEGACY_TOKEN_FILES", ()),
            patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        self.mint = AsyncMock(return_value=minted("ghs_first"))
        self.patches.append(patch.object(app_auth.swarm_github_app, "installation_token", self.mint))
        self.patches[-1].start()
        self.status = AsyncMock(return_value={"status": "connected", "auth_method": "app"})
        self.patches.append(patch.object(app_auth, "status", self.status))
        self.patches[-1].start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _connect(self):
        return run(app_auth.connect("grant-1", account_login="octo", installation_id=42))

    # ── The pollers see it ────────────────────────────────────────────────

    def test_connect_feeds_both_pollers_through_the_shared_token(self):
        with patch("services.cowork_agent.github_poller.note_auth_change") as notified:
            self.assertTrue(self._connect()["ok"])
        notified.assert_called_once()  # a stale not_authenticated backoff is dropped now

        self.assertEqual(common.get_github_token(), "ghs_first")
        self.assertEqual(common.get_github_auth_method(), "app")

        # Issue poller: gh runs with the installation token as GH_TOKEN.
        from services.cowork_agent.connectors.github import issues
        self.assertEqual(issues._subprocess_env()["GH_TOKEN"], "ghs_first")

        # Relay poller: git auth resolves to the same token (x-access-token header).
        from services.cowork_agent.xo_projects_sync.github import _git_extraheader, resolve_auth
        auth = run(resolve_auth())
        self.assertEqual((auth.token, auth.source), ("ghs_first", "connector"))
        self.assertIn("basic", _git_extraheader(auth))

    def test_refresh_replaces_a_token_near_expiry_and_keeps_the_grant(self):
        self._connect()
        entry = token_store.get_entry("github")
        entry["expires_at"] = time.time() + 60  # inside the refresh margin
        token_store.set_entry("github", entry)

        self.mint.return_value = minted("ghs_second")
        self.assertTrue(run(app_auth.refresh_if_needed()))
        entry = token_store.get_entry("github")
        self.assertEqual(common.get_github_token(), "ghs_second")
        self.assertEqual((entry["app_grant"], entry["account_login"], entry["installation_id"]),
                         ("grant-1", "octo", 42))

    def test_routine_refresh_is_not_a_credential_change_for_the_issue_poller(self):
        from services.cowork_agent import github_poller
        self._connect()
        before = github_poller._auth_signature()
        entry = token_store.get_entry("github")
        entry["expires_at"] = time.time() + 60
        token_store.set_entry("github", entry)
        self.mint.return_value = minted("ghs_second")
        with patch.object(github_poller, "note_auth_change") as notified:
            self.assertTrue(run(app_auth.refresh_if_needed()))
        notified.assert_not_called()
        # Same grant, so the hourly rotation does not clear every backoff.
        self.assertEqual(github_poller._auth_signature(), before)

    def test_refresh_after_expiry_wakes_the_issue_poller(self):
        self._connect()
        entry = token_store.get_entry("github")
        entry["expires_at"] = time.time() - 5  # lapsed: polls are failing on it
        token_store.set_entry("github", entry)
        self.mint.return_value = minted("ghs_second")
        with patch("services.cowork_agent.github_poller.note_auth_change") as notified:
            self.assertTrue(run(app_auth.refresh_if_needed()))
        notified.assert_called_once()

    def test_relay_fetch_carries_the_app_token(self):
        from services.cowork_agent.project_sharing import clone, git_ops
        self._connect()
        auth, _ = run(clone._github_auth())
        args = clone._config_args("github.com/acme/app", auth)
        with patch.object(git_ops, "_run", AsyncMock(return_value=(0, "", ""))) as git:
            run(git_ops.fetch_origin("/repo", config_args=args))
        argv = git.call_args.args
        self.assertEqual(argv[1:3], ("-c", args[1]))
        self.assertIn("extraheader", argv[2])
        self.assertEqual(argv[3:], ("fetch", "origin", "--quiet"))

    def test_refresh_is_a_no_op_while_the_token_is_fresh(self):
        self._connect()
        self.mint.reset_mock()
        self.assertFalse(run(app_auth.refresh_if_needed()))
        self.mint.assert_not_called()

    def test_failed_refresh_keeps_the_old_token(self):
        self._connect()
        self.mint.return_value = SwarmResult(ok=False, status=409, detail="uninstalled")
        self.assertFalse(run(app_auth.refresh_if_needed(force=True)))
        self.assertEqual(common.get_github_token(), "ghs_first")

    def test_connect_with_a_rejected_grant_stores_nothing(self):
        self.mint.return_value = SwarmResult(ok=False, status=403, detail="Invalid or expired GitHub App grant")
        result = self._connect()
        self.assertEqual((result["ok"], result["status"]), (False, "needs_auth"))
        self.assertIsNone(common.get_github_token())

    # ── The gh / PAT flows are left alone ─────────────────────────────────

    def test_refresher_never_touches_a_pat_or_gh_connection(self):
        for method in ("pat", "cli"):
            with patch("services.cowork_agent.github_poller.note_auth_change"):
                common.save_github_token("gho_user", auth_method=method)
            self.assertFalse(run(app_auth.refresh_if_needed(force=True)))
            self.mint.assert_not_called()
            self.assertEqual(common.get_github_token(), "gho_user")

    def test_gh_login_after_the_app_replaces_it(self):
        self._connect()
        with patch("services.cowork_agent.github_poller.note_auth_change"):
            common.save_github_token("gho_user", auth_method="cli")
        self.assertFalse(run(app_auth.refresh_if_needed(force=True)))
        self.assertEqual(common.get_github_token(), "gho_user")

    # ── Status ────────────────────────────────────────────────────────────

    def test_status_routes_app_tokens_away_from_get_user(self):
        self._connect()
        with patch.object(common, "validate_token", AsyncMock()) as validate:
            self.assertEqual(run(common.get_status())["auth_method"], "app")
        validate.assert_not_called()  # GET /user always 403s for an installation token


class SwarmHeadersTest(unittest.TestCase):
    def test_extra_headers_never_override_the_bearer_token(self):
        from services.swarm_api import _http

        seen = {}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kw):
                seen.update(kw["headers"])
                raise RuntimeError("stop")

        with patch.object(_http, "auth_token", return_value="xo-token"), \
                patch.object(_http.httpx, "AsyncClient", _Client):
            run(_http.request("POST", "/github-app/token",
                              headers={"X-GitHub-App-Grant": "g", "Authorization": "Bearer evil"}))
        self.assertEqual(seen, {"X-GitHub-App-Grant": "g", "Authorization": "Bearer xo-token"})


if __name__ == "__main__":
    unittest.main()
