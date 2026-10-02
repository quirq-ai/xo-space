"""Self-update for the xo-space checkout, via git.

Surfaced in the Space UI's Setup tab: check the checkout's own remote for a
newer version, report how far behind HEAD is, and move forward on request.

Two channels, chosen by where the checkout is:

- **release** (a detached HEAD, which is what the installer's tag clone is,
  or the ``main`` branch): the target is the newest ``vX.Y.Z`` tag on the
  remote, never the ``main`` tip. It only ever moves forward: a checkout
  that already contains that tag (the main tip, a newer build) is up to
  date. A remote with no release tags falls back to the branch channel.
- **branch** (any other branch, e.g. ``development``): the target is
  ``origin/<branch>``, fast-forward only, as before.

Pure git, deliberately conservative: it never touches a dirty tree and never
rewrites history (a diverged checkout is reported, not rebased). Applying an
update changes code on disk; the running server keeps executing the old
version until restarted. The installer's ``fetch_repo`` applies the same
release rule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from utils.commands import CommandResult, run_sync

REPO_ROOT = Path(__file__).resolve().parents[2]

_FETCH_TIMEOUT_S = 20
_APPLY_TIMEOUT_S = 60
_REMOTE = "origin"
# Releases are tagged on this branch (RELEASING.md); a checkout on it follows
# release tags rather than its tip.
_RELEASE_BRANCH = "main"
_RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# Strip credentials from remote URLs before they reach a response:
# https://user:token@host/... → https://host/...
_URL_USERINFO_RE = re.compile(r"//[^/@]+@")


class UpdateError(RuntimeError):
    """A git step failed in a way the caller should surface verbatim."""


def _git(*args: str, timeout: float = 10.0) -> CommandResult:
    return run_sync(["git", "-C", str(REPO_ROOT), *args], timeout=timeout, separate_stderr=True)


def _line(res: CommandResult) -> str:
    return (res.stdout or "").strip()


def _commit_info(ref: str) -> Optional[dict]:
    res = _git("log", "-1", "--date=short",
               "--format=%h%x01%ad%x01%s", ref)
    if res.returncode != 0:
        return None
    sha, _, rest = _line(res).partition("\x01")
    day, _, subject = rest.partition("\x01")
    return {"sha": sha, "date": day, "subject": subject[:120]}


def _fetch_failed(res: Optional[CommandResult]) -> str:
    detail = _URL_USERINFO_RE.sub("//", (res.stderr if res else "timed out").strip())
    return ("Could not reach the remote to check for updates "
            f"({detail[:200] or 'network error'}). Showing local state only.")


def _version(tag: str) -> tuple[int, int, int]:
    major, minor, patch = _RELEASE_TAG_RE.match(tag).groups()
    return int(major), int(minor), int(patch)


def _latest_release_tag(fetch: bool) -> tuple[Optional[str], Optional[str]]:
    """The newest vX.Y.Z tag (no pre-releases): from the remote when
    ``fetch``, else from the local tags. Returns ``(tag, error)``; a remote
    with no release tags is ``(None, None)``."""
    if fetch:
        res = _git("ls-remote", "--tags", "--refs", _REMOTE, timeout=_FETCH_TIMEOUT_S)
        if res.timed_out or res.returncode != 0:
            return None, _fetch_failed(None if res.timed_out else res)
        names = [ln.rsplit("refs/tags/", 1)[-1] for ln in (res.stdout or "").splitlines()
                 if "refs/tags/" in ln]
    else:
        names = _line(_git("tag", "--list", "v*")).split()
    tags = [name for name in names if _RELEASE_TAG_RE.match(name)]
    return (max(tags, key=_version) if tags else None), None


def _is_ancestor(older: str, newer: str) -> bool:
    return _git("merge-base", "--is-ancestor", older, newer).returncode == 0


def _count(revs: str) -> int:
    return int(_line(_git("rev-list", "--count", revs)) or 0)


def _release_status(status: dict, tag: str, fetch: bool) -> dict:
    """Compare HEAD with release ``tag``. Only "HEAD is an ancestor of the
    tag" means behind: that check also holds in the installer's shallow
    clone, because fetching the tag brings the commits in between. The other
    direction does not (a shallow HEAD cannot see older history), so a
    shallow checkout that is not behind is simply up to date."""
    if fetch:
        fetched = _git("fetch", "--quiet", "--no-tags", _REMOTE,
                       f"+refs/tags/{tag}:refs/tags/{tag}", timeout=_FETCH_TIMEOUT_S)
        if fetched.timed_out or fetched.returncode != 0:
            status["fetch_ok"] = False
            status["message"] = _fetch_failed(None if fetched.timed_out else fetched)
    target = f"refs/tags/{tag}^{{commit}}"
    if _git("rev-parse", "--verify", "--quiet", target).returncode != 0:
        status["fetch_ok"] = False
        status["message"] = status["message"] or f"Release {tag} is not available locally."
        status.update({"latest_tag": tag, "latest": None, "behind": 0, "ahead": 0,
                       "up_to_date": None})
        return status

    status["latest_tag"] = tag
    status["latest"] = _commit_info(target)
    if _is_ancestor("HEAD", target):
        behind = _count(f"HEAD..{target}")
        status.update({"behind": behind, "ahead": 0, "up_to_date": behind == 0})
    elif _is_ancestor(target, "HEAD") or _line(_git("rev-parse", "--is-shallow-repository")) == "true":
        # Newer than the latest release (e.g. the main tip): nothing to do
        # until a newer tag is cut. Never a downgrade.
        status.update({"behind": 0, "ahead": _count(f"{target}..HEAD"), "up_to_date": True})
    else:
        status.update({"behind": _count(f"HEAD..{target}"), "ahead": _count(f"{target}..HEAD"),
                       "up_to_date": False})
    return status


def _branch_status(status: dict, branch: str, fetch: bool) -> dict:
    if fetch:
        fetched = _git("fetch", "--quiet", _REMOTE, branch,
                       timeout=_FETCH_TIMEOUT_S)
        if fetched.timed_out or fetched.returncode != 0:
            status["fetch_ok"] = False
            status["message"] = _fetch_failed(None if fetched.timed_out else fetched)

    upstream = f"{_REMOTE}/{branch}"
    if _git("rev-parse", "--verify", "--quiet", upstream).returncode != 0:
        status["fetch_ok"] = False
        status["message"] = status["message"] or (
            f"The remote has no '{branch}' branch to compare against."
        )
        status.update({"latest": None, "behind": 0, "ahead": 0,
                       "up_to_date": None})
        return status

    behind = _count(f"HEAD..{upstream}")
    ahead = _count(f"{upstream}..HEAD")
    status.update({
        "latest": _commit_info(upstream),
        "behind": behind,
        "ahead": ahead,
        "up_to_date": behind == 0,
    })
    return status


def check_update_status(fetch: bool = True) -> dict:
    """Compare HEAD against its update target: the newest release tag, or
    the remote branch (see the module docstring for which). Network only
    for the fetch; everything else reads local refs, so an offline check
    still reports the current version with ``fetch_ok`` false."""
    if not (REPO_ROOT / ".git").exists():
        return {
            "supported": False,
            "reason": "not_a_git_checkout",
            "message": "This installation is not a git checkout, so it "
                       "cannot self-update. Re-run the installer instead.",
        }

    remotes = _line(_git("remote")).split()
    if _REMOTE not in remotes:
        return {
            "supported": False,
            "reason": "no_origin_remote",
            "message": f"The checkout has no '{_REMOTE}' remote to update from.",
        }

    branch = _line(_git("rev-parse", "--abbrev-ref", "HEAD"))
    detached = not branch or branch == "HEAD"
    status: dict = {
        "supported": True,
        "channel": "branch",
        "branch": None if detached else branch,
        "remote": _REMOTE,
        "dirty": bool(_line(_git("status", "--porcelain"))),
        "current": _commit_info("HEAD"),
        "current_tag": _line(_git("describe", "--tags", "--exact-match", "HEAD")) or None,
        "fetch_ok": True,
        "message": "",
    }

    if detached or branch == _RELEASE_BRANCH:
        tag, error = _latest_release_tag(fetch)
        if error:
            # Offline: the current version only (apply refuses on fetch_ok false).
            status.update({"fetch_ok": False, "message": error, "latest": None,
                           "behind": 0, "ahead": 0, "up_to_date": None})
            return status
        if tag:
            status["channel"] = "release"
            return _release_status(status, tag, fetch)
        if detached:
            return {
                "supported": False,
                "reason": "detached_head",
                "message": "The checkout is on a detached HEAD and its remote "
                           "has no release tags; check out a branch to enable "
                           "self-update.",
            }
    return _branch_status(status, branch, fetch)


def apply_update() -> dict:
    """Move the checkout forward to its update target: fast-forward a
    branch, or check the new tag out on a detached HEAD. Refuses (with an
    actionable reason, never an exception) when the tree is dirty, the
    checkout diverged, the remote is unreachable, or there is nothing to do.
    A successful update still needs a server restart to take effect."""
    status = check_update_status(fetch=True)
    if not status.get("supported"):
        return {"updated": False, "reason": status["reason"],
                "message": status["message"]}
    if not status.get("fetch_ok"):
        return {"updated": False, "reason": "fetch_failed",
                "message": status["message"]}
    if status.get("dirty"):
        return {
            "updated": False, "reason": "dirty_tree",
            "message": "The checkout has local changes. Commit, stash, or "
                       "discard them, then update again.",
        }
    release = status["channel"] == "release"
    if release and status.get("up_to_date"):
        return {"updated": False, "reason": "up_to_date",
                "message": f"No release newer than {status['latest_tag']}."}
    if status.get("ahead"):
        where = f"release {status['latest_tag']}" if release else "the remote"
        return {
            "updated": False, "reason": "diverged",
            "message": f"The checkout has {status['ahead']} commit(s) that "
                       f"{where} does not. Self-update only moves forward; "
                       "reconcile manually.",
        }
    if status.get("up_to_date"):
        return {"updated": False, "reason": "up_to_date",
                "message": "Already on the latest version."}

    target = (f"refs/tags/{status['latest_tag']}^{{commit}}" if release
              else f"{_REMOTE}/{status['branch']}")
    old = status["current"]["sha"] if status.get("current") else None
    if status["branch"] is None:
        moved = _git("checkout", "--quiet", "--detach", target, timeout=_APPLY_TIMEOUT_S)
        step = "git checkout"
    else:
        moved = _git("merge", "--ff-only", target, timeout=_APPLY_TIMEOUT_S)
        step = "git merge --ff-only"
    if moved.returncode != 0:
        raise UpdateError(
            f"{step} failed: "
            + _URL_USERINFO_RE.sub("//", (moved.stderr or moved.output or "").strip())[:300]
        )

    new = _commit_info("HEAD")
    changed = _line(_git("diff", "--name-only", f"{old}..{new['sha']}"))
    requirements_changed = "requirements.txt" in changed.split("\n")
    version = status["latest_tag"] if release else new["sha"]
    return {
        "updated": True,
        "from": old,
        "to": new,
        "tag": status.get("latest_tag") if release else None,
        "commits": status["behind"],
        "requirements_changed": requirements_changed,
        "restart_required": True,
        "message": f"Updated {status['behind']} commit(s) to {version}. "
                   "Restart the server to run it"
                   + ("; requirements.txt changed, and its new dependencies "
                      "are installed when the server starts." if requirements_changed else "."),
    }
