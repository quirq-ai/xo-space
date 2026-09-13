"""Timeline "By file / By project" modes.

By project renders every project's git commit history in parallel lanes,
fed by the new ``gitHistory`` field that space_index derives from the same
``git log`` that already dates the leaves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from services.cowork_agent.visualizer.space_index import (
    MAX_HISTORY_DAYS,
    _aggregate_history,
    _git_facts,
)


ROOT = Path(__file__).resolve().parents[1]


class AggregateHistoryTests(unittest.TestCase):
    def test_collapses_commits_into_per_day_entries(self) -> None:
        out = _aggregate_history([
            ("2026-01-02", "second"),
            ("2026-01-01", "first"),
            ("2026-01-02", "third"),
        ])
        self.assertEqual([day["d"] for day in out], ["2026-01-01", "2026-01-02"])
        self.assertEqual(out[1]["n"], 2)
        self.assertEqual(out[1]["s"], ["second", "third"])

    def test_caps_subjects_per_day_and_truncates(self) -> None:
        day = "2026-03-01"
        out = _aggregate_history([(day, "x" * 200)] + [(day, f"c{i}") for i in range(9)])
        self.assertEqual(out[0]["n"], 10)
        self.assertEqual(len(out[0]["s"]), 3)
        self.assertEqual(len(out[0]["s"][0]), 80)

    def test_skips_empty_subjects_but_counts_them(self) -> None:
        out = _aggregate_history([("2026-04-01", ""), ("2026-04-01", "real")])
        self.assertEqual(out[0]["n"], 2)
        self.assertEqual(out[0]["s"], ["real"])

    def test_keeps_only_the_newest_day_entries(self) -> None:
        first = date(2020, 1, 1)
        total = MAX_HISTORY_DAYS + 70
        days = [(first + timedelta(days=i)).isoformat() for i in range(total)]
        out = _aggregate_history([(d, "s") for d in days])
        self.assertEqual(len(out), MAX_HISTORY_DAYS)
        # The oldest 70 days are the ones dropped; order stays chronological.
        self.assertEqual(out[0]["d"], days[70])
        self.assertEqual(out[-1]["d"], days[-1])


def _repo_builder(repo: Path, tmp: str):
    """Return a git() helper that shells out to one resolved git binary.

    The binary is resolved from the ambient PATH once (the same PATH
    _git_facts itself uses), so the guard, the fixture, and the code under
    test all exercise the same git."""
    git_bin = shutil.which("git")

    def git(*args: str) -> None:
        subprocess.run(
            [git_bin, "-C", str(repo), *args],
            check=True, capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "HOME": tmp,
                "PATH": os.path.dirname(git_bin or "/usr/bin/git")
                + ":/usr/bin:/bin:/usr/local/bin",
            },
        )

    return git


class GitFactsHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")

    def test_git_facts_yields_dated_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = _repo_builder(repo, tmp)
            git("init", "-q")
            (repo / "a.txt").write_text("a", encoding="utf-8")
            git("add", "a.txt")
            git("commit", "-q", "-m", "add a")
            (repo / "b.txt").write_text("b", encoding="utf-8")
            git("add", "b.txt")
            git("commit", "-q", "-m", "add b")

            created, first_commit, commits, history = _git_facts(repo)

        self.assertEqual(len(history), 2)
        self.assertEqual([subject for _, subject in history], ["add a", "add b"])
        for day, _ in history:
            self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(first_commit, history[0][0])
        self.assertEqual(created.get("a.txt"), history[0][0])
        self.assertEqual(commits, [["a.txt"], ["b.txt"]])

    def test_control_bytes_in_subjects_cannot_fabricate_headers(self) -> None:
        """A subject containing VT + \\x01 must not crash the build or leak a
        bogus date into the history (str.splitlines would split on the VT and
        promote the remainder to a fake commit header)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = _repo_builder(repo, tmp)
            git("init", "-q")
            (repo / "a.txt").write_text("a", encoding="utf-8")
            git("add", "a.txt")
            msg = repo / "msg.txt"  # untracked; only a.txt is staged
            msg.write_bytes(b"evil\x0b\x01bogusdate\x02fake subject")
            git("commit", "-q", "--cleanup=verbatim", "-F", str(msg))

            created, first_commit, commits, history = _git_facts(repo)

        self.assertEqual(len(history), 1)
        day, subject = history[0]
        self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotIn("bogusdate", [d for d, _ in history])
        # Control bytes are stripped from the stored subject.
        self.assertNotRegex(subject, r"[\x00-\x1f\x7f]")
        self.assertEqual(first_commit, day)
        self.assertEqual(created.get("a.txt"), day)
        self.assertEqual(len(commits), 1)

    def test_non_repo_returns_empty_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            created, first_commit, commits, history = _git_facts(Path(tmp))
        self.assertEqual((created, first_commit, commits, history), ({}, None, [], []))

    def test_non_repo_project_inside_a_repo_inherits_nothing(self) -> None:
        """A plain project folder nested in an enclosing repo must not adopt
        the parent's history as its own timeline lane."""
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            git = _repo_builder(outer, tmp)
            git("init", "-q")
            inner = outer / "plain-project"
            inner.mkdir()
            (inner / "notes.md").write_text("n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "outer commit")

            facts = _git_facts(inner)

        self.assertEqual(facts, ({}, None, [], []))


class GitOnlyDatesTests(unittest.TestCase):
    """Leaf dates come from git only: untracked files and non-git projects
    carry null dates, sit out the timeline, and never stretch its axis."""

    def test_build_dates_leaves_from_git_only(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        import json

        from services.cowork_agent.visualizer import space_index

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("repo", "plain"):
                (root / name / ".xo").mkdir(parents=True)
                (root / name / ".xo" / "project.json").write_text(
                    json.dumps({"name": name}), encoding="utf-8"
                )
            repo = root / "repo"
            git = _repo_builder(repo, tmp)
            git("init", "-q")
            (repo / "tracked.py").write_text("x", encoding="utf-8")
            git("add", "tracked.py")
            git("commit", "-q", "-m", "add tracked")
            (repo / "untracked.py").write_text("y", encoding="utf-8")
            (root / "plain" / "notes.md").write_text("n", encoding="utf-8")

            os.environ["XO_PROJECTS_ROOT"] = str(root)
            try:
                data = space_index.build_space_data()
            finally:
                os.environ.pop("XO_PROJECTS_ROOT", None)

        dates = {leaf["path"]: leaf["date"] for leaf in data["leaves"]}
        self.assertRegex(dates["repo/tracked.py"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsNone(dates["repo/untracked.py"])
        self.assertIsNone(dates["plain/notes.md"])
        # The axis spans the one commit day, not today's mtimes.
        commit_day = dates["repo/tracked.py"]
        self.assertLessEqual(data["timeline"]["start"], commit_day)
        span = [d for d in (data["timeline"]["start"], data["timeline"]["end"])]
        for bound in span:
            self.assertRegex(bound, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(list(data["gitHistory"]), ["p_repo"])


class TimelineModeWiringTests(unittest.TestCase):
    """The Timeline controls stay connected to their data and renderer."""

    def test_index_carries_the_mode_toggle(self) -> None:
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="tmode"', index)
        self.assertIn('data-tmode="file"', index)
        self.assertIn('data-tmode="project"', index)
        self.assertIn("css/timeline.css?v=", index)

    def test_atlas_renders_git_history_and_remembers_the_mode(self) -> None:
        atlas = (
            ROOT / "space_ui" / "js" / "views" / "atlas.js"
        ).read_text(encoding="utf-8")
        self.assertIn("DATA.gitHistory", atlas)
        self.assertIn("space.timelineMode", atlas)
        self.assertIn("setTMode", atlas)
        # Traces are a By-file tool; starting one leaves project mode.
        self.assertIn("if(tMode!=='file')setTMode('file')", atlas)

    def test_builder_emits_git_history(self) -> None:
        source = (
            ROOT / "services" / "cowork_agent" / "visualizer" / "space_index.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"gitHistory": git_history', source)

if __name__ == "__main__":
    unittest.main()
