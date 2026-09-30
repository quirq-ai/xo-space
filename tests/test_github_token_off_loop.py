"""The issue poller reads the GitHub token off the event loop.

`get_github_token()` can wait on `gh auth token` for its whole timeout, and the
poller, like every `gh api` call, runs on the server's event loop. So both
places that read the token on the poller's path do it in a worker thread, and
a hung gh slows the poller down instead of stalling every request.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent import github_poller
from services.cowork_agent.connectors.github import issues
from utils.commands import CommandResult

TOKEN = "ghp_" + "a" * 36
OTHER = "ghp_" + "b" * 36


class TokenReadsLeaveTheEventLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        env[github_poller.ENV_AUTH_DETECT] = "1"
        for p in (
            patch.dict(os.environ, env, clear=True),
            # No real gh session on this machine may count as a credential.
            patch.object(github_poller, "gh_hosts_file", return_value=Path(tmp.name) / "hosts.yml"),
        ):
            p.start()
            self.addCleanup(p.stop)
        github_poller.reset_state()
        self.addCleanup(github_poller.reset_state)
        self.reader_threads: list[int] = []

    def read_token(self, token: str = TOKEN):
        def read(*_args, **_kwargs):
            self.reader_threads.append(threading.get_ident())
            return token
        return read

    def test_a_gh_api_call_reads_the_token_off_the_loop(self) -> None:
        answered = CommandResult(argv=["gh"], returncode=0, output="{}", duration_seconds=0.0)

        async def call() -> int:
            await issues._run_gh(["gh", "api", "user"], 5)
            return threading.get_ident()

        with patch.object(issues, "get_github_token", side_effect=self.read_token()), \
             patch.object(issues, "run", AsyncMock(return_value=answered)) as run_gh:
            loop_thread = asyncio.run(call())

        self.assertEqual(len(self.reader_threads), 1)
        self.assertNotEqual(self.reader_threads[0], loop_thread)
        self.assertEqual(run_gh.await_args.kwargs["env"]["GH_TOKEN"], TOKEN)

    def test_the_poller_reads_the_credential_state_off_the_loop(self) -> None:
        async def tick() -> int:
            await github_poller.detect_auth_change()
            return threading.get_ident()

        with patch.object(github_poller, "get_github_token", side_effect=self.read_token()):
            loop_thread = asyncio.run(tick())

        self.assertEqual(len(self.reader_threads), 1)
        self.assertNotEqual(self.reader_threads[0], loop_thread)

    def test_a_changed_credential_still_clears_the_backoff(self) -> None:
        async def two_ticks() -> tuple[bool, bool]:
            with patch.object(github_poller, "get_github_token", side_effect=self.read_token()):
                first = await github_poller.detect_auth_change()
            github_poller._cooldowns["octo/repo"] = float("inf")
            with patch.object(github_poller, "get_github_token", side_effect=self.read_token(OTHER)):
                second = await github_poller.detect_auth_change()
            return first, second

        first, second = asyncio.run(two_ticks())

        self.assertFalse(first)  # the first look is the baseline, not a change
        self.assertTrue(second)
        self.assertNotIn("octo/repo", github_poller._cooldowns)


if __name__ == "__main__":
    unittest.main()
