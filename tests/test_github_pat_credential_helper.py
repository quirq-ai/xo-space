"""The git credential helper a pasted GitHub PAT installs.

Runs real git against a throwaway global gitconfig, and asks git itself
(`git credential fill`) what it would authenticate github.com with, so the
helper's shell quoting is exercised exactly as git runs it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.connectors.github import common

KEY = common.GITHUB_CREDENTIAL_KEY
TOKEN = "github_pat_test0123456789"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipUnless(shutil.which("git") and shutil.which("python3"), "git and python3 are required")
class PatCredentialHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # A space in the path proves the token-file argument is quoted.
        self.tmp = Path(self._tmp.name) / "state dir"
        self.tmp.mkdir()
        self.gitconfig = self.tmp / "gitconfig"
        self.gitconfig.touch()
        self.token_file = self.tmp / "token.json"
        self._env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
        })
        self._env.start()
        self._token_file = patch.object(common, "TOKEN_FILE", self.token_file)
        self._token_file.start()

    def tearDown(self) -> None:
        self._token_file.stop()
        self._env.stop()
        self._tmp.cleanup()

    def git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], input=stdin, capture_output=True, text=True)

    def helpers(self) -> list[str]:
        return self.git("config", "--global", "--get-all", KEY).stdout.splitlines()

    def fill(self) -> str:
        """What git would send to github.com; empty when it has nothing."""
        res = self.git("credential", "fill", stdin="protocol=https\nhost=github.com\n\n")
        return res.stdout if res.returncode == 0 else ""

    def store_token(self, token: str | None) -> None:
        entry = {"access_token": token, "auth_method": "pat"}
        self.token_file.write_text(json.dumps({"github": entry}), encoding="utf-8")

    def test_git_authenticates_with_the_stored_token(self) -> None:
        self.store_token(TOKEN)
        run(common.configure_pat_credential_helper())

        out = self.fill()
        self.assertIn("username=x-access-token", out)
        self.assertIn(f"password={TOKEN}", out)
        # The secret is read at request time, never written into git config.
        self.assertNotIn(TOKEN, self.gitconfig.read_text(encoding="utf-8"))

    def test_rotated_token_is_picked_up_without_reconfiguring(self) -> None:
        self.store_token(TOKEN)
        run(common.configure_pat_credential_helper())
        self.store_token("github_pat_rotated")
        self.assertIn("password=github_pat_rotated", self.fill())

    def test_missing_token_yields_nothing(self) -> None:
        run(common.configure_pat_credential_helper())
        self.assertEqual(self.fill(), "")
        self.store_token(None)
        self.assertEqual(self.fill(), "")

    def test_reconnect_does_not_duplicate_the_entry(self) -> None:
        run(common.configure_pat_credential_helper())
        run(common.configure_pat_credential_helper())
        self.assertEqual(self.helpers(), [common.pat_credential_helper()])

    def test_existing_foreign_helper_is_left_alone(self) -> None:
        self.git("config", "--global", KEY, "!gh auth git-credential")
        run(common.configure_pat_credential_helper())
        self.assertEqual(self.helpers(), ["!gh auth git-credential"])

    def test_remove_deletes_only_our_entry(self) -> None:
        run(common.configure_pat_credential_helper())
        self.git("config", "--global", "--add", KEY, "!gh auth git-credential")
        run(common.remove_pat_credential_helper())
        self.assertEqual(self.helpers(), ["!gh auth git-credential"])

    def test_remove_without_our_entry_is_a_no_op(self) -> None:
        run(common.remove_pat_credential_helper())
        self.assertEqual(self.helpers(), [])


if __name__ == "__main__":
    unittest.main()
