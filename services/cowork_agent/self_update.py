"""Self-update for the xo-space checkout, via git.

Surfaced in the Space UI's Setup tab: check the checkout's own remote for a
newer version, report how far behind HEAD is, and fast-forward on request.
Pure git, deliberately conservative: it never touches a dirty tree, never
leaves the current branch, and only ever fast-forwards (a diverged local
branch is reported, not rebased). Applying an update changes code on disk;
the running server keeps executing the old version until restarted.
"""

from __future__ import annotations

import re
from utils.commands import CommandResult, run_sync
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

_FETCH_TIMEOUT_S = 20
_APPLY_TIMEOUT_S = 60
_REMOTE = "origin"
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


def check_update_status(fetch: bool = True) -> dict:
    """Compare HEAD against the remote branch. Network only for the fetch;
    everything else reads local refs, so an offline check still reports the
    current version with ``fetch_ok`` false."""
    if not (REPO_ROOT / ".git").exists():
        return {
            "supported": False,
            "reason": "not_a_git_checkout",
            "message": "This installation is not a git checkout, so it "
                       "cannot self-update. Re-run the installer instead.",
        }

    branch = _line(_git("rev-parse", "--abbrev-ref", "HEAD"))
    if not branch or branch == "HEAD":
        return {
            "supported": False,
            "reason": "detached_head",
            "message": "The checkout is on a detached HEAD; check out a "
                       "branch to enable self-update.",
        }

    remotes = _line(_git("remote")).split()
    if _REMOTE not in remotes:
        return {
            "supported": False,
            "reason": "no_origin_remote",
            "message": f"The checkout has no '{_REMOTE}' remote to update from.",
        }

    status: dict = {
        "supported": True,
        "branch": branch,
        "remote": _REMOTE,
        "dirty": bool(_line(_git("status", "--porcelain"))),
        "current": _commit_info("HEAD"),
        "fetch_ok": True,
        "message": "",
    }

    if fetch:
        fetched = _git("fetch", "--quiet", _REMOTE, branch,
                       timeout=_FETCH_TIMEOUT_S)
        if fetched.timed_out:
            fetched = None
        if fetched is None or fetched.returncode != 0:
            detail = _URL_USERINFO_RE.sub("//", (fetched.stderr if fetched else "timed out").strip())
            status["fetch_ok"] = False
            status["message"] = (
                "Could not reach the remote to check for updates "
                f"({detail[:200] or 'network error'}). Showing local state only."
            )

    upstream = f"{_REMOTE}/{branch}"
    if _git("rev-parse", "--verify", "--quiet", upstream).returncode != 0:
        status["fetch_ok"] = False
        status["message"] = status["message"] or (
            f"The remote has no '{branch}' branch to compare against."
        )
        status.update({"latest": None, "behind": 0, "ahead": 0,
                       "up_to_date": None})
        return status

    behind = int(_line(_git("rev-list", "--count", f"HEAD..{upstream}")) or 0)
    ahead = int(_line(_git("rev-list", "--count", f"{upstream}..HEAD")) or 0)
    status.update({
        "latest": _commit_info(upstream),
        "behind": behind,
        "ahead": ahead,
        "up_to_date": behind == 0,
    })
    return status


def apply_update() -> dict:
    """Fast-forward the checkout to the remote branch. Refuses (with an
    actionable reason, never an exception) when the tree is dirty, the
    branches diverged, the remote is unreachable, or there is nothing to do.
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
    if status.get("ahead"):
        return {
            "updated": False, "reason": "diverged",
            "message": f"The local branch has {status['ahead']} commit(s) the "
                       "remote does not. Self-update only fast-forwards; "
                       "reconcile manually.",
        }
    if status.get("up_to_date"):
        return {"updated": False, "reason": "up_to_date",
                "message": "Already on the latest version."}

    upstream = f"{_REMOTE}/{status['branch']}"
    old = status["current"]["sha"] if status.get("current") else None
    merged = _git("merge", "--ff-only", upstream, timeout=_APPLY_TIMEOUT_S)
    if merged.returncode != 0:
        raise UpdateError(
            "git merge --ff-only failed: "
            + _URL_USERINFO_RE.sub("//", (merged.stderr or merged.output or "").strip())[:300]
        )

    new = _commit_info("HEAD")
    changed = _line(_git("diff", "--name-only", f"{old}..{new['sha']}"))
    requirements_changed = "requirements.txt" in changed.split("\n")
    return {
        "updated": True,
        "from": old,
        "to": new,
        "commits": status["behind"],
        "requirements_changed": requirements_changed,
        "restart_required": True,
        "message": f"Updated {status['behind']} commit(s) to {new['sha']}. "
                   "Restart the server to run it"
                   + (", and re-run the installer first: requirements.txt "
                      "changed." if requirements_changed else "."),
    }
