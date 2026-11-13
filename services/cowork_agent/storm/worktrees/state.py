"""A thread branch's git state, as a person catching up reads it (v2).

``read`` looks at a thread's branch from the project's own checkout (refs
are shared by every worktree, so the thread's tree need not exist) and says:

* ``base``: the project's default branch (``origin/HEAD``'s, else ``main``,
  else ``master``, else what the project folder has checked out), and
  ``ahead`` / ``behind``: the branch's commits it lacks, and its commits the
  branch lacks;
* ``commits``: the thread's own commits (since its ``base_commit``), and
  ``last_commit`` ``{sha, subject, at}`` when there is one;
* ``merged``, ``merged_at``, ``merged_by``: whether that work is in the
  default branch, locally or in its remote-tracking ref (``ancestor``),
  as equivalent patches (``patches``: a rebase or cherry-pick), or as the
  pull request says (``pr``: a squash merge on the forge), and when;
* ``upstream`` ``{ref, pushed, ahead, behind, gone}``, and ``unpushed``:
  the branch's commits no remote-tracking ref has;
* ``pr`` ``{number, url, state, draft, merged_at}`` when ``gh`` can tell
  (``pr_lookup``): it asks the forge, so it runs on its own schedule.

Every git command is local (``git.git``: no transport, no hooks). XO never
fetches, so "pushed" and the remote's default branch are what the last push
or fetch someone ran left in the remote-tracking refs. A branch that is gone
keeps what was last read of it (``branch_exists`` false): a removed thread
still says it was merged.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.storm.worktrees.git import git
from utils.commands import run

#: The fields of a state, in the order the API shows them.
FIELDS = ("branch", "branch_exists", "base", "ahead", "behind", "commits", "last_commit", "merged", "merged_at",
          "merged_by", "upstream", "unpushed", "pr")
#: How long ``gh`` may take to answer (it asks the forge over the network).
GH_TIMEOUT_S = 20.0
#: The pull requests ``gh`` lists for one head branch (the newest is shown).
GH_LIMIT = 5
_GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
           "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"}
_SEP = "\x1f"
_REF_FORMAT = _SEP.join(("%(refname)", "%(symref)", "%(objectname)", "%(upstream)", "%(upstream:track,nobracket)",
                         "%(subject)", "%(committerdate:iso-strict)"))


def empty(branch: str) -> dict[str, Any]:
    """What is known of a branch before anything was read."""
    return {"branch": branch, "branch_exists": False, "base": None, "ahead": None, "behind": None, "commits": None,
            "last_commit": None, "merged": False, "merged_at": None, "merged_by": None,
            "upstream": None, "unpushed": None, "pr": None}


async def read(project: Path, branch: str, base_commit: str | None, *,
               previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The branch's state now (the ``pr`` is ``previous``'s: ``pr_lookup``
    refreshes it). Raises nothing for a git that can't tell: the fields it
    couldn't read keep ``previous``'s values."""
    before = {**empty(branch), **(previous or {})}
    refs = await _refs(project, branch)
    tip = refs.get(f"refs/heads/{branch}")
    if tip is None:                          # gone: what was last read of it stays
        return {**before, "branch": branch, "branch_exists": False}
    state: dict[str, Any] = {**before, "branch": branch, "branch_exists": True}
    base, base_refs = await _default_branch(project, refs)
    state["base"] = base
    state["last_commit"] = None
    commits = await _count(project, f"{base_commit}..{tip['sha']}") if base_commit else None
    state["commits"] = commits
    if commits is None or commits > 0:
        state["last_commit"] = {"sha": tip["sha"], "subject": tip["subject"], "at": tip["at"]}
    state.update(await _against_base(project, tip["sha"], base_refs, base_commit, commits, before, state.get("pr")))
    state["upstream"], state["unpushed"] = await _upstream(project, branch, tip, refs)
    return state


def merge_pr(state: Mapping[str, Any], pr: Mapping[str, Any] | None) -> dict[str, Any]:
    """``state`` with a pull request ``gh`` just reported: a merged one
    marks the work merged (the forge knows of a squash merge git can't see)."""
    out = {**state, "pr": dict(pr) if pr else None}
    if pr and pr.get("state") == "MERGED" and not out.get("merged"):
        out.update(merged=True, merged_at=pr.get("merged_at"), merged_by="pr")
    return out


def changed(a: Mapping[str, Any] | None, b: Mapping[str, Any]) -> bool:
    return a is None or any(a.get(k) != b.get(k) for k in FIELDS)


# ── reading refs ─────────────────────────────────────────────────────────


async def _refs(project: Path, branch: str) -> dict[str, dict[str, Any]]:
    """The refs ``read`` needs, in one ``for-each-ref``: the branch, its
    remote-tracking copies, ``origin/HEAD``, and the usual default names."""
    patterns = [f"refs/heads/{branch}", f"refs/remotes/*/{branch}", "refs/remotes/origin/HEAD",
                "refs/heads/main", "refs/heads/master", "refs/remotes/origin/main", "refs/remotes/origin/master"]
    result = await git(project, "for-each-ref", f"--format={_REF_FORMAT}", *patterns)
    out: dict[str, dict[str, Any]] = {}
    if not result.ok:
        return out
    for line in result.output.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 7:
            continue
        name, symref, sha, upstream, track, subject, at = parts
        out[name] = {"symref": symref or None, "sha": sha, "upstream": upstream or None, "track": track,
                     "subject": subject, "at": at or None}
    return out


async def _default_branch(project: Path, refs: dict[str, dict[str, Any]]) -> tuple[str | None, list[str]]:
    """The default branch's name, and the refs that hold it (the local
    branch first, then its remote-tracking ref), those that exist."""
    origin_head = (refs.get("refs/remotes/origin/HEAD") or {}).get("symref")
    name = None
    if origin_head and origin_head.startswith("refs/remotes/origin/"):
        name = origin_head.removeprefix("refs/remotes/origin/")
    if name is None:
        for candidate in ("main", "master"):
            if f"refs/heads/{candidate}" in refs or f"refs/remotes/origin/{candidate}" in refs:
                name = candidate
                break
    if name is None:
        result = await git(project, "symbolic-ref", "--quiet", "--short", "HEAD")
        name = result.output.strip() if result.ok and result.output.strip() else None
    if name is None:
        return None, []
    held = []
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        if ref in refs or (await git(project, "rev-parse", "--verify", "--quiet", ref)).ok:
            held.append(ref)
    return name, held


async def _count(project: Path, *spec: str) -> int | None:
    result = await git(project, "rev-list", "--count", *spec)
    text = result.output.strip()
    return int(text) if result.ok and text.isdigit() else None


async def _against_base(project: Path, tip: str, base_refs: list[str], base_commit: str | None,
                        commits: int | None, before: Mapping[str, Any], pr: Mapping[str, Any] | None
                        ) -> dict[str, Any]:
    out: dict[str, Any] = {"ahead": None, "behind": None, "merged": False, "merged_at": None, "merged_by": None}
    if not base_refs:
        return out
    result = await git(project, "rev-list", "--left-right", "--count", f"{base_refs[0]}...{tip}")
    parts = result.output.split()
    if result.ok and len(parts) == 2 and all(p.isdigit() for p in parts):
        out["behind"], out["ahead"] = int(parts[0]), int(parts[1])
    if commits == 0:
        return out                          # nothing of the thread's own to be merged
    # The same tip already found merged: when stays as it was found.
    if before.get("merged") and before.get("merged_by") == "ancestor" and \
            (before.get("last_commit") or {}).get("sha") == tip:
        return {**out, "merged": True, "merged_at": before.get("merged_at"), "merged_by": "ancestor"}
    for ref in base_refs:
        ahead = out["ahead"] if ref == base_refs[0] else await _count(project, f"{ref}..{tip}")
        if ahead == 0:
            return {**out, "merged": True, "merged_at": await _merged_at(project, tip, ref), "merged_by": "ancestor"}
    if base_commit:
        cherry = await git(project, "cherry", base_refs[0], tip, base_commit)
        lines = [line for line in cherry.output.splitlines() if line.strip()] if cherry.ok else []
        if lines and all(line.startswith("-") for line in lines):
            return {**out, "merged": True, "merged_at": None, "merged_by": "patches"}
    if pr and pr.get("state") == "MERGED":
        return {**out, "merged": True, "merged_at": pr.get("merged_at"), "merged_by": "pr"}
    return out


async def _merged_at(project: Path, tip: str, ref: str) -> str | None:
    """When ``tip`` entered ``ref``: the oldest commit of ``ref``'s
    first-parent line that descends from it is the merge (its date), or,
    when that commit's first parent is ``tip`` itself (a fast-forward), the
    tip's own date."""
    result = await git(project, "rev-list", "--first-parent", "--ancestry-path", f"{tip}..{ref}")
    shas = result.output.split() if result.ok else []
    commit = shas[-1] if shas else tip
    shown = await git(project, "show", "-s", f"--format=%P{_SEP}%cI", commit)
    if not shown.ok or _SEP not in shown.output:
        return None
    parents, at = shown.output.strip().split(_SEP, 1)
    if commit != tip and parents.split()[:1] == [tip]:
        tip_shown = await git(project, "show", "-s", "--format=%cI", tip)
        return tip_shown.output.strip() or None if tip_shown.ok else None
    return at or None


async def _upstream(project: Path, branch: str, tip: Mapping[str, Any], refs: dict[str, dict[str, Any]]
                    ) -> tuple[dict[str, Any], int | None]:
    upstream = tip.get("upstream")
    track = tip.get("track") or ""
    copies = sorted(name for name in refs if name.startswith("refs/remotes/") and name.endswith(f"/{branch}"))
    ref = upstream or (copies[0] if copies else None)
    view: dict[str, Any] = {"ref": ref.removeprefix("refs/remotes/") if ref else None,
                            "pushed": bool(copies) or (bool(upstream) and track != "gone"),
                            "ahead": None, "behind": None, "gone": track == "gone"}
    if ref and track != "gone":
        result = await git(project, "rev-list", "--left-right", "--count", f"{ref}...{tip['sha']}")
        parts = result.output.split()
        if result.ok and len(parts) == 2 and all(p.isdigit() for p in parts):
            view["behind"], view["ahead"] = int(parts[0]), int(parts[1])
    remotes = await git(project, "remote")
    unpushed = await _count(project, tip["sha"], "--not", "--remotes") if remotes.ok and remotes.output.strip() \
        else None
    return view, unpushed


# ── the pull request, from the forge ─────────────────────────────────────


def gh_available() -> bool:
    return shutil.which("gh") is not None


async def pr_lookup(project: Path, branch: str) -> tuple[bool, dict[str, Any] | None]:
    """``(answered, pr)``: the newest pull request whose head is ``branch``
    (an open one first), or None when there is none. ``answered`` is False
    when ``gh`` couldn't tell (not installed, not signed in, not a GitHub
    remote, offline): what was known stays."""
    if not gh_available():
        return False, None
    result = await run(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", str(GH_LIMIT),
                        "--json", "number,url,state,isDraft,mergedAt,updatedAt,headRefName"],
                       cwd=project, timeout=GH_TIMEOUT_S, env={**os.environ, **_GH_ENV}, separate_stderr=True,
                       log_output=False)
    if not result.ok:
        return False, None
    try:
        items = json.loads(result.output or "[]")
    except ValueError:
        return False, None
    items = [i for i in items if isinstance(i, dict) and i.get("headRefName") == branch]
    if not items:
        return True, None
    items.sort(key=lambda i: (i.get("state") == "OPEN", i.get("updatedAt") or ""), reverse=True)
    pr = items[0]
    return True, {"number": pr.get("number"), "url": pr.get("url"), "state": pr.get("state"),
                  "draft": bool(pr.get("isDraft")), "merged_at": pr.get("mergedAt") or None}
