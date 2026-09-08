"""git_provenance() — remote URL and default branch, from the project's own repo.

Everything is driven against real repositories under a throwaway temp root,
the way test_space_file_history.py does: the whole point of this helper is
what actual git prints in each failure mode, so mocking git would test
nothing. The two mocked cases are the ones that cannot be staged on disk —
git missing from PATH, and a git that hangs past the timeout.

The load-bearing case is ``test_nested_plain_folder_...``: git answers rc=0
with the *enclosing* repo's URL there, so a helper without the ``.git`` gate
silently attributes the parent's remote to the project.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import git_provenance as gp
from services.cowork_agent.visualizer.git_provenance import (
    git_provenance,
    is_git_repo,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True,
        capture_output=True, text=True,
    )
    return res.stdout.strip()


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class GitProvenanceTests(unittest.TestCase):
    """Real repositories in a throwaway root — never the user's projects."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def repo(self, name: str, *, branch: str = "main", commit: bool = True) -> Path:
        pdir = self.root / name
        pdir.mkdir(parents=True)
        git(pdir, "init", "-q", "-b", branch)
        if commit:
            (pdir / "a.txt").write_text("a\n", encoding="utf-8")
            git(pdir, "add", "a.txt")
            git(pdir, "commit", "-qm", "one")
        return pdir

    # --- absent / unknown ------------------------------------------------

    def test_plain_folder_is_not_a_repo(self) -> None:
        plain = self.root / "scratch-notes"
        plain.mkdir()
        self.assertEqual(
            git_provenance(plain),
            {"remote_url": None, "default_branch": None},
        )

    def test_missing_path_never_raises(self) -> None:
        self.assertEqual(
            git_provenance(self.root / "does-not-exist"),
            {"remote_url": None, "default_branch": None},
        )

    def test_path_is_a_file(self) -> None:
        f = self.root / "a-file"
        f.write_text("not a project\n", encoding="utf-8")
        self.assertEqual(
            git_provenance(f), {"remote_url": None, "default_branch": None}
        )

    def test_repo_with_no_remote(self) -> None:
        """No origin: URL unknown, but the checked-out branch still answers."""
        pdir = self.repo("solo")
        self.assertEqual(
            git_provenance(pdir),
            {"remote_url": None, "default_branch": "main"},
        )

    def test_empty_repo_no_commits(self) -> None:
        """``branch --show-current`` reports the initial branch pre-commit."""
        pdir = self.repo("fresh", branch="trunk", commit=False)
        got = git_provenance(pdir)
        self.assertIsNone(got["remote_url"])
        self.assertEqual(got["default_branch"], "trunk")

    def test_empty_repo_with_remote(self) -> None:
        pdir = self.repo("fresh-remote", commit=False)
        git(pdir, "remote", "add", "origin",
            "https://github.com/owner/repo.git")
        self.assertEqual(
            git_provenance(pdir),
            {"remote_url": "https://github.com/owner/repo.git",
             "default_branch": "main"},
        )

    # --- the nested-folder trap (requirement 2) --------------------------

    def test_nested_plain_folder_does_not_inherit_parent_remote(self) -> None:
        """A plain folder inside a checkout must report nothing.

        Asserted against the raw git answer, so this stays a real test: git
        exits 0 there and prints the *enclosing* repo's URL.
        """
        parent = self.repo("parent")
        git(parent, "remote", "add", "origin",
            "https://github.com/owner/parent.git")
        nested = parent / "nested-project"
        nested.mkdir()

        raw = subprocess.run(
            ["git", "-C", str(nested), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, env=GIT_ENV,
        )
        self.assertEqual(raw.returncode, 0)          # git happily answers…
        self.assertEqual(raw.stdout.strip(),         # …with the parent's URL
                         "https://github.com/owner/parent.git")

        self.assertEqual(
            git_provenance(nested),
            {"remote_url": None, "default_branch": None},
        )
        self.assertFalse(is_git_repo(nested))

    def test_nested_repo_reports_its_own_remote(self) -> None:
        """The gate must not swallow a real repo nested inside another."""
        parent = self.repo("outer")
        git(parent, "remote", "add", "origin",
            "https://github.com/owner/parent.git")
        inner = self.repo("outer/inner")
        git(inner, "remote", "add", "origin",
            "https://github.com/owner/inner.git")
        self.assertEqual(
            git_provenance(inner)["remote_url"],
            "https://github.com/owner/inner.git",
        )

    # --- clones, detached HEAD, worktrees --------------------------------

    def test_clone_reports_origin_head(self) -> None:
        src = self.repo("src")
        dst = self.root / "cloned"
        subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                       check=True, env=GIT_ENV, capture_output=True)
        got = git_provenance(dst)
        self.assertEqual(got["remote_url"], str(src))
        self.assertEqual(got["default_branch"], "main")

    def test_clone_default_branch_keeps_slashes(self) -> None:
        """``--short`` prints ``origin/release/2.x``; only ``origin/`` goes."""
        src = self.repo("slashed", branch="release/2.x")
        dst = self.root / "slashed-clone"
        subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                       check=True, env=GIT_ENV, capture_output=True)
        self.assertEqual(git_provenance(dst)["default_branch"], "release/2.x")

    def test_detached_head_in_a_clone_still_resolves(self) -> None:
        """origin/HEAD is a ref, not the checkout — detaching cannot lose it."""
        src = self.repo("src2")
        (src / "b.txt").write_text("b\n", encoding="utf-8")
        git(src, "add", "b.txt")
        git(src, "commit", "-qm", "two")
        dst = self.root / "detached-clone"
        subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                       check=True, env=GIT_ENV, capture_output=True)
        git(dst, "checkout", "-q", "HEAD~1")
        self.assertEqual(git(dst, "branch", "--show-current"), "")  # detached
        self.assertEqual(git_provenance(dst)["default_branch"], "main")

    def test_detached_head_without_origin_head_is_none(self) -> None:
        """No clone → no origin/HEAD; detached → no current branch → None."""
        pdir = self.repo("local-detached")
        (pdir / "b.txt").write_text("b\n", encoding="utf-8")
        git(pdir, "add", "b.txt")
        git(pdir, "commit", "-qm", "two")
        git(pdir, "remote", "add", "origin",
            "https://github.com/owner/repo.git")
        git(pdir, "checkout", "-q", "HEAD~1")
        self.assertEqual(
            git_provenance(pdir),
            {"remote_url": "https://github.com/owner/repo.git",
             "default_branch": None},
        )

    def test_linked_worktree_dot_git_is_a_file(self) -> None:
        """``.exists()`` not ``.is_dir()``: a worktree's ``.git`` is a file."""
        src = self.repo("wt-src")
        dst = self.root / "wt-clone"
        subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                       check=True, env=GIT_ENV, capture_output=True)
        wt = self.root / "wt"
        git(dst, "worktree", "add", "-q", str(wt))
        self.assertTrue((wt / ".git").is_file())
        self.assertFalse((wt / ".git").is_dir())
        self.assertTrue(is_git_repo(wt))
        self.assertEqual(git_provenance(wt)["remote_url"], str(src))

    # --- credentials (requirement 3) -------------------------------------

    def test_credentials_are_stripped_from_the_url(self) -> None:
        pdir = self.repo("creds")
        git(pdir, "remote", "add", "origin",
            "https://alice:ghp_sekrit@github.com/owner/repo.git")
        url = git_provenance(pdir)["remote_url"]
        self.assertEqual(url, "https://github.com/owner/repo.git")
        self.assertNotIn("ghp_sekrit", url)
        self.assertNotIn("alice", url)

    def test_userless_and_ssh_urls_survive_verbatim(self) -> None:
        """No credential, no rewrite — including the SSH username.

        ``ssh://git@…`` used to come back as ``ssh://github.com/…``: the
        sanitiser stripped the whole userinfo field, and ``git@`` there is
        the SSH *username*, not a secret. The stripped form is not
        clone-able (SSH falls back to the local login name), and since
        ``project.json:git`` is the copy that survives a restore — the
        tarball drops ``.git`` and keeps ``project.json`` — the mangled
        URL would be the only one left. See ``sanitize_remote_url``.
        """
        for name, url in (
            ("plain-https", "https://github.com/owner/repo.git"),
            ("scp-ssh", "git@github.com:owner/repo.git"),
            ("ssh-url", "ssh://git@github.com/owner/repo.git"),
            ("ssh-url-port", "ssh://git@github.com:2222/owner/repo.git"),
            ("git+ssh", "git+ssh://git@github.com/owner/repo.git"),
            ("ssh-no-user", "ssh://github.com/owner/repo.git"),
        ):
            with self.subTest(name):
                pdir = self.repo(name)
                git(pdir, "remote", "add", "origin", url)
                self.assertEqual(git_provenance(pdir)["remote_url"], url)

    def test_credentials_are_stripped_whatever_shape_they_take(self) -> None:
        """The SSH carve-out must not open a hole.

        A bare ``https://<token>@host/…`` is a real GitHub auth form, so
        "keep userinfo that has no colon" would leak it. The rule is
        scheme-based, not colon-based, and an ssh URL carrying a password
        still loses the password.
        """
        for name, url, expect in (
            ("https-user-pass", "https://alice:ghp_sekrit@github.com/o/r.git",
             "https://github.com/o/r.git"),
            ("https-bare-token", "https://ghp_sekrit@github.com/o/r.git",
             "https://github.com/o/r.git"),
            ("ssh-with-password", "ssh://git:ghp_sekrit@github.com/o/r.git",
             "ssh://git@github.com/o/r.git"),
            ("unknown-scheme", "weird://alice:ghp_sekrit@host/o/r.git",
             "weird://host/o/r.git"),
        ):
            with self.subTest(name):
                pdir = self.repo(name)
                git(pdir, "remote", "add", "origin", url)
                got = git_provenance(pdir)["remote_url"]
                self.assertEqual(got, expect)
                self.assertNotIn("ghp_sekrit", got)

    # --- git itself unavailable ------------------------------------------

    def test_git_binary_missing(self) -> None:
        pdir = self.repo("no-git-binary")
        with patch.object(gp.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(
                git_provenance(pdir),
                {"remote_url": None, "default_branch": None},
            )

    def test_git_hangs_past_the_timeout(self) -> None:
        pdir = self.repo("hung")
        boom = subprocess.TimeoutExpired(cmd="git", timeout=gp._GIT_TIMEOUT_S)
        with patch.object(gp.subprocess, "run", side_effect=boom):
            self.assertEqual(
                git_provenance(pdir),
                {"remote_url": None, "default_branch": None},
            )

    def test_timeout_is_passed_to_every_git_call(self) -> None:
        pdir = self.repo("bounded")
        real = subprocess.run
        seen: list[float] = []

        def spy(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real(cmd, **kwargs)

        with patch.object(gp.subprocess, "run", side_effect=spy):
            git_provenance(pdir)
        self.assertTrue(seen)
        self.assertTrue(all(t == gp._GIT_TIMEOUT_S for t in seen), seen)


if __name__ == "__main__":
    unittest.main()
