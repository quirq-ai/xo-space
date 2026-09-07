from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import clone, config, git_ops, poller, status

R = "github.com/acme/trip-planner"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class CloneFunctionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root),
                                            "QUIRQ_STATE_ROOT": str(Path(self._tmp.name) / ".quirq")})
        self._env.start()
        self._auth = patch.object(clone, "_github_auth", new=AsyncMock(return_value=(None, False)))
        self._auth.start()

    def tearDown(self) -> None:
        self._auth.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_clones_into_hidden_temp_then_renames_into_place(self) -> None:
        seen = {}

        async def fake_clone(url, dest, *, config_args, cwd, timeout):
            seen.update(url=url, dest=Path(dest), config_args=config_args, cwd=Path(cwd))
            (Path(dest) / ".git").mkdir(parents=True)
            return True, "", False

        with patch.object(git_ops, "clone", new=fake_clone):
            res = run(clone.clone_shared_repo(R))
        self.assertEqual((res.state, res.project), ("cloned", "trip-planner"))
        self.assertEqual(seen["url"], "https://github.com/acme/trip-planner.git")
        self.assertEqual(seen["dest"].name, ".trip-planner.cloning")   # hidden while in flight
        self.assertEqual(seen["cwd"], self.root.resolve())
        self.assertTrue((self.root / "trip-planner" / ".git").is_dir())
        self.assertFalse(seen["dest"].exists())                        # renamed, not copied
        from services.cowork_agent.project_sharing import state
        self.assertIsNotNone(state.load_cloned_at(R))                  # remembered for the UI

    def test_existing_folder_with_same_origin_is_already(self) -> None:
        (self.root / "Trip-Planner" / ".git").mkdir(parents=True)  # capitalised on disk
        with patch.object(git_ops, "origin_url", new=AsyncMock(return_value="git@github.com:acme/Trip-Planner.git")), \
             patch.object(git_ops, "clone", new=AsyncMock()) as gclone:
            res = run(clone.clone_shared_repo(R))
        self.assertEqual((res.state, res.project), ("already", "Trip-Planner"))
        gclone.assert_not_called()

    def test_existing_folder_with_other_origin_is_exists(self) -> None:
        (self.root / "trip-planner" / ".git").mkdir(parents=True)
        with patch.object(git_ops, "origin_url", new=AsyncMock(return_value="https://github.com/other/thing")), \
             patch.object(git_ops, "clone", new=AsyncMock()) as gclone:
            res = run(clone.clone_shared_repo(R))
        self.assertEqual(res.state, "exists")
        gclone.assert_not_called()

    def test_failure_removes_temp_and_classifies_auth(self) -> None:
        async def fake_clone(url, dest, *, config_args, cwd, timeout):
            Path(dest).mkdir(parents=True)
            return False, "fatal: could not read Username for 'https://github.com': terminal prompts disabled", False

        with patch.object(git_ops, "clone", new=fake_clone):
            res = run(clone.clone_shared_repo(R))
        self.assertEqual(res.state, "needs_auth")
        self.assertFalse(res.had_token)
        self.assertFalse((self.root / ".trip-planner.cloning").exists())
        self.assertFalse((self.root / "trip-planner").exists())

    def test_refusals_split_on_whether_a_token_was_sent(self) -> None:
        # anonymous and refused: sign in.  token sent and still refused: that
        # account is not a collaborator.  anything else: a plain error.
        self.assertEqual(clone.classify_failure("remote: Repository not found.", had_token=False), "needs_auth")
        self.assertEqual(clone.classify_failure("remote: Repository not found.", had_token=True), "no_access")
        self.assertEqual(clone.classify_failure("fatal: Authentication failed for 'https://github.com/x/y.git/'", had_token=True), "no_access")
        self.assertEqual(clone.classify_failure("fatal: unable to access: Could not resolve host", had_token=True), "error")
        self.assertEqual(clone.classify_failure("fatal: unable to access: Could not resolve host", had_token=False), "error")

    def test_no_access_names_the_connected_account(self) -> None:
        class Auth:
            token = "ghp_secret"

        async def fake_clone(url, dest, *, config_args, cwd, timeout):
            Path(dest).mkdir(parents=True)
            return False, "remote: Repository not found.\nfatal: repository 'https://github.com/acme/trip-planner.git/' not found", False

        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(Auth(), True))), \
             patch.object(clone, "_github_login", new=AsyncMock(return_value="krishbhimani")), \
             patch.object(git_ops, "clone", new=fake_clone):
            res = run(clone.clone_shared_repo(R))
        self.assertEqual(res.state, "no_access")
        self.assertTrue(res.had_token)
        self.assertIn("connected as krishbhimani", res.detail)
        self.assertIn("cannot see this repo", res.detail)
        self.assertFalse((self.root / ".trip-planner.cloning").exists())

    def test_timeout_is_an_error_with_detail(self) -> None:
        with patch.object(git_ops, "clone", new=AsyncMock(return_value=(False, "", True))), \
             patch.dict(os.environ, {"PROJECT_SHARING_CLONE_TIMEOUT_SECONDS": "45"}):
            res = run(clone.clone_shared_repo(R))
        self.assertEqual(res.state, "error")
        self.assertIn("timed out after 45s", res.detail)

    def test_token_is_passed_as_a_header_config_for_github_only(self) -> None:
        class Auth:  # duck-typed GitHubAuth
            token = "ghp_secret"
        seen = {}

        async def fake_clone(url, dest, *, config_args, cwd, timeout):
            seen["config_args"] = config_args
            Path(dest).mkdir(parents=True)
            return True, "", False

        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(Auth(), True))), \
             patch.object(git_ops, "clone", new=fake_clone):
            res = run(clone.clone_shared_repo(R))
        self.assertEqual(res.state, "cloned")
        self.assertTrue(res.had_token)
        self.assertEqual(seen["config_args"][0], "-c")
        self.assertTrue(seen["config_args"][1].startswith("http.https://github.com/.extraheader=AUTHORIZATION: basic "))
        self.assertNotIn("ghp_secret", " ".join(seen["config_args"]))  # base64-encoded, never raw

        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(Auth(), True))), \
             patch.object(git_ops, "clone", new=fake_clone):
            run(clone.clone_shared_repo("gitlab.com/acme/other"))
        self.assertEqual(seen["config_args"], [])  # GitHub token is not sent elsewhere

    def test_cleanup_removes_only_cloning_temp_dirs(self) -> None:
        (self.root / ".x.cloning").mkdir()
        (self.root / ".keep").mkdir()
        (self.root / "real").mkdir()
        removed = clone.cleanup_stale_temp_dirs(self.root)
        self.assertEqual(removed, [".x.cloning"])
        self.assertTrue((self.root / ".keep").exists())
        self.assertTrue((self.root / "real").exists())


class AutoCloneInTickTests(unittest.TestCase):
    def setUp(self) -> None:
        status.reset()
        poller.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(Path(self._tmp.name) / ".quirq"),
            "XO_SPACE_ID": "ws-b", "PROJECT_SHARING_POLL_JITTER_RATIO": "0",
        })
        self._env.start()
        self._auth = patch.object(config, "auth_token", return_value="tok")
        self._auth.start()

    def tearDown(self) -> None:
        self._auth.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _tick(self, clone_result):
        with patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": [{"repo": R, "available": True}]})), \
             patch.object(clone, "clone_shared_repo", new=AsyncMock(return_value=clone_result)) as cl:
            delay = run(poller.run_tick())
        return delay, cl

    def test_available_repo_is_cloned_and_tick_drains(self) -> None:
        delay, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
        cl.assert_awaited_once_with(R)
        self.assertEqual(delay, poller.DRAIN_INTERVAL)
        snap = status.snapshot()["repos"][R]
        self.assertIsNone(snap["clone"])
        self.assertIn("cloned", [e["kind"] for e in status.snapshot()["recent"]])

    def test_kill_switch_prevents_cloning(self) -> None:
        with patch.dict(os.environ, {"PROJECT_SHARING_AUTO_CLONE": "false"}):
            _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
        cl.assert_not_called()

    def test_needs_auth_is_not_retried_until_a_token_appears(self) -> None:
        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(None, False))):
            _, cl = self._tick(clone.CloneResult("needs_auth", "trip-planner", "auth", had_token=False))
            cl.assert_awaited_once()
            _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
            cl.assert_not_called()                       # still no token: no retry
        self.assertEqual(status.snapshot()["repos"][R]["clone"]["state"], "needs_auth")
        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(object(), True))):
            _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
            cl.assert_awaited_once()                     # token appeared: retried

    def test_error_backs_off_exponentially(self) -> None:
        _, cl = self._tick(clone.CloneResult("error", "trip-planner", "boom"))
        cl.assert_awaited_once()
        c = status.snapshot()["repos"][R]["clone"]
        self.assertEqual(c["state"], "error")
        self.assertGreater(c["next_retry_at"], time.time() + 290)   # ~5 min
        _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
        cl.assert_not_called()                           # backoff not elapsed
        self.assertEqual(poller._clone_backoff(1), 300.0)
        self.assertEqual(poller._clone_backoff(3), 1200.0)
        self.assertEqual(poller._clone_backoff(9), 3600.0)

    def test_no_access_retries_on_backoff_not_on_token_presence(self) -> None:
        with patch.object(clone, "_github_auth", new=AsyncMock(return_value=(object(), True))):
            _, cl = self._tick(clone.CloneResult("no_access", "trip-planner", "cannot see", had_token=True))
            cl.assert_awaited_once()
            c = status.snapshot()["repos"][R]["clone"]
            self.assertEqual(c["state"], "no_access")
            self.assertIsNotNone(c["next_retry_at"])       # backed off like an error
            _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
            cl.assert_not_called()                          # a token being present is not the trigger

    def test_exists_retries_only_once_the_folder_is_gone(self) -> None:
        (self.root / "trip-planner").mkdir()
        _, cl = self._tick(clone.CloneResult("exists", "trip-planner", "other repo"))
        cl.assert_awaited_once()
        _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
        cl.assert_not_called()
        (self.root / "trip-planner").rmdir()
        _, cl = self._tick(clone.CloneResult("cloned", "trip-planner"))
        cl.assert_awaited_once()
