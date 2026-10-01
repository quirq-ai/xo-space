"""git_ops bounds every git call: a hung network or lock must not stall the
relay, and a missing credential fails at once instead of waiting on a prompt."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import git_ops
from utils.commands import CommandResult


def result(code=0, out="", *, err="", timed_out=False):
    return CommandResult(argv=[], returncode=code, output=out, duration_seconds=0,
                         timed_out=timed_out, stderr=err)


class GitOpsTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, value):
        mock = AsyncMock(return_value=value)
        p = patch.object(git_ops, "run", mock)
        p.start()
        self.addCleanup(p.stop)
        return mock

    async def test_fetch_is_bounded_and_never_prompts(self):
        run = self.runner(result())
        self.assertEqual(await git_ops.fetch_origin("/repo"), (True, ""))
        kwargs = run.await_args.kwargs
        self.assertEqual(kwargs["timeout"], git_ops.FETCH_TIMEOUT)
        self.assertEqual(git_ops.FETCH_TIMEOUT, 300.0)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    async def test_ls_remote_is_bounded_and_never_prompts(self):
        run = self.runner(result(out="abc123\trefs/heads/main\n"))
        self.assertEqual(await git_ops.remote_head("/repo", "main"), "abc123")
        kwargs = run.await_args.kwargs
        self.assertEqual(kwargs["timeout"], git_ops.LS_REMOTE_TIMEOUT)
        self.assertEqual(git_ops.LS_REMOTE_TIMEOUT, 60.0)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    async def test_local_calls_are_bounded_and_keep_the_environment(self):
        run = self.runner(result(out="https://github.com/acme/app.git\n"))
        await git_ops.origin_url("/repo")
        await git_ops.commit_present("/repo", "a" * 40)
        await git_ops.behind_count("/repo", "main")
        for call in run.await_args_list:
            self.assertEqual(call.kwargs["timeout"], git_ops.LOCAL_TIMEOUT)
            self.assertIsNone(call.kwargs.get("env"))
        self.assertEqual(git_ops.LOCAL_TIMEOUT, 60.0)

    async def test_timed_out_fetch_is_a_named_failure(self):
        self.runner(result(-9, timed_out=True))
        ok, err = await git_ops.fetch_origin("/repo")
        self.assertFalse(ok)
        self.assertIn("timed out after 300s", err)

    async def test_timed_out_ls_remote_and_local_reads_degrade(self):
        self.runner(result(-9, out="abc123\trefs/heads/main\n", timed_out=True))
        self.assertIsNone(await git_ops.remote_head("/repo", "main"))
        self.assertIsNone(await git_ops.origin_url("/repo"))
        self.assertFalse(await git_ops.commit_present("/repo", "a" * 40))
        self.assertIsNone(await git_ops.behind_count("/repo", "main"))


if __name__ == "__main__":
    unittest.main()
