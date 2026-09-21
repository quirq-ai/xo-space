"""The GitHub connector's repository allowlist and the checks that honour it."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.connectors import token_store
from services.cowork_agent.connectors.github import (
    cli_auth,
    git_credential,
    issue_actions,
    issues,
    repo_access,
)
from services.cowork_agent.connectors.github.common import save_github_token


class RepoAccessTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(token_store, "TOKEN_FILE", Path(tmp.name) / "token.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class SelectionStoreTests(RepoAccessTestCase):
    def test_defaults_to_all_so_existing_connections_are_unchanged(self):
        self.assertEqual(repo_access.get_repo_access(), {"mode": "all", "repos": []})
        self.assertTrue(repo_access.is_repo_allowed("octo/anything"))
        save_github_token("gho_x", auth_method="cli")
        self.assertTrue(repo_access.is_repo_allowed("octo/anything"))

    def test_selected_allows_only_listed_repos_case_insensitively(self):
        save_github_token("gho_x", auth_method="cli")
        stored = repo_access.set_repo_access("selected", ["Octo/App", "octo/app", "octo/lib.git"])
        self.assertEqual(stored, {"mode": "selected", "repos": ["octo/app", "octo/lib"]})
        self.assertTrue(repo_access.is_repo_allowed("OCTO/app"))
        self.assertFalse(repo_access.is_repo_allowed("octo/secret"))
        self.assertFalse(repo_access.is_repo_allowed(None))

    def test_empty_selection_allows_nothing(self):
        save_github_token("gho_x", auth_method="cli")
        repo_access.set_repo_access("selected", [])
        self.assertFalse(repo_access.is_repo_allowed("octo/app"))

    def test_selection_keeps_the_token_and_a_new_token_resets_it(self):
        save_github_token("gho_x", auth_method="cli")
        repo_access.set_repo_access("selected", ["octo/app"])
        entry = token_store.get_entry("github")
        self.assertEqual(entry["access_token"], "gho_x")
        self.assertEqual(entry["auth_method"], "cli")
        save_github_token("gho_y", auth_method="cli")
        self.assertEqual(repo_access.get_repo_access()["mode"], "all")

    def test_rejects_bad_input_and_missing_connection(self):
        with self.assertRaises(repo_access.RepoAccessError):
            repo_access.set_repo_access("selected", ["octo/app"])  # not connected
        save_github_token("gho_x", auth_method="cli")
        for mode, repos in (("some", []), ("selected", ["not a slug"]), ("selected", ["a/b/c"])):
            with self.assertRaises(repo_access.RepoAccessError):
                repo_access.set_repo_access(mode, repos)


class EnforcementTests(RepoAccessTestCase):
    def test_issue_reads_refuse_an_unselected_repo_before_calling_gh(self):
        save_github_token("gho_x", auth_method="cli")
        repo_access.set_repo_access("selected", ["octo/app"])
        with mock.patch.object(issues, "run_graphql") as gql, \
                mock.patch.object(issue_actions, "run_graphql") as gql_one:
            many = asyncio.run(issues.fetch_open_issues("octo/secret"))
            one = asyncio.run(issue_actions.fetch_issue("octo/secret", 1))
        gql.assert_not_called()
        gql_one.assert_not_called()
        self.assertEqual((many.ok, many.error_kind), (False, "forbidden"))
        self.assertEqual((one.ok, one.error_kind), (False, "forbidden"))


class CliLoginTests(RepoAccessTestCase):
    def _connect(self, mode):
        poll = {"status": "completed", "token": "gho_x", "repo_access": mode}
        validation = {"valid": True, "username": "octo"}
        with mock.patch.object(cli_auth, "poll_login", mock.AsyncMock(return_value=poll)), \
                mock.patch.object(cli_auth, "validate_token", mock.AsyncMock(return_value=validation)), \
                mock.patch.object(cli_auth, "configure_git_identity", mock.AsyncMock()), \
                mock.patch.object(cli_auth, "apply_git_policy", mock.AsyncMock()):
            return asyncio.run(cli_auth.connect("sid"))

    def test_selected_login_lands_with_nothing_allowed(self):
        result = self._connect("selected")
        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["repo_access"], {"mode": "selected", "repos": []})
        self.assertFalse(repo_access.is_repo_allowed("octo/app"))

    def test_all_login_allows_everything(self):
        result = self._connect("all")
        self.assertEqual(result["payload"]["repo_access"]["mode"], "all")
        self.assertTrue(repo_access.is_repo_allowed("octo/app"))

    def test_start_rejects_an_unknown_mode(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(cli_auth.start_login(repo_access="some"))


class GitCredentialTests(RepoAccessTestCase):
    def _ask(self, path, host="github.com"):
        return git_credential.answer({"protocol": "https", "host": host, "path": path})[0]

    def test_helper_gives_the_token_only_for_selected_repos(self):
        save_github_token("gho_x", auth_method="cli")
        repo_access.set_repo_access("selected", ["octo/app"])
        self.assertEqual(self._ask("Octo/App.git")["password"], "gho_x")
        self.assertEqual(self._ask("octo/app.git/info/lfs")["password"], "gho_x")
        # Refused outright: no fall-through to another helper or a prompt.
        self.assertEqual(self._ask("octo/secret.git"), {"quit": "true"})
        self.assertEqual(self._ask(""), {"quit": "true"})
        self.assertEqual(self._ask("octo/app.git", host="gist.github.com"), {"quit": "true"})

    def test_helper_ignores_other_hosts_and_a_missing_token(self):
        self.assertEqual(self._ask("octo/app.git"), {})
        save_github_token("gho_x", auth_method="cli")
        self.assertEqual(self._ask("octo/app.git", host="gitlab.com"), {})

    def test_policy_installs_and_removes_the_helper_in_gitconfig(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        gitconfig = Path(home.name) / ".gitconfig"
        env = mock.patch.dict("os.environ", {"HOME": home.name, "GIT_CONFIG_GLOBAL": str(gitconfig)})
        # No gh here: the test must never log a developer's real session out.
        which = mock.patch.object(
            git_credential.shutil, "which",
            side_effect=lambda name: "/usr/bin/git" if name == "git" else None,
        )
        with env, which:
            save_github_token("gho_x", auth_method="cli")
            repo_access.set_repo_access("selected", ["octo/app"])
            asyncio.run(git_credential.apply_policy())
            installed = gitconfig.read_text()
            repo_access.set_repo_access("all")
            asyncio.run(git_credential.apply_policy())
            removed = gitconfig.read_text()
        self.assertIn("github_git_credential.py", installed)
        self.assertIn("useHttpPath = true", installed)
        self.assertNotIn("github_git_credential.py", removed)


if __name__ == "__main__":
    unittest.main()
