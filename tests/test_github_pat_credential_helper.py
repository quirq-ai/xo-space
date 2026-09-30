"""Connecting GitHub hands the token to `gh`, and keeps no copy of its own.

A pasted PAT goes through `gh auth login --with-token` and `gh auth setup-git`,
so git authenticates through `gh auth git-credential`, the connector reads the
token back with `gh auth token`, and token.json is never touched.

Runs the real subprocess path against a fake `gh` on PATH (it records every
call and keeps its "session" in $GH_CONFIG_DIR/hosts.yml, as gh does) and real
git against a throwaway global gitconfig.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.connectors import github_pat as github_pat_routes
from services.cowork_agent.connectors import token_store
from services.cowork_agent.connectors.github import cli_auth, common, pat
from utils.commands import CommandResult

TOKEN = "github_pat_test0123456789abcdefghij"
HUBOT_TOKEN = "github_pat_hubot0123456789abcdefghi"
VALID = {
    "valid": True, "status": "connected", "username": "octo", "name": "Octo Cat",
    "avatar_url": "", "scopes": "", "user_id": 1, "email": "",
}
OTHER = {**VALID, "username": "hubot", "name": "Hubot", "user_id": 2, "email": "hubot@example.com"}

# Tokens starting with "ghp_noscope" are refused the way gh refuses a classic
# PAT without `read:org`. Like gh, it keeps several accounts per host (a token
# containing "hubot" signs in as hubot, any other as octo): login adds one and
# makes it active, logging out the active one makes another active, and a
# logout that names no user fails once there are two. hosts.yml holds the
# active token; a bare one, with no accounts.json, is a single account.
FAKE_GH = textwrap.dedent("""\
    #!{python}
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    cfg = os.environ["GH_CONFIG_DIR"]
    hosts = os.path.join(cfg, "hosts.yml")
    accounts = os.path.join(cfg, "accounts.json")
    stdin = sys.stdin.read() if "--with-token" in args else None
    with open(os.path.join(cfg, "calls.jsonl"), "a") as f:
        f.write(json.dumps({{"args": args, "stdin": stdin,
                             "env_token": os.environ.get("GH_TOKEN")}}) + "\\n")

    def load():
        if os.path.exists(accounts):
            return json.load(open(accounts))
        if os.path.exists(hosts):
            return {{"active": "octo", "users": {{"octo": open(hosts).read()}}}}
        return {{"active": None, "users": {{}}}}

    def save(state):
        json.dump(state, open(accounts, "w"))
        if state["active"]:
            open(hosts, "w").write(state["users"][state["active"]])
        elif os.path.exists(hosts):
            os.remove(hosts)

    if args[:2] == ["auth", "login"]:
        token = stdin.strip()
        if token.startswith("ghp_noscope"):
            sys.stderr.write("error validating token: missing required scope 'read:org'\\n")
            sys.exit(1)
        state = load()
        login = "hubot" if "hubot" in token else "octo"
        state["users"][login] = token
        state["active"] = login
        save(state)
    elif args[:2] == ["auth", "token"]:
        if not os.path.exists(hosts):
            sys.stderr.write("no oauth token found for github.com\\n")
            sys.exit(1)
        print(open(hosts).read(), flush=True)
        if os.path.exists(os.path.join(cfg, "hang")):
            time.sleep(60)
    elif args[:2] == ["auth", "status"]:
        state = load()
        listed = [{{"login": login, "active": login == state["active"]}} for login in state["users"]]
        print(json.dumps({{"hosts": {{"github.com": listed}}}}))
    elif args[:2] == ["auth", "logout"]:
        state = load()
        user = args[args.index("--user") + 1] if "--user" in args else None
        if user is None:
            if len(state["users"]) > 1:
                sys.stderr.write("unable to determine which account to log out of\\n")
                sys.exit(1)
            user = next(iter(state["users"]), None)
        if user not in state["users"] or os.path.exists(os.path.join(cfg, "stuck")):
            sys.stderr.write("not logged in\\n")
            sys.exit(1)
        del state["users"][user]
        if state["active"] == user:
            state["active"] = next(iter(state["users"]), None)
        save(state)
    elif args[:2] == ["auth", "setup-git"]:
        for host in ("github.com", "gist.github.com"):
            key = "credential.https://" + host + ".helper"
            subprocess.run(["git", "config", "--global", "--replace-all", key, ""], check=True)
            subprocess.run(["git", "config", "--global", "--add", key, "!gh auth git-credential"], check=True)
""")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipUnless(shutil.which("git"), "git is required")
class GhCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake_gh = bin_dir / "gh"
        fake_gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
        fake_gh.chmod(0o755)
        self.gh_config = self.tmp / "gh"
        self.gh_config.mkdir()
        self.gitconfig = self.tmp / "gitconfig"
        self.gitconfig.touch()
        self.token_file = self.tmp / "token.json"

        self._patches = [
            patch.dict(os.environ, {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "GH_CONFIG_DIR": str(self.gh_config),
                "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                # A token in the server's environment must not leak into gh's store.
                "GH_TOKEN": "ghp_from_the_environment",
                "QUIRQ_COMMAND_LOG": "off",
            }),
            # Only so a stray write would land here and fail the tests, not in ~.
            patch.object(token_store, "TOKEN_FILE", self.token_file),
            patch.object(token_store, "_LEGACY_TOKEN_FILES", ()),
            patch.object(pat, "validate_token", AsyncMock(return_value=dict(VALID))),
            patch.object(cli_auth, "validate_token", AsyncMock(return_value=dict(VALID))),
        ]
        for p in self._patches:
            p.start()
        common._forget_gh_token()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        common._forget_gh_token()
        self._tmp.cleanup()

    def calls(self) -> list[dict]:
        log = self.gh_config / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def commands(self) -> list[list[str]]:
        return [c["args"][:2] for c in self.calls()]

    def gitconfig_text(self) -> str:
        return self.gitconfig.read_text(encoding="utf-8")

    def accounts(self) -> set[str]:
        """The github.com accounts the fake gh holds."""
        path = self.gh_config / "accounts.json"
        return set(json.loads(path.read_text(encoding="utf-8"))["users"]) if path.exists() else set()

    def logouts(self) -> list[list[str]]:
        return [c["args"][2:] for c in self.calls() if c["args"][:2] == ["auth", "logout"]]

    def terminal_login(self, token: str) -> None:
        """`gh auth login` run by hand, next to the connector."""
        subprocess.run(["gh", "auth", "login", "--with-token"], input=token, text=True, check=True)

    def test_pat_is_handed_to_gh_on_stdin_and_nowhere_else(self) -> None:
        result = run(pat.connect(TOKEN))

        self.assertTrue(result["ok"])
        login = next(c for c in self.calls() if c["args"][:2] == ["auth", "login"])
        self.assertIn("--with-token", login["args"])
        self.assertNotIn(TOKEN, login["args"])
        self.assertEqual(login["stdin"], TOKEN)
        self.assertIsNone(login["env_token"])
        self.assertIn(["auth", "setup-git"], self.commands())

        self.assertFalse(self.token_file.exists())
        self.assertEqual(common.get_github_token(), TOKEN)
        self.assertEqual(common.get_github_auth_method(), "pat")

    def test_token_gh_rejects_is_stored_nowhere(self) -> None:
        # Scopes that pass the connector's own check, so this is gh refusing.
        pat.validate_token.return_value = {**VALID, "scopes": "repo, read:org"}
        result = run(pat.connect("ghp_noscope0123456789012345678901234"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "needs_auth")
        self.assertIn("read:org", result["error"])
        self.assertNotIn(["auth", "setup-git"], self.commands())
        self.assertIsNone(common.get_github_token())

    def test_a_classic_token_without_the_scopes_gh_needs_is_refused_before_gh(self) -> None:
        classic = "ghp_classic0123456789012345678901234567"
        pat.validate_token.return_value = {**VALID, "scopes": "gist, workflow"}
        result = run(pat.connect(classic))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "needs_auth")
        self.assertEqual(result["code"], pat.MISSING_SCOPES)
        self.assertEqual(result["missing_scopes"], ["repo", "read:org"])
        self.assertIn("`repo` and `read:org` scopes", result["error"])
        self.assertIn(pat.TOKENS_PAGE, result["error"])
        self.assertNotIn(classic, result["error"])
        self.assertEqual(self.calls(), [])  # gh never saw it
        self.assertIsNone(common.get_github_token())

    def test_only_tokens_with_scopes_are_checked_and_org_scopes_include_read_org(self) -> None:
        classic = "ghp_" + "a" * 36
        cases = [
            (classic, "repo, read:org, gist", []),
            (classic, "repo, write:org", []),
            (classic, "repo, admin:org", []),
            (classic, "repo", ["read:org"]),
            (classic, "", ["repo", "read:org"]),
            ("0123456789abcdef0123456789abcdef01234567", "read:org", ["repo"]),  # old 40-hex classic
            ("gho_" + "a" * 36, "gist", ["repo", "read:org"]),
            ("github_pat_" + "a" * 40, "", []),  # fine-grained: no scopes to check
        ]
        for token, scopes, missing in cases:
            with self.subTest(token=token[:11], scopes=scopes):
                self.assertEqual(pat.missing_required_scopes(token, scopes), missing)

    def test_without_gh_the_pat_is_refused_before_validation(self) -> None:
        with patch.object(pat, "gh_available", return_value=False):
            result = run(pat.connect(TOKEN))

        self.assertFalse(result["ok"])
        self.assertIn("cli.github.com", result["error"])
        pat.validate_token.assert_not_awaited()
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.token_file.exists())

    def test_a_terminal_sign_in_is_the_connection(self) -> None:
        (self.gh_config / "hosts.yml").write_text("gho_terminal0123456789", encoding="utf-8")
        self.assertEqual(common.get_github_token(), "gho_terminal0123456789")
        self.assertEqual(common.get_github_auth_method(), "cli")

    def test_token_is_read_once_until_gh_state_changes(self) -> None:
        run(pat.connect(TOKEN))
        before = self.commands().count(["auth", "token"])
        common.get_github_token()
        common.get_github_token()
        self.assertEqual(self.commands().count(["auth", "token"]), before + 1)

        # Signing out in a terminal rewrites gh's state, and the connector sees it.
        (self.gh_config / "hosts.yml").unlink()
        self.assertIsNone(common.get_github_token())

    def test_a_gh_that_hangs_after_printing_the_token_leaks_it_nowhere(self) -> None:
        # The runner kills gh on the timeout but keeps what it already printed.
        (self.gh_config / "hosts.yml").write_text(TOKEN, encoding="utf-8")
        (self.gh_config / "hang").touch()
        with patch.object(common, "_GH_TOKEN_TIMEOUT_SECONDS", 0.5):
            with self.assertLogs(common.log, "WARNING") as logged:
                self.assertIsNone(common.get_github_token())
            with self.assertRaises(RuntimeError) as raised:
                common.get_github_token(read_only=True)

        self.assertIn("timed out", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn(TOKEN, "\n".join(logged.output))

    def test_a_hung_gh_is_asked_once_until_its_state_changes(self) -> None:
        (self.gh_config / "hosts.yml").write_text(TOKEN, encoding="utf-8")
        (self.gh_config / "hang").touch()
        with patch.object(common, "_GH_TOKEN_TIMEOUT_SECONDS", 0.5):
            with self.assertLogs(common.log, "WARNING") as logged:
                self.assertIsNone(common.get_github_token())
                self.assertIsNone(common.get_github_token())
            # Remembering the failure must not turn it into "not connected".
            with self.assertRaises(RuntimeError):
                common.get_github_token(read_only=True)
        self.assertEqual(self.commands().count(["auth", "token"]), 1)
        self.assertEqual(len(logged.output), 1)

        # A new sign-in is worth asking about at once, retry window or not.
        (self.gh_config / "hang").unlink()
        (self.gh_config / "hosts.yml").write_text(TOKEN, encoding="utf-8")
        self.assertEqual(common.get_github_token(), TOKEN)

    def test_a_hung_gh_is_asked_again_once_the_retry_window_passes(self) -> None:
        (self.gh_config / "hosts.yml").write_text(TOKEN, encoding="utf-8")
        (self.gh_config / "hang").touch()
        with patch.object(common, "_GH_TOKEN_TIMEOUT_SECONDS", 0.5), \
             patch.object(common, "_GH_TOKEN_RETRY_SECONDS", 0), \
             self.assertLogs(common.log, "WARNING"):
            self.assertIsNone(common.get_github_token())

        (self.gh_config / "hang").unlink()
        self.assertEqual(common.get_github_token(), TOKEN)
        self.assertEqual(self.commands().count(["auth", "token"]), 2)

    def test_a_timed_out_login_read_keeps_the_token_out_of_the_warning(self) -> None:
        # The runner kills gh on the timeout but keeps what it already printed.
        killed = CommandResult(argv=["gh"], returncode=-9, output=TOKEN, duration_seconds=10.0, timed_out=True)
        with patch.object(cli_auth, "run", AsyncMock(return_value=killed)), \
             self.assertLogs(cli_auth.log, "WARNING") as logged:
            self.assertIsNone(run(cli_auth._read_gh_token()))

        self.assertIn("timed out", "\n".join(logged.output))
        self.assertNotIn(TOKEN, "\n".join(logged.output))

    def test_the_token_gh_prints_never_reaches_the_command_log(self) -> None:
        # A classic PAT is 40 hex characters: no prefix for redaction to catch.
        classic = "0123456789abcdef0123456789abcdef01234567"
        (self.gh_config / "hosts.yml").write_text(classic, encoding="utf-8")
        command_log = self.tmp / "commands.log"
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG": "", "QUIRQ_COMMAND_LOG_PATH": str(command_log)}):
            self.assertEqual(common.get_github_token(), classic)
            self.assertEqual(run(cli_auth._read_gh_token()), classic)

        text = command_log.read_text(encoding="utf-8")
        self.assertEqual(text.count("gh auth token --hostname github.com"), 2)
        self.assertNotIn(classic, text)

    def test_device_flow_leaves_the_token_with_gh(self) -> None:
        (self.gh_config / "hosts.yml").write_text("gho_devicecode0123456789", encoding="utf-8")
        completed = {"status": "completed", "token": "gho_devicecode0123456789"}
        with patch.object(cli_auth, "poll_login", AsyncMock(return_value=completed)):
            result = run(cli_auth.connect("session"))

        self.assertTrue(result["ok"])
        self.assertFalse(self.token_file.exists())
        self.assertEqual(common.get_github_token(), "gho_devicecode0123456789")

    def test_disconnect_signs_gh_out_of_its_active_account(self) -> None:
        run(pat.connect(TOKEN))
        run(common.disconnect_github_account())

        logout = next(c for c in self.calls() if c["args"][:2] == ["auth", "logout"])
        self.assertEqual(logout["args"][2:], ["--hostname", "github.com", "--user", "octo"])
        self.assertIsNone(logout["env_token"])
        self.assertIsNone(common.get_github_token())
        self.assertFalse(self.token_file.exists())

    def test_disconnect_removes_what_connecting_wrote_to_gitconfig(self) -> None:
        subprocess.run(["git", "config", "--global", "alias.co", "checkout"], check=True)
        run(pat.connect(TOKEN))
        self.assertIn("name = Octo Cat", self.gitconfig_text())
        self.assertIn("helper = !gh auth git-credential", self.gitconfig_text())

        run(common.disconnect_github_account())

        # The user's own settings stay; no empty section is left behind.
        self.assertEqual(self.gitconfig_text(), "[alias]\n\tco = checkout\n")

    def test_the_next_account_to_connect_writes_its_own_identity(self) -> None:
        run(pat.connect(TOKEN))
        run(common.disconnect_github_account())
        pat.validate_token.return_value = dict(OTHER)
        run(pat.connect(TOKEN))

        text = self.gitconfig_text()
        self.assertIn("name = Hubot", text)
        self.assertIn("email = hubot@example.com", text)
        self.assertNotIn("Octo", text)
        self.assertIn("helper = !gh auth git-credential", text)

    def test_disconnecting_with_nothing_in_gitconfig_is_quiet(self) -> None:
        with self.assertNoLogs(common.log, "WARNING"):
            self.assertTrue(run(common.disconnect_github_account()))
        self.assertEqual(self.gitconfig_text(), "")

    # gh keeps several accounts per host: the connector must hold exactly one.

    def test_connecting_another_account_signs_gh_out_of_the_first(self) -> None:
        run(pat.connect(TOKEN))
        pat.validate_token.return_value = dict(OTHER)
        result = run(pat.connect(HUBOT_TOKEN))

        self.assertTrue(result["ok"])
        self.assertEqual(self.accounts(), {"hubot"})
        self.assertEqual(self.logouts(), [["--hostname", "github.com", "--user", "octo"]])
        self.assertEqual(common.get_github_token(), HUBOT_TOKEN)

    def test_a_rejected_replacement_keeps_the_current_connection(self) -> None:
        run(pat.connect(TOKEN))
        pat.validate_token.return_value = {**VALID, "scopes": "repo, read:org"}
        result = run(pat.connect("ghp_noscope0123456789012345678901234"))

        self.assertFalse(result["ok"])
        self.assertEqual(self.logouts(), [])
        self.assertEqual(common.get_github_token(), TOKEN)

    def test_device_flow_signs_gh_out_of_the_account_it_replaced(self) -> None:
        run(pat.connect(TOKEN))
        self.terminal_login(HUBOT_TOKEN)  # what the device flow leaves behind
        completed = {"status": "completed", "token": HUBOT_TOKEN}
        with patch.object(cli_auth, "poll_login", AsyncMock(return_value=completed)):
            result = run(cli_auth.connect("session"))

        self.assertTrue(result["ok"])
        self.assertEqual(self.accounts(), {"hubot"})
        self.assertEqual(common.get_github_token(), HUBOT_TOKEN)

    def test_disconnect_signs_gh_out_of_every_account(self) -> None:
        run(pat.connect(TOKEN))
        self.terminal_login(HUBOT_TOKEN)
        self.assertEqual(self.accounts(), {"octo", "hubot"})

        self.assertTrue(run(common.disconnect_github_account()))

        # Logging out only the active account would have made octo active again.
        self.assertEqual(self.accounts(), set())
        self.assertEqual(sorted(u[-1] for u in self.logouts()), ["hubot", "octo"])
        self.assertIsNone(common.get_github_token())

    def test_disconnect_reports_a_token_gh_keeps(self) -> None:
        run(pat.connect(TOKEN))
        (self.gh_config / "stuck").touch()  # gh refuses to log out
        with self.assertLogs(common.log, "WARNING"):
            self.assertFalse(run(common.disconnect_github_account()))
        self.assertEqual(common.get_github_token(), TOKEN)

    def test_without_an_account_list_gh_gets_the_bare_logout(self) -> None:
        # An older gh without `auth status --json`: one account still goes.
        run(pat.connect(TOKEN))
        failed = CommandResult(argv=["gh"], returncode=1, output="unknown flag: --json", duration_seconds=0.0)
        real_run = common.run

        async def no_json(argv, **kwargs):
            return failed if argv[1:3] == ["auth", "status"] else await real_run(argv, **kwargs)

        with patch.object(common, "run", no_json):
            self.assertTrue(run(common.disconnect_github_account()))
        self.assertEqual(self.logouts(), [["--hostname", "github.com"]])


class TokenRouteTests(unittest.TestCase):
    def post(self, refusal: dict):
        app = FastAPI()
        app.include_router(github_pat_routes.router)
        with patch.object(github_pat_routes.github_pat, "connect", AsyncMock(return_value=refusal)):
            return TestClient(app).post("/api/connectors/github/token", json={"token": "ghp_" + "a" * 36})

    def test_a_missing_scopes_refusal_reaches_the_client_as_a_code(self) -> None:
        res = self.post({"ok": False, "status": "needs_auth", "code": pat.MISSING_SCOPES,
                         "missing_scopes": ["read:org"], "error": "missing read:org"})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"status": "needs_auth", "error": "missing read:org",
                                      "code": "missing_scopes", "missing_scopes": ["read:org"]})

    def test_other_refusals_keep_their_body(self) -> None:
        res = self.post({"ok": False, "status": "needs_auth", "error": "Token is invalid or revoked."})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"status": "needs_auth", "error": "Token is invalid or revoked."})


class DisconnectRouteTests(unittest.TestCase):
    def post(self, signed_out: bool):
        app = FastAPI()
        app.include_router(github_pat_routes.router)
        with patch.object(github_pat_routes, "disconnect_github_account", AsyncMock(return_value=signed_out)):
            return TestClient(app).post("/api/connectors/github/disconnect")

    def test_signed_out_answers_needs_auth(self) -> None:
        res = self.post(True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "needs_auth"})

    def test_a_token_gh_still_holds_is_not_reported_as_signed_out(self) -> None:
        res = self.post(False)

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()["status"], "failed")
        self.assertIn("gh auth logout", res.json()["error"])


if __name__ == "__main__":
    unittest.main()
