"""Git-backed self-update (Setup tab · 04 Version).

Exercises the real git flows against throwaway repos: a local "origin" plays
the remote, a clone plays the installed checkout, and REPO_ROOT is pointed at
the clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent import self_update


def _git_env(home: str) -> dict:
    git_bin = shutil.which("git") or "/usr/bin/git"
    return {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": home,
        "PATH": os.path.dirname(git_bin) + ":/usr/bin:/bin:/usr/local/bin",
    }


class SelfUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.origin = tmp / "origin"
        self.work = tmp / "work"
        self.env = _git_env(self._tmp.name)
        self.origin.mkdir()
        self._run("init", "-q", cwd=self.origin)
        (self.origin / "requirements.txt").write_text("a==1\n", encoding="utf-8")
        (self.origin / "code.py").write_text("x=1\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.origin)
        self._run("commit", "-q", "-m", "first", cwd=self.origin)
        self._run("clone", "-q", str(self.origin), str(self.work), cwd=tmp)
        self._orig_root = self_update.REPO_ROOT
        self_update.REPO_ROOT = self.work

    def tearDown(self) -> None:
        self_update.REPO_ROOT = self._orig_root
        self._tmp.cleanup()

    def _run(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       capture_output=True, env=self.env)

    def _commit_upstream(self, message: str, requirements: bool = False) -> None:
        target = "requirements.txt" if requirements else "code.py"
        path = self.origin / target
        path.write_text(path.read_text(encoding="utf-8") + "#\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.origin)
        self._run("commit", "-q", "-m", message, cwd=self.origin)

    def test_up_to_date_status_and_noop_apply(self) -> None:
        status = self_update.check_update_status()
        self.assertTrue(status["supported"])
        self.assertTrue(status["fetch_ok"])
        self.assertTrue(status["up_to_date"])
        self.assertEqual(status["behind"], 0)
        self.assertFalse(status["dirty"])
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "up_to_date")

    def test_behind_remote_then_fast_forward(self) -> None:
        self._commit_upstream("bump requirements", requirements=True)
        status = self_update.check_update_status()
        self.assertFalse(status["up_to_date"])
        self.assertEqual(status["behind"], 1)
        self.assertNotEqual(status["latest"]["sha"], status["current"]["sha"])
        result = self_update.apply_update()
        self.assertTrue(result["updated"])
        self.assertEqual(result["commits"], 1)
        self.assertTrue(result["requirements_changed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["to"]["sha"], status["latest"]["sha"])
        self.assertTrue(self_update.check_update_status()["up_to_date"])

    def test_dirty_tree_refuses(self) -> None:
        self._commit_upstream("newer")
        (self.work / "local-edit.txt").write_text("wip", encoding="utf-8")
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "dirty_tree")

    def test_diverged_refuses(self) -> None:
        self._commit_upstream("remote work")
        (self.work / "code.py").write_text("x=2\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.work)
        self._run("commit", "-q", "-m", "local work", cwd=self.work)
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "diverged")

    def test_non_checkout_is_unsupported(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        self_update.REPO_ROOT = plain
        status = self_update.check_update_status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["reason"], "not_a_git_checkout")
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "not_a_git_checkout")


class ReleaseChannelTests(unittest.TestCase):
    """main and tag checkouts follow release tags, never the main tip, and
    only ever move forward (the installer's fetch_repo applies the same
    rule; tests/install_sh_harness.sh covers that side)."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.origin = tmp / "origin"
        self.env = _git_env(self._tmp.name)
        self.origin.mkdir()
        self._run("init", "-q", "-b", "main", cwd=self.origin)
        (self.origin / "requirements.txt").write_text("a==1\n", encoding="utf-8")
        self._commit("first", "x=1\n")
        self._tag("v1.9.0")
        self.v110 = self._commit("second", "x=2\n")
        self._tag("v1.10.0")
        self._commit("third", "x=3\n")
        self._tag("v2.0.0-rc1")  # a pre-release is never a target
        self.tip = self._commit("main tip", "x=4\n")
        self._orig_root = self_update.REPO_ROOT

    def tearDown(self) -> None:
        self_update.REPO_ROOT = self._orig_root
        self._tmp.cleanup()

    def _run(self, *args: str, cwd: Path) -> str:
        return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                              capture_output=True, text=True, env=self.env).stdout.strip()

    def _commit(self, message: str, code: str, requirements: bool = False) -> str:
        (self.origin / "code.py").write_text(code, encoding="utf-8")
        if requirements:
            path = self.origin / "requirements.txt"
            path.write_text(path.read_text(encoding="utf-8") + "b==1\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.origin)
        self._run("commit", "-q", "-m", message, cwd=self.origin)
        return self._run("rev-parse", "HEAD", cwd=self.origin)

    def _tag(self, name: str) -> None:
        self._run("tag", "-a", name, "-m", name, cwd=self.origin)

    def _checkout(self, name: str, *clone_args: str) -> Path:
        work = Path(self._tmp.name) / name
        self._run("clone", "-q", *clone_args, f"file://{self.origin}", str(work),
                  cwd=Path(self._tmp.name))
        self_update.REPO_ROOT = work
        return work

    def _head(self, work: Path) -> str:
        return self._run("rev-parse", "HEAD", cwd=work)

    def test_tag_install_moves_to_the_next_release_not_the_tip(self) -> None:
        work = self._checkout("tagged", "--depth", "1", "--branch", "v1.10.0")
        status = self_update.check_update_status()
        self.assertTrue(status["supported"])
        self.assertEqual(status["channel"], "release")
        self.assertIsNone(status["branch"])
        self.assertEqual(status["current_tag"], "v1.10.0")
        self.assertTrue(status["up_to_date"])

        v111 = self._commit("fix", "x=5\n", requirements=True)
        self._tag("v1.11.0")
        self._commit("after the release", "x=6\n")
        status = self_update.check_update_status()
        self.assertEqual(status["latest_tag"], "v1.11.0")
        self.assertFalse(status["up_to_date"])
        self.assertGreater(status["behind"], 0)

        result = self_update.apply_update()
        self.assertTrue(result["updated"])
        self.assertEqual(result["tag"], "v1.11.0")
        self.assertTrue(result["requirements_changed"])
        self.assertEqual(self._head(work), v111)
        self.assertEqual(self._run("rev-parse", "--abbrev-ref", "HEAD", cwd=work), "HEAD")
        self.assertTrue(self_update.check_update_status()["up_to_date"])

    def test_main_tip_ahead_of_the_release_is_never_moved_back(self) -> None:
        for name, args in (("full", ()), ("shallow", ("--depth", "1"))):
            with self.subTest(name):
                work = self._checkout(name, *args)
                status = self_update.check_update_status()
                self.assertEqual(status["channel"], "release")
                self.assertEqual(status["latest_tag"], "v1.10.0")
                self.assertTrue(status["up_to_date"])
                result = self_update.apply_update()
                self.assertFalse(result["updated"])
                self.assertEqual(result["reason"], "up_to_date")
                self.assertEqual(self._head(work), self.tip)

    def test_main_behind_a_release_fast_forwards_to_it_and_stays_on_main(self) -> None:
        work = self._checkout("onmain")
        self._run("reset", "-q", "--hard", self.v110, cwd=work)
        v111 = self._commit("fix", "x=5\n")
        self._tag("v1.11.0")
        self._commit("after the release", "x=6\n")
        result = self_update.apply_update()
        self.assertTrue(result["updated"])
        self.assertEqual(self._head(work), v111)
        self.assertEqual(self._run("rev-parse", "--abbrev-ref", "HEAD", cwd=work), "main")

    def test_local_commits_on_main_are_reported_as_diverged(self) -> None:
        work = self._checkout("local")
        self._run("reset", "-q", "--hard", self.v110, cwd=work)
        (work / "code.py").write_text("mine\n", encoding="utf-8")
        self._run("add", "-A", cwd=work)
        self._run("commit", "-q", "-m", "local work", cwd=work)
        self._commit("fix", "x=5\n")
        self._tag("v1.11.0")
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "diverged")

    def test_other_branches_follow_their_own_tip(self) -> None:
        self._run("checkout", "-q", "-b", "development", cwd=self.origin)
        work = self._checkout("dev", "--branch", "development")
        dev_tip = self._commit("dev work", "x=dev\n")
        status = self_update.check_update_status()
        self.assertEqual(status["channel"], "branch")
        self.assertEqual(status["branch"], "development")
        result = self_update.apply_update()
        self.assertTrue(result["updated"])
        self.assertEqual(self._head(work), dev_tip)

    def test_detached_without_release_tags_is_unsupported(self) -> None:
        for tag in ("v1.9.0", "v1.10.0", "v2.0.0-rc1"):
            self._run("tag", "-d", tag, cwd=self.origin)
        work = self._checkout("detached")
        self._run("checkout", "-q", "--detach", "HEAD", cwd=work)
        status = self_update.check_update_status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["reason"], "detached_head")


if __name__ == "__main__":
    unittest.main()
