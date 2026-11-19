"""The overview's git scan answers from its last scan only while the repo's
history and git settings haven't moved, and checks itself: every answer it
gives equals a fresh scan's."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import space_index


class GitFactsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        space_index._facts_cache.clear()
        space_index._facts_hits.clear()
        self.addCleanup(space_index._facts_cache.clear)
        self.addCleanup(space_index._facts_hits.clear)
        self.stats = patch.dict(space_index.git_facts_stats, {k: 0 for k in space_index.git_facts_stats})
        self.stats.start()
        self.addCleanup(self.stats.stop)
        self.git("init", "-q", "-b", "main")
        self.commit("a.txt", "add a")
        self.commit("b.txt", "add b")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def commit(self, name: str, message: str) -> None:
        (self.repo / name).write_text(message, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def facts(self):
        return space_index._git_facts(self.repo)

    def test_an_unmoved_repo_is_answered_without_git_and_the_same(self) -> None:
        first = self.facts()
        with patch.object(space_index, "_scan_git_facts", side_effect=AssertionError("git ran")), \
             patch.object(space_index, "GIT_FACTS_VERIFY_EVERY", 1000):
            for _ in range(5):
                self.assertEqual(self.facts(), first)
        self.assertEqual((space_index.git_facts_stats["scans"], space_index.git_facts_stats["reused"]), (1, 5))

    def test_every_change_to_history_or_settings_scans_again(self) -> None:
        changes = {
            "a new commit": lambda: self.commit("c.txt", "add c"),
            "an amended commit": lambda: self.git("commit", "-q", "--amend", "-m", "add c, reworded"),
            "a branch switch": lambda: (self.git("checkout", "-q", "-b", "other"), self.commit("d.txt", "add d")),
            "a reset": lambda: self.git("reset", "-q", "--hard", "HEAD~1"),
            "the repo's config": lambda: self.git("config", "core.quotePath", "false"),
            "packed refs": lambda: self.git("pack-refs", "--all"),
        }
        with patch.object(space_index, "GIT_FACTS_VERIFY_EVERY", 1000):
            self.facts()
            for name, change in changes.items():
                with self.subTest(change=name):
                    change()
                    expected = space_index._scan_git_facts(self.repo)
                    self.assertEqual(self.facts(), expected)
                    self.assertEqual(self.facts(), expected)       # and then reused

    def test_a_reused_answer_is_checked_against_a_scan(self) -> None:
        with patch.object(space_index, "GIT_FACTS_VERIFY_EVERY", 1):
            first = self.facts()
            for _ in range(3):
                self.assertEqual(self.facts(), first)
        self.assertEqual(space_index.git_facts_stats["verified"], 3)
        self.assertEqual(space_index.git_facts_stats["mismatches"], 0)

    def test_a_change_the_signature_misses_is_caught_by_the_check(self) -> None:
        with patch.object(space_index, "GIT_FACTS_VERIFY_EVERY", 1):
            self.facts()
            signature = space_index._git_signature(self.repo)
            self.commit("e.txt", "add e")
            with patch.object(space_index, "_git_signature", return_value=signature):
                fresh = self.facts()
        self.assertEqual(fresh, space_index._scan_git_facts(self.repo))
        self.assertEqual(space_index.git_facts_stats["mismatches"], 1)

    def test_what_a_caller_changes_never_reaches_the_cache(self) -> None:
        created, _, commits, history = self.facts()
        created.clear()
        commits.append(["x"])
        history.clear()
        with patch.object(space_index, "GIT_FACTS_VERIFY_EVERY", 1000):
            self.assertEqual(self.facts(), space_index._scan_git_facts(self.repo))

    def test_a_folder_that_isnt_a_plain_repo_is_scanned_every_time(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        for _ in range(3):
            self.assertEqual(space_index._git_facts(plain), ({}, None, [], []))
        self.assertEqual(space_index.git_facts_stats["reused"], 0)

    def test_settings_from_an_included_file_or_reftable_refs_are_never_cached(self) -> None:
        cases = {
            "an include": lambda: self.git("config", "include.path", str(Path(self._tmp.name) / "extra")),
            "reftable": lambda: (self.repo / ".git" / "reftable").mkdir(),
            "a branch that is a symbolic ref": lambda: (
                self.git("symbolic-ref", "refs/heads/alias", "refs/heads/main"),
                self.git("symbolic-ref", "HEAD", "refs/heads/alias")),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                if name == "a branch that is a symbolic ref":
                    shutil.rmtree(self.repo / ".git" / "reftable", ignore_errors=True)
                    self.git("config", "--unset", "include.path")
                make()
                self.assertIsNone(space_index._git_signature(self.repo))
                before = space_index.git_facts_stats["reused"]
                self.facts()
                self.facts()
                self.assertEqual(space_index.git_facts_stats["reused"], before)


if __name__ == "__main__":
    unittest.main()
