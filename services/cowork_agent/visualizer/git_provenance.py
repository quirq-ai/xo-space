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

* **Credentials are stripped before the URL is returned, but the SSH
  username is not.** A user-created repository may carry
  ``https://user:token@github.com/…`` in ``.git/config``, and ``config
  --get`` returns it verbatim; callers persist this value. Stripping the
  *whole* userinfo field — which is what ``self_update._URL_USERINFO_RE``
  does — is right for that case and wrong for ``ssh://git@github.com/…``,
  where ``git@`` is the SSH **username**, not a secret: dropping it yields
  a URL that is not clone-able, because SSH then falls back to the local
  login name. See :func:`sanitize_remote_url` for the scheme-aware split
  and why this module does not reuse the stderr scrubber.

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

# Schemes whose userinfo is a **username**, not a credential. For these the
# user is kept and only a ``:password`` is dropped; for everything else the
# whole userinfo field goes. The list is deliberately short and explicit —
# an unrecognised scheme falls through to the strip-everything branch, so a
# new scheme fails closed (a mangled URL, never a leaked token).
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def sanitize_remote_url(url: str) -> str:
    """Strip credentials from a git remote URL, keeping the SSH username.

    Three shapes reach this function and they need three answers:

    ``https://user:token@github.com/o/r.git``
        Userinfo is a credential. Strip it whole. **A bare
        ``https://ghp_token@github.com/…`` is also a credential** — GitHub
        accepts a token as the username with no password — which is why
        this cannot simply be "keep userinfo that has no colon".

    ``ssh://git@github.com/o/r.git``
        Userinfo is the SSH username. Keep it. Stripping ``git@`` (what
        ``self_update._URL_USERINFO_RE`` alone does, and what this module
        used to persist) produces ``ssh://github.com/o/r.git``, which is
        not clone-able: SSH falls back to the local login name and GitHub
        only accepts ``git``. A ``:password`` in an ssh URL is still
        dropped — it is a credential wherever it appears.

    ``git@github.com:o/r.git``
        The scp-short form. It has no ``//``, so there is no userinfo
        *field* to parse — ``git@`` here is part of the syntax. Returned
        untouched, which is also what the old regex did, by accident of
        its ``//`` anchor rather than by intent.

    Why not fix ``self_update._URL_USERINFO_RE`` in place, per the usual
    "fix the primitive, not the caller" rule: that regex is applied to git
    **stderr** (``self_update.py:99,162``), which is arbitrary text with no
    scheme to parse. Scrubbing every ``//…@`` there is the correct, blunt
    answer. The two jobs only look alike — one sanitises a structured URL,
    the other redacts free text — so they are deliberately kept apart.
    (Repos this system creates are already clean:
    ``xo_projects_sync/github.py`` injects the token per command rather
    than into the remote.)
    """
    if not url or "//" not in url:
        return url                          # scp-short form, or not a URL
    scheme, _, rest = url.partition("://")
    if not rest:                            # no scheme: fail closed
        return _URL_USERINFO_RE.sub("//", url)
    authority, sep, tail = rest.partition("/")
    if "@" not in authority:
        return url                          # no userinfo at all
    userinfo, _, host = authority.rpartition("@")
    if scheme.lower() not in _SSH_SCHEMES:
        return f"{scheme}://{host}{sep}{tail}"
    user = userinfo.partition(":")[0]       # keep the user, drop any password
    if not user:
        return f"{scheme}://{host}{sep}{tail}"
    return f"{scheme}://{user}@{host}{sep}{tail}"


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
    out["remote_url"] = sanitize_remote_url(url) or None
    out["default_branch"] = (
        default or _git_out(pdir, "branch", "--show-current") or None
    )
    return out
