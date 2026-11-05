"""The GitHub connector reuses this machine's gh login (cli_auth): a login
that already has the scopes the Space needs is imported, never logged out
and replaced by a second sign-in. gh is a fake on PATH; GitHub's /user is
mocked; nothing is saved to the developer's token store."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.connectors import github_cli, github_pat
from services.cowork_agent.connectors.github import cli_auth
from tests.launchpad_kit import remove, temp_root

TOKEN = "gho_existing_login_token_123"


def valid(scopes: str) -> dict:
    return {"valid": True, "status": "connected", "username": "octo", "name": "Octo", "avatar_url": "",
            "scopes": scopes, "user_id": 42, "email": ""}


class ReuseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "gh-calls"
        self.stored = self.root / "gh-token"
        gh = self.bin / "gh"
        gh.write_text(f"""#!/bin/sh
echo "$*" >> {self.calls}
case "$1 $2" in
  "auth token") if [ -s {self.stored} ]; then cat {self.stored}; exit 0; fi
                echo "no oauth token found" >&2; exit 1 ;;
  "auth logout") : > {self.stored}; exit 0 ;;
  "auth login") echo "! First copy your one-time code: ABCD-1234"; sleep 30 ;;
esac
""")
        gh.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                                "QUIRQ_COMMAND_LOG": "off", "GH_TOKEN": "gho_not_the_login"})
        self.env.start()
        self.scopes = "gist, read:org, repo, workflow"
        self.validate = mock.patch.object(cli_auth, "validate_token",
                                          mock.AsyncMock(side_effect=lambda token: valid(self.scopes)))
        self.save = mock.patch.object(cli_auth, "save_github_token")
        self.identity = mock.patch.object(cli_auth, "configure_git_identity", mock.AsyncMock())
        self.validated, self.saved, self.configured = self.validate.start(), self.save.start(), self.identity.start()
        cli_auth._existing_cache = None
        cli_auth._imported.clear()

    def tearDown(self) -> None:
        for patch in (self.validate, self.save, self.identity, self.env):
            patch.stop()
        cli_auth._existing_cache = None
        cli_auth._imported.clear()
        remove(self.root)

    def gh_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class ExistingLoginTests(ReuseTestCase):
    def test_a_login_with_repo_and_workflow_is_usable(self) -> None:
        self.stored.write_text(TOKEN + "\n")
        found = asyncio.run(cli_auth.existing_login())
        self.assertEqual(found, {"logged_in": True, "username": "octo", "scopes": self.scopes, "missing": [],
                                 "usable": True})
        self.validated.assert_awaited_once_with(TOKEN)              # gh's login, not GH_TOKEN
        asyncio.run(cli_auth.existing_login())
        self.assertEqual(len(self.gh_calls()), 1)                   # reused for a while

    def test_a_login_without_workflow_is_not(self) -> None:
        self.stored.write_text(TOKEN)
        self.scopes = "repo, read:org"
        found = asyncio.run(cli_auth.existing_login())
        self.assertEqual((found["usable"], found["missing"]), (False, ["workflow"]))

    def test_no_login(self) -> None:
        self.assertEqual(asyncio.run(cli_auth.existing_login()), {"logged_in": False, "usable": False})


class ImportTests(ReuseTestCase):
    def test_importing_saves_the_login_as_the_connection(self) -> None:
        self.stored.write_text(TOKEN)
        result = asyncio.run(cli_auth.import_existing_login())
        self.assertTrue(result["ok"])
        self.assertEqual((result["payload"]["status"], result["payload"]["username"], result["payload"]["imported"]),
                         ("connected", "octo", True))
        self.saved.assert_called_once_with(TOKEN, auth_method="cli")
        self.configured.assert_awaited_once()

    def test_a_login_that_cant_be_used_is_not_imported(self) -> None:
        result = asyncio.run(cli_auth.import_existing_login())
        self.assertEqual((result["ok"], result["code"]), (False, "gh_not_logged_in"))
        self.stored.write_text(TOKEN)
        self.scopes = "repo"
        result = asyncio.run(cli_auth.import_existing_login())
        self.assertEqual((result["code"], result["missing"]), ("gh_missing_scopes", ["workflow"]))
        self.assertIn("without the workflow scope", result["message"])
        self.saved.assert_not_called()


class StartLoginTests(ReuseTestCase):
    def test_connect_uses_the_existing_login_instead_of_logging_it_out(self) -> None:
        self.stored.write_text(TOKEN)

        async def flow() -> tuple:
            started = await cli_auth.start_login()
            return started, await cli_auth.connect(started["session_id"]), await cli_auth.connect(started["session_id"])

        started, first, again = asyncio.run(flow())
        self.assertEqual((started["status"], started["imported"], started["user_code"]), ("connected", True, ""))
        self.assertEqual((first["ok"], first["payload"]["username"]), (True, "octo"))
        self.assertEqual(again, {"ok": False, "status": "not_found"})          # handed out once
        self.assertFalse(any(call.startswith(("auth logout", "auth login")) for call in self.gh_calls()))
        self.assertEqual(self.stored.read_text(), TOKEN)                         # the login is still there

    def test_an_imported_answer_nobody_polls_expires(self) -> None:
        # The Space's own card never polls an imported session: its answer
        # must not stay in memory for ever.
        self.stored.write_text(TOKEN)
        started = asyncio.run(cli_auth.start_login())
        self.assertIn(started["session_id"], cli_auth._imported)
        later = cli_auth.time.time() + cli_auth.SESSION_TTL_SECONDS + 1
        with mock.patch.object(cli_auth.time, "time", return_value=later):
            asyncio.run(cli_auth.start_login())
        self.assertNotIn(started["session_id"], cli_auth._imported)
        self.assertEqual(len(cli_auth._imported), 1)

    def test_a_login_missing_workflow_falls_back_to_the_device_flow(self) -> None:
        self.stored.write_text(TOKEN)
        self.scopes = "repo"

        async def flow() -> dict:
            started = await cli_auth.start_login()
            await cli_auth.cancel_login(started["session_id"])
            return started

        started = asyncio.run(flow())
        self.assertEqual(started["user_code"], "ABCD-1234")
        calls = self.gh_calls()
        self.assertLess(calls.index("auth logout --hostname github.com"),
                        next(i for i, c in enumerate(calls) if c.startswith("auth login")))
        self.saved.assert_not_called()

    def test_fresh_always_starts_the_device_flow(self) -> None:
        self.stored.write_text(TOKEN)

        async def flow() -> dict:
            started = await cli_auth.start_login(fresh=True)
            await cli_auth.cancel_login(started["session_id"])
            return started

        self.assertEqual(asyncio.run(flow())["user_code"], "ABCD-1234")


class RouteTests(ReuseTestCase):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(github_cli.router)
        app.include_router(github_pat.router)
        self.client = TestClient(app)

    def test_existing_never_shows_the_token(self) -> None:
        self.stored.write_text(TOKEN)
        response = self.client.get("/api/connectors/github/cli/existing")
        self.assertEqual(response.json()["usable"], True)
        self.assertNotIn(TOKEN, response.text)

    def test_import_answers_the_connection_or_why_not(self) -> None:
        response = self.client.post("/api/connectors/github/cli/import")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "gh_not_logged_in"))
        self.stored.write_text(TOKEN)
        cli_auth._existing_cache = None
        response = self.client.post("/api/connectors/github/cli/import")
        self.assertEqual((response.status_code, response.json()["status"], response.json()["imported"]),
                         (200, "connected", True))
        self.assertNotIn(TOKEN, response.text)

    def test_start_says_connected_when_it_imported(self) -> None:
        self.stored.write_text(TOKEN)
        started = self.client.post("/api/connectors/github/cli/start", json={}).json()
        self.assertEqual((started["status"], started["imported"]), ("connected", True))
        polled = self.client.post("/api/connectors/github/cli/poll", json={"session_id": started["session_id"]})
        self.assertEqual((polled.status_code, polled.json()["status"]), (200, "connected"))

    def test_status_hints_at_the_gh_login_when_nothing_is_saved(self) -> None:
        self.stored.write_text(TOKEN)
        with mock.patch.object(github_pat, "get_status", mock.AsyncMock(return_value={"status": "needs_auth"})):
            status = self.client.get("/api/connectors/github/status").json()
        self.assertEqual((status["status"], status["gh_login"]["usable"], status["gh_login"]["username"]),
                         ("needs_auth", True, "octo"))
        with mock.patch.object(github_pat, "get_status",
                               mock.AsyncMock(return_value={"status": "connected", "username": "octo"})):
            self.assertNotIn("gh_login", self.client.get("/api/connectors/github/status").json())


if __name__ == "__main__":
    unittest.main()
