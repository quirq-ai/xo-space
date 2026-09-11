"""Git provenance for one project — its origin URL and default branch."""

from __future__ import annotations

from pathlib import Path

from utils.commands import run_sync

from services.cowork_agent.self_update import _URL_USERINFO_RE

# Same bound ``space_index._git_facts`` uses: a wedged git must not stall the
# caller's tick.
_GIT_TIMEOUT_S = 5

# Schemes whose userinfo is a **username**, not a credential.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def sanitize_remote_url(url: str) -> str:
    """Strip credentials from a git remote URL, keeping the SSH username."""
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
    """Whether ``pdir`` is the root of its own repository."""
    try:
        return (pdir / ".git").exists()
    except Exception:
        return False


def _git_out(pdir: Path, *args: str) -> str:
    """One git read. Empty string on any failure — never raises, never logs."""
    res = run_sync(
        ["git", "-C", str(pdir), *args],
        timeout=_GIT_TIMEOUT_S, separate_stderr=True,
    )
    return res.stdout.strip() if res.ok else ""


def git_provenance(pdir: Path) -> dict[str, str | None]:
    """Remote URL and default branch for one project, read from its OWN repo."""
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
