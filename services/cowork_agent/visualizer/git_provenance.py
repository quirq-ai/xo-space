"""Git provenance for one project — its origin URL and default branch.

Read from the project's **own** repository, and never raises: a project
folder may be a clone, a bare ``git init``, a linked worktree, or not a
repository at all, and every caller (``project.json`` and ``projects.json``,
syncplan §5.1-5.2) needs a well-formed document in all of those cases. Both
fields are ``None`` when the answer is unknown.

Three deliberate, non-obvious choices, each verified on git 2.55.0:

* **``git config --get remote.origin.url``, not ``git remote get-url
  origin``.** ``config --get`` is silent in every failure mode — neither a
  repository nor no remote gives rc=1 with *empty stdout and empty stderr* —
  whereas ``remote get-url`` writes ``fatal:`` / ``error:`` text that a
  merged or logged stream would surface as noise.

* **The ``.git`` gate is correctness, not an optimisation.** ``git -C
  <plain folder nested in a checkout> config --get remote.origin.url`` exits
  **0 with the enclosing repository's URL**, so an ungated read silently
  attributes the parent's remote to the project. ``space_index._git_facts``
  and ``file_history._repo_toplevel`` hold the same line. The test is
  ``.exists()`` and not ``.is_dir()``, because ``.git`` is a *file* in a
  linked worktree or a submodule.

* **Credentials are stripped before the URL is returned.** A user-created
  repository may carry ``https://user:token@github.com/…`` in
  ``.git/config``, and ``config --get`` returns it verbatim; callers persist
  this value. The sanitiser is the repo's only one,
  ``self_update._URL_USERINFO_RE``, imported rather than copied so a fix
  lands in one place. (Repos this system creates are already clean —
  ``xo_projects_sync/github.py`` injects the token per command instead of
  into the remote.)

Everything here is offline. ``git ls-remote --symref`` would answer the
default-branch question authoritatively, but it hits the network, which a
watcher tick must never do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from services.cowork_agent.self_update import _URL_USERINFO_RE

# Same bound ``space_index._git_facts`` uses: a wedged git must not stall
# the caller's tick.
_GIT_TIMEOUT_S = 5


def is_git_repo(pdir: Path) -> bool:
    """Whether ``pdir`` is the root of its own repository.

    ``.exists()`` rather than ``.is_dir()`` on purpose: a linked worktree
    and a submodule both carry ``.git`` as a *file* holding a ``gitdir:``
    pointer, and those are real repositories with a real origin.
    """
    try:
        return (pdir / ".git").exists()
    except Exception:
        return False


def _git_out(pdir: Path, *args: str) -> str:
    """One git read. Empty string on any failure — never raises, never logs."""
    try:
        res = subprocess.run(
            ["git", "-C", str(pdir), *args],
            capture_output=True, text=True, errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def git_provenance(pdir: Path) -> dict[str, str | None]:
    """Remote URL and default branch for one project, read from its OWN repo.

    Both ``None`` when unavailable — not a repository, git not installed, no
    ``origin`` remote, no commits.

    ``symbolic-ref refs/remotes/origin/HEAD`` is offline and survives a
    detached HEAD, but only ``clone`` populates it; hence the
    ``branch --show-current`` fallback, which yields the branch name on an
    empty repository and an empty string on a detached HEAD.
    """
    out: dict[str, str | None] = {"remote_url": None, "default_branch": None}
    if not is_git_repo(pdir):   # a nested plain folder must not inherit
        return out              # the enclosing repository's remote
    url = _git_out(pdir, "config", "--get", "remote.origin.url")
    head = _git_out(pdir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    # ``--short`` yields "origin/<branch>"; drop only the remote name, so a
    # default branch containing "/" ("release/2.x") survives intact.
    default = head.split("/", 1)[1] if "/" in head else head
    out["remote_url"] = _URL_USERINFO_RE.sub("//", url) or None
    out["default_branch"] = (
        default or _git_out(pdir, "branch", "--show-current") or None
    )
    return out
