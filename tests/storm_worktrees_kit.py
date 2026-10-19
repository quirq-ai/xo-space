"""Real git repositories in a temp folder for storm's worktree tests (git is
not an agent CLI). The person's own git configuration is shut out, and a
fixed author is set, so the tests read the same everywhere."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git_env() -> mock._patch:
    """Patch ``os.environ`` for the test's git (the code's git inherits it)."""
    return mock.patch.dict(os.environ, GIT_ENV)


def sh(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def repo(folder: Path, *, ignore: str = "node_modules/\n.env\n") -> Path:
    """A git project with one commit (a README and a .gitignore)."""
    folder.mkdir(parents=True, exist_ok=True)
    sh(folder, "init", "-q", "-b", "main")
    (folder / "README.md").write_text("a project\n", encoding="utf-8")
    (folder / ".gitignore").write_text(ignore, encoding="utf-8")
    sh(folder, "add", "README.md", ".gitignore")
    sh(folder, "commit", "-q", "-m", "first")
    return folder


def head(folder: Path) -> str:
    return sh(folder, "rev-parse", "HEAD").strip()


def branches(folder: Path) -> list[str]:
    return sh(folder, "branch", "--format=%(refname:short)").split()


def worktree_paths(folder: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in sh(folder, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]
