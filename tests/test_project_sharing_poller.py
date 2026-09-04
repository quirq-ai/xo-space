from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import config, poller, state, status, watcher

R = "github.com/acme/trip-planner"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class PollerTickTests(unittest.TestCase):
    def setUp(self) -> None:
        status.reset()
        poller.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clone = self.root / "projects" / "trip-planner"
        (self.clone / ".git").mkdir(parents=True)
        self._env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.root / ".quirq"),
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
            "XO_PROJECT_ID": "ws-bbb",
            "PROJECT_SHARING_ENABLED": "true",
            "PROJECT_SHARING_POLL_INTERVAL_SECONDS": "60",
            "PROJECT_SHARING_POLL_JITTER_RATIO": "0",
        })
        self._env.start()
        self._auth = patch.object(config, "auth_token", return_value="tok")
        self._auth.start()

    def tearDown(self) -> None:
        self._auth.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_parked_when_no_auth_makes_no_network_call(self) -> None:
        with patch.object(config, "auth_token", return_value=None):
            with patch.object(poller.swarm_client, "poll", new=AsyncMock()) as p:
                delay = run(poller.run_tick())
        p.assert_not_called()
        self.assertEqual(status.snapshot()["reason"], "no_auth")
        self.assertEqual(delay, 60.0)

    def test_parked_when_no_workspace_id(self) -> None:
        with patch.dict(os.environ, {"XO_PROJECT_ID": ""}):
            with patch.object(poller.swarm_client, "poll", new=AsyncMock()) as p:
                run(poller.run_tick())
        p.assert_not_called()
        self.assertEqual(status.snapshot()["reason"], "no_workspace_id")

    def test_parked_when_disabled(self) -> None:
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "false"}):
            with patch.object(poller.swarm_client, "poll", new=AsyncMock()) as p:
                run(poller.run_tick())
        p.assert_not_called()
        self.assertEqual(status.snapshot()["reason"], "disabled")

    def test_cursor_advances_only_to_highest_present_commit(self) -> None:
        events = [{"seq": 41, "commit": "c1"}, {"seq": 42, "commit": "c2"}, {"seq": 43, "commit": "c3"}]
        present = {"c1", "c2"}
        with patch.object(poller.git_ops, "origin_url", new=AsyncMock(return_value="git@github.com:acme/trip-planner.git")), \
             patch.object(poller.git_ops, "fetch_origin", new=AsyncMock(return_value=(True, ""))), \
             patch.object(poller.git_ops, "commit_present", new=AsyncMock(side_effect=lambda d, sha: sha in present)), \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": [{"repo": R, "events": events, "has_more": False}]})), \
             patch.object(watcher, "run_tick_repo", new=AsyncMock(return_value="noop")):
            delay = run(poller.run_tick())
        self.assertEqual(state.load_cursor(R), 42)
        self.assertEqual(state.load_last_reported(R), "c2")
        self.assertEqual(delay, poller.DRAIN_INTERVAL)  # one event not present -> drain
        self.assertEqual([e["kind"] for e in status.snapshot()["recent"]], ["fetched"])

    def test_available_repo_is_recorded_once_and_not_fetched(self) -> None:
        with patch.object(poller.git_ops, "origin_url", new=AsyncMock(return_value="https://github.com/acme/other")), \
             patch.object(poller.git_ops, "fetch_origin", new=AsyncMock()) as fetch, \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": [{"repo": R, "available": True}]})), \
             patch.object(watcher, "run_tick_repo", new=AsyncMock(return_value="noop")):
            run(poller.run_tick())
            run(poller.run_tick())
        fetch.assert_not_called()
        self.assertEqual([e["kind"] for e in status.snapshot()["recent"]], ["shared_with_you"])

    def test_ledger_delivered_commits_are_not_re_reported(self) -> None:
        # Fetch step writes last_reported before publish runs, so the publish
        # step sees remote == last_reported and stays quiet.
        events = [{"seq": 41, "commit": "c1"}]
        seen = {}

        async def fake_publish(ws, repo, repo_dir, branch):
            seen["last"] = state.load_last_reported(repo)
            return "noop"

        with patch.object(poller.git_ops, "origin_url", new=AsyncMock(return_value="https://github.com/acme/trip-planner")), \
             patch.object(poller.git_ops, "fetch_origin", new=AsyncMock(return_value=(True, ""))), \
             patch.object(poller.git_ops, "commit_present", new=AsyncMock(return_value=True)), \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": [{"repo": R, "events": events, "has_more": False}]})), \
             patch.object(watcher, "run_tick_repo", new=fake_publish):
            run(poller.run_tick())
        self.assertEqual(seen["last"], "c1")

    def test_on_change_fires_at_end_of_tick(self) -> None:
        calls = []
        status.on_change(lambda: calls.append(1))
        with patch.object(poller.git_ops, "origin_url", new=AsyncMock(return_value="https://github.com/acme/trip-planner")), \
             patch.object(poller.swarm_client, "poll", new=AsyncMock(return_value={"repos": []})), \
             patch.object(watcher, "run_tick_repo", new=AsyncMock(return_value="noop")):
            run(poller.run_tick())
            run(poller.run_tick())
        self.assertEqual(len(calls), 1)


class WatcherPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self._tmp.name) / "repo"
        (self.repo_dir / ".git").mkdir(parents=True)
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(Path(self._tmp.name) / ".quirq")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_first_sighting_only_baselines(self) -> None:
        with patch.object(watcher.git_ops, "remote_head", new=AsyncMock(return_value="c0")), \
             patch.object(watcher.swarm_client, "report_commits", new=AsyncMock()) as rep:
            action = run(watcher.run_tick_repo("ws-a", R, self.repo_dir, "main"))
        self.assertEqual(action, "baseline")
        rep.assert_not_called()
        self.assertEqual(state.load_last_reported(R), "c0")

    def test_local_ref_fast_path_skips_ls_remote(self) -> None:
        state.save_last_reported(R, "c0")
        with patch.object(watcher.git_ops, "local_remote_head", return_value="c2"), \
             patch.object(watcher.git_ops, "commit_present", new=AsyncMock(return_value=True)), \
             patch.object(watcher.git_ops, "remote_head", new=AsyncMock()) as ls, \
             patch.object(watcher.git_ops, "enumerate_hashes", new=AsyncMock(return_value=["c1", "c2"])), \
             patch.object(watcher.swarm_client, "report_commits", new=AsyncMock(return_value=True)) as rep:
            action = run(watcher.run_tick_repo("ws-a", R, self.repo_dir, "main"))
        self.assertEqual(action, "reported")
        ls.assert_not_called()
        rep.assert_awaited_once_with(R, "ws-a", ["c1", "c2"])
        self.assertEqual(state.load_last_reported(R), "c2")

    def test_failed_report_keeps_marker_for_retry(self) -> None:
        state.save_last_reported(R, "c0")
        with patch.object(watcher.git_ops, "local_remote_head", return_value=None), \
             patch.object(watcher.git_ops, "remote_head", new=AsyncMock(return_value="c1")), \
             patch.object(watcher.git_ops, "commit_present", new=AsyncMock(return_value=True)), \
             patch.object(watcher.git_ops, "enumerate_hashes", new=AsyncMock(return_value=["c1"])), \
             patch.object(watcher.swarm_client, "report_commits", new=AsyncMock(return_value=False)):
            action = run(watcher.run_tick_repo("ws-a", R, self.repo_dir, "main"))
        self.assertEqual(action, "report_failed")
        self.assertEqual(state.load_last_reported(R), "c0")
