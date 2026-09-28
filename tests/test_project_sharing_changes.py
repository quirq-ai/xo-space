"""One commit's changes for the Sharing page: file list with +/- counts and a
diff preview, read from real git. Only commits on origin/<branch> are shown."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.project_sharing import git_ops, service


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class NumstatParseTests(unittest.TestCase):
    def test_parses_text_binary_and_skips_junk(self) -> None:
        out = "\n1\t0\ta.txt\x00-\t-\tb.bin\x00junk\x003\t2\tdir/c d.md\x00"
        self.assertEqual(git_ops.parse_numstat_z(out), [
            {"path": "a.txt", "additions": 1, "deletions": 0, "binary": False},
            {"path": "b.bin", "additions": None, "deletions": None, "binary": True},
            {"path": "dir/c d.md", "additions": 3, "deletions": 2, "binary": False},
        ])


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitRepoCase(unittest.TestCase):
    """A throwaway repo whose origin/main is set by update-ref, standing in
    for the relay's fetch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        self._env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "PROJECT_SHARING_WATCH_BRANCH": "main",
        })
        self._env.start()
        self.repo = self.tmp / "trip-planner"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        self.root = self.commit({"README.md": "hello\n"}, "root")
        self.second = self.commit({"README.md": "hello\nworld\n",
                                   "notes with space.md": "a\nb\n",
                                   "logo.bin": b"\x00\x01\x02"}, "second")
        self.publish()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def commit(self, files: dict, message: str) -> str:
        for name, body in files.items():
            target = self.repo / name
            if isinstance(body, bytes):
                target.write_bytes(body)
            else:
                target.write_text(body, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def publish(self) -> None:
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")


class GitOpsChangesTests(GitRepoCase):
    def test_files_of_a_commit_with_counts(self) -> None:
        parent = run(git_ops.first_parent(self.repo, self.second))
        self.assertEqual(parent, self.root)
        files = {f["path"]: f for f in run(git_ops.commit_files(self.repo, self.second, parent))}
        self.assertEqual(files["README.md"]["additions"], 1)
        self.assertEqual(files["README.md"]["deletions"], 0)
        self.assertEqual(files["notes with space.md"]["additions"], 2)
        self.assertTrue(files["logo.bin"]["binary"])

    def test_root_commit_has_no_parent_and_still_lists_files(self) -> None:
        self.assertIsNone(run(git_ops.first_parent(self.repo, self.root)))
        files = run(git_ops.commit_files(self.repo, self.root, None))
        self.assertEqual(files, [{"path": "README.md", "additions": 1, "deletions": 0, "binary": False}])

    def test_diff_of_one_path_with_a_space_in_its_name(self) -> None:
        text = run(git_ops.commit_diff(self.repo, self.second, self.root, "notes with space.md"))
        self.assertIn("+a", text.splitlines())
        self.assertNotIn("README", text)

    def test_merge_commit_is_read_against_its_first_parent(self) -> None:
        git(self.repo, "checkout", "--quiet", "-b", "feature")
        self.commit({"feature.txt": "f\n"}, "feature")
        git(self.repo, "checkout", "--quiet", "main")
        self.commit({"other.txt": "o\n"}, "other")
        git(self.repo, "merge", "--no-ff", "--quiet", "-m", "merge feature", "feature")
        merge = git(self.repo, "rev-parse", "HEAD")
        parent = run(git_ops.first_parent(self.repo, merge))
        files = run(git_ops.commit_files(self.repo, merge, parent))
        self.assertEqual([f["path"] for f in files], ["feature.txt"])

    def test_reachability_from_origin(self) -> None:
        self.assertTrue(run(git_ops.on_branch(self.repo, self.second, "main")))
        local = self.commit({"local.txt": "x\n"}, "local only")
        self.assertFalse(run(git_ops.on_branch(self.repo, local, "main")))
        self.assertIsNone(run(git_ops.resolve_commit(self.repo, "0" * 40)))
        self.assertEqual(run(git_ops.resolve_commit(self.repo, self.second[:9])), self.second)


if __name__ == "__main__":
    unittest.main()
