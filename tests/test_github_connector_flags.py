"""Feature flags for the GitHub connector's PAT and `gh auth login` methods.

The GitHub App is the default connect method: both flags default to off.
A flag gates connecting through its method only: `/cli/cancel` always works,
and a token already stored stays readable by the pollers when its method is off.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.connectors.github_cli import router as cli_router
from routers.cowork_agent.connectors.github_pat import router as pat_router
from services.cowork_agent.connectors import token_store
from services.cowork_agent.connectors.github import cli_auth, common, flags, pat

PAT_TOKEN = "ghp_" + "x" * 36


class GitHubConnectorFlagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(token_store, "TOKEN_FILE", Path(self.tmp.name) / "token.json"),
            patch.object(token_store, "_LEGACY_TOKEN_FILES", ()),
            patch.dict(os.environ, {}, clear=False),
            patch.object(pat, "connect", AsyncMock(return_value={"ok": True, "payload": {"status": "connected"}})),
            patch.object(cli_auth, "start_login", AsyncMock(return_value={"session_id": "s"})),
            patch.object(cli_auth, "connect", AsyncMock(return_value={"ok": True, "payload": {"status": "connected"}})),
            patch.object(cli_auth, "cancel_login", AsyncMock(return_value={"ok": True})),
        ]
        for p in self.patches:
            p.start()
        for name in (flags.ENV_PAT_ENABLED, flags.ENV_CLI_AUTH_ENABLED):
            os.environ.pop(name, None)
        app = FastAPI()
        app.include_router(pat_router)
        app.include_router(cli_router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_only_the_app_method_is_on_by_default(self):
        self.assertEqual(self.client.post("/api/connectors/github/token", json={"token": PAT_TOKEN}).status_code, 403)
        self.assertEqual(self.client.post("/api/connectors/github/cli/start").status_code, 403)
        self.assertEqual(self.client.post("/api/connectors/github/cli/poll", json={"session_id": "s"}).status_code, 403)
        pat.connect.assert_not_called()
        cli_auth.start_login.assert_not_called()
        self.assertEqual(self.client.get("/api/connectors/github/methods").json(),
                         {"pat": False, "cli": False, "app": True})

    def test_both_methods_can_be_turned_back_on(self):
        os.environ[flags.ENV_PAT_ENABLED] = "true"
        os.environ[flags.ENV_CLI_AUTH_ENABLED] = "true"
        self.assertEqual(self.client.post("/api/connectors/github/token", json={"token": PAT_TOKEN}).status_code, 200)
        self.assertEqual(self.client.post("/api/connectors/github/cli/start").status_code, 200)
        self.assertEqual(self.client.post("/api/connectors/github/cli/poll", json={"session_id": "s"}).status_code, 200)

    def test_pat_flag_blocks_only_the_pat_route(self):
        os.environ[flags.ENV_CLI_AUTH_ENABLED] = "true"
        os.environ[flags.ENV_PAT_ENABLED] = "false"
        r = self.client.post("/api/connectors/github/token", json={"token": PAT_TOKEN})
        self.assertEqual(r.status_code, 403)
        self.assertIn(flags.ENV_PAT_ENABLED, r.json()["detail"])
        pat.connect.assert_not_called()
        self.assertEqual(self.client.post("/api/connectors/github/cli/start").status_code, 200)
        self.assertEqual(self.client.get("/api/connectors/github/methods").json()["pat"], False)

    def test_cli_flag_blocks_start_and_poll_but_not_cancel(self):
        os.environ[flags.ENV_PAT_ENABLED] = "true"
        os.environ[flags.ENV_CLI_AUTH_ENABLED] = "0"
        self.assertEqual(self.client.post("/api/connectors/github/cli/start").status_code, 403)
        self.assertEqual(self.client.post("/api/connectors/github/cli/poll", json={"session_id": "s"}).status_code, 403)
        cli_auth.start_login.assert_not_called()
        cli_auth.connect.assert_not_called()
        self.assertEqual(self.client.post("/api/connectors/github/cli/cancel", json={"session_id": "s"}).status_code, 200)
        self.assertEqual(self.client.post("/api/connectors/github/token", json={"token": PAT_TOKEN}).status_code, 200)

    def test_a_stored_token_outlives_its_method_being_disabled(self):
        with patch("services.cowork_agent.github_poller.note_auth_change"):
            common.save_github_token("gho_cli_user", auth_method="cli")
        os.environ[flags.ENV_CLI_AUTH_ENABLED] = "false"
        os.environ[flags.ENV_PAT_ENABLED] = "false"
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        from services.cowork_agent.connectors.github import issues
        self.assertEqual(common.get_github_token(), "gho_cli_user")
        self.assertEqual(issues._subprocess_env()["GH_TOKEN"], "gho_cli_user")

    def test_flag_values(self):
        for raw, expected in (("", False), ("true", True), ("1", True), ("on", True),
                              ("false", False), ("0", False), ("no", False), ("off", False)):
            os.environ[flags.ENV_PAT_ENABLED] = raw
            self.assertIs(flags.pat_enabled(), expected, raw)


if __name__ == "__main__":
    unittest.main()
