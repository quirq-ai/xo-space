"""Async git plumbing for the commit relay. Every function is failure-tolerant:
git errors return None/False/empty — the relay must degrade, never crash.

`local_remote_head` is the one synchronous, subprocess-free reader: it looks at
the remote-tracking ref git itself updates after `git push`, so a machine can
notice its own push without a network call."""
from __future__ import annotations

import os
import re
from pathlib import Path

from utils.commands import run

_SEP = "\x1f"  # unit separator: cannot appear in git subjects/authors


async def _run(repo_dir, *args) -> tuple[int, str, str]:
    """`git -C <repo_dir> <args>` through the one executor. (code, stdout, stderr);
    a missing git binary or a runner exception is a non-zero code with the
    reason in stderr, so every caller's failure branch already covers it."""
    res = await run(["git", "-C", str(repo_dir), *args], separate_stderr=True)
    if res.binary_missing or res.exception:
        return res.returncode, "", res.output
    return res.returncode, res.stdout, res.stderr


async def origin_url(repo_dir) -> str | None:
    code, out, _ = await _run(repo_dir, "config", "--get", "remote.origin.url")
    out = out.strip()
    return out if code == 0 and out else None


async def remote_head(repo_dir, branch: str) -> str | None:
    """SHA of origin's branch tip via ls-remote (ref names only, no objects)."""
    code, out, _ = await _run(repo_dir, "ls-remote", "origin", f"refs/heads/{branch}")
    if code != 0:
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    sha = line.split("\t")[0].strip() if line else ""
    return sha or None


def local_remote_head(repo_dir, branch: str) -> str | None:
    """SHA recorded in .git/refs/remotes/origin/<branch> (loose or packed).
    None when the ref is unknown or .git is not a plain directory."""
    git_dir = Path(repo_dir) / ".git"
    if not git_dir.is_dir():
        return None
    loose = git_dir / "refs" / "remotes" / "origin" / branch
    try:
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8").strip()
            return sha if len(sha) in (40, 64) else None
        packed = git_dir / "packed-refs"
        if packed.is_file():
            want = f"refs/remotes/origin/{branch}"
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == want:
                    return parts[0]
    except OSError:
        return None
    return None


async def enumerate_hashes(repo_dir, since_sha: str, head_sha: str) -> list[str]:
    """Hashes in since..head from LOCAL history (oldest first), no fetch.
    Falls back to [head_sha] when the range cannot be computed locally."""
    if since_sha:
        code, out, _ = await _run(repo_dir, "rev-list", "--reverse", f"{since_sha}..{head_sha}")
        if code == 0:
            hashes = [h.strip() for h in out.splitlines() if h.strip()]
            if hashes:
                return hashes
    return [head_sha]


async def fetch_origin(repo_dir) -> tuple[bool, str]:
    code, _, err = await _run(repo_dir, "fetch", "origin", "--quiet")
    return code == 0, err.strip()


async def commit_present(repo_dir, sha: str) -> bool:
    if not sha:
        return False
    code, _, _ = await _run(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}")
    return code == 0


async def recent_commits(repo_dir, branch: str, limit: int = 20) -> tuple[list[dict], str]:
    """Minimal feed: hash, subject, author, ISO date. Prefers origin/<branch>
    so fetched-but-unmerged commits show; falls back to HEAD."""
    fmt = f"--format=%H{_SEP}%s{_SEP}%an{_SEP}%cI"
    for source in (f"origin/{branch}", "HEAD"):
        code, out, _ = await _run(repo_dir, "log", source, f"-n{int(limit)}", fmt)
        if code != 0:
            continue
        commits = []
        for line in out.splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "subject": parts[1],
                                "author": parts[2], "date": parts[3]})
        return commits, source
    return [], "none"


SHA_RE = re.compile(r"[0-9a-f]{7,64}")

# Repo content comes from other people: never run a diff driver or textconv
# filter their .gitattributes might name, show renames as delete + add so
# paths stay literal, and keep colour codes out of the text.
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")


async def resolve_commit(repo_dir, sha: str) -> str | None:
    """Full hash when `sha` names a commit in this repo, else None."""
    code, out, _ = await _run(repo_dir, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    out = out.strip()
    return out if code == 0 and out else None


async def on_branch(repo_dir, sha: str, branch: str) -> bool:
    """True when `sha` is reachable from origin/<branch>: the Sharing page only
    shows commits the relay fetched onto that ref."""
    code, _, _ = await _run(repo_dir, "merge-base", "--is-ancestor", sha, f"origin/{branch}")
    return code == 0


async def first_parent(repo_dir, sha: str) -> str | None:
    """None for a root commit. A merge is read against its first parent: what
    the branch looked like before the merge landed."""
    return await resolve_commit(repo_dir, f"{sha}^1")


def _diff_argv(sha: str, parent: str | None, *extra: str) -> list[str]:
    if parent:
        return ["diff", *_DIFF_FLAGS, *extra, parent, sha]
    return ["diff-tree", "-r", "--root", "--no-commit-id", *_DIFF_FLAGS, *extra, sha]


def parse_numstat_z(out: str) -> list[dict]:
    """`--numstat -z` records ("adds\\tdels\\tpath\\0"); binary files report
    "-" for both counts."""
    files: list[dict] = []
    for record in out.split("\0"):
        parts = record.lstrip("\n").split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        adds, dels, path = parts
        binary = adds == "-" and dels == "-"
        try:
            additions = None if binary else int(adds)
            deletions = None if binary else int(dels)
        except ValueError:
            continue
        files.append({"path": path, "additions": additions, "deletions": deletions, "binary": binary})
    return files


async def commit_files(repo_dir, sha: str, parent: str | None) -> list[dict] | None:
    code, out, _ = await _run(repo_dir, *_diff_argv(sha, parent, "--numstat", "-z"))
    return parse_numstat_z(out) if code == 0 else None


async def commit_diff(repo_dir, sha: str, parent: str | None, path: str) -> str | None:
    code, out, _ = await _run(repo_dir, *_diff_argv(sha, parent, "-p"), "--", path)
    return out if code == 0 else None


async def clone(url: str, dest, *, config_args: list[str] | None = None,
                cwd=None, timeout: float = 600.0) -> tuple[bool, str, bool]:
    """`git [config_args] clone -- <url> <dest>` with a hard timeout.
    Returns (ok, stderr, timed_out). `config_args` carry `-c` credential
    overrides so the token never appears in the URL or in .git/config."""
    argv = ["git", *(config_args or []), "clone", "--", url, str(dest)]
    # The runner closes stdin, so with GIT_TERMINAL_PROMPT=0 git can never
    # block on a credential prompt; a private repo fails fast instead.
    res = await run(argv, cwd=cwd, timeout=timeout,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    separate_stderr=True)
    if res.timed_out:
        return False, f"timed out after {timeout}s", True
    if res.binary_missing or res.exception:
        return False, res.output, False
    return res.ok, res.stderr, False


async def head_sha(repo_dir) -> str | None:
    code, out, _ = await _run(repo_dir, "rev-parse", "HEAD")
    out = out.strip()
    return out if code == 0 and out else None


async def apply_ff(repo_dir, branch: str) -> tuple[bool, str]:
    """`git merge --ff-only origin/<branch>`: the one write the relay ever
    makes to a working tree, and only the write a person would make by hand
    with the command the UI shows. --ff-only means it can never create a
    merge commit or touch a diverged branch; git refuses and we report why
    (first meaningful stderr line, trimmed)."""
    code, _, err = await _run(repo_dir, "merge", "--ff-only", f"origin/{branch}")
    if code == 0:
        return True, ""
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    detail = " ".join(lines)[:300] if lines else f"git merge exited with {code}"
    return False, detail


async def behind_count(repo_dir, branch: str) -> int | None:
    """Commits on origin/<branch> not yet in HEAD: 'fetched, not applied'."""
    code, out, _ = await _run(repo_dir, "rev-list", "--count", f"HEAD..origin/{branch}")
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None
