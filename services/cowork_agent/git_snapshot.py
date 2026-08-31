"""Commit history and per-commit tree snapshots, read straight from git.

The Space UI's snapshot view asks four questions about a project:
which commits exist, what did the whole tree look like at one commit,
how much did each file change in that commit, and what did one file
contain there. Each answer comes from a
single git subprocess against the project's own repository — nothing is
indexed or cached on disk, because a commit is immutable and the log is
cheap at the depths the UI requests.

Hardening mirrors visualizer/space_index.py:

- A project without its own ``.git`` gets ``None``, never the enclosing
  repository's history (a plain folder nested in a dotfiles $HOME would
  otherwise inherit the parent's log).
- Every sha is regex-gated before it reaches a command line, and paths
  follow ``--`` separators, so neither can smuggle an option.
- Commit subjects are attacker-ish input (any byte but NUL/newline):
  output is framed on control bytes git itself emits (``%x01``/``%x02``),
  remaining control bytes are stripped, and decodes are non-strict.
- Any failure (no git binary, timeout, bad object) is ``None`` or an
  empty collection — the router turns that into its empty/404 shape and
  the UI shows a truthful message instead of a traceback.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

_GIT_TIMEOUT_S = 10.0

# Full or abbreviated object name. Anything else never reaches argv.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# The snapshot payload is bounded like space.json is: a tree bigger than
# this is truncated (largest files kept) and flagged, so one monorepo
# commit cannot ship a multi-megabyte response.
MAX_TREE_ENTRIES = 4000


def normalize_sha(sha: str) -> Optional[str]:
    """Lower-cased sha iff it looks like one; None otherwise."""
    s = (sha or "").strip().lower()
    return s if _SHA_RE.match(s) else None


def _is_repo(pdir: Path) -> bool:
    return (pdir / ".git").exists()


def _run_git(pdir: Path, *args: str) -> Optional[bytes]:
    """Run one git command in ``pdir``; stdout bytes, or None on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(pdir), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _clean(text: str) -> str:
    return _CTRL_RE.sub(" ", text).strip()


# One \x01-framed header per commit; \x02 splits the fields inside it.
_LOG_FORMAT = "%x01%H%x02%h%x02%aI%x02%s"


def _parse_log(raw: bytes) -> list[dict]:
    """Parse ``--pretty=_LOG_FORMAT [--shortstat]`` output into commit dicts."""
    commits: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        if line.startswith("\x01"):
            parts = line[1:].split("\x02")
            if len(parts) != 4:
                continue  # fabricated header: a control byte inside a subject
            sha, short, date, subject = parts
            if not _SHA_RE.match(sha):
                continue
            commits.append(
                {
                    "sha": sha,
                    "short": _clean(short),
                    "date": _clean(date),
                    "subject": _clean(subject),
                    "files_changed": None,
                }
            )
        elif commits and "changed" in line:
            # --shortstat: " 3 files changed, 10 insertions(+), 2 deletions(-)"
            m = re.match(r"\s*(\d+) files? changed", line)
            if m:
                commits[-1]["files_changed"] = int(m.group(1))
    return commits


def list_commits(pdir: Path, limit: int = 40) -> Optional[list[dict]]:
    """Newest-first commits: sha, short, ISO date, subject, files_changed.

    ``None`` when the directory is not its own git repository (distinct
    from an empty repo, which is an empty list).
    """
    if not _is_repo(pdir):
        return None
    raw = _run_git(
        pdir,
        "log",
        f"--max-count={max(1, min(int(limit), 200))}",
        "--date=iso-strict",
        f"--pretty=format:{_LOG_FORMAT}",
        "--shortstat",
    )
    if raw is None:
        # An empty repository has no HEAD, and `git log` fails on it; an
        # initialized-but-uncommitted project is still a repo with no commits.
        return [] if _run_git(pdir, "rev-parse", "--git-dir") is not None else None
    return _parse_log(raw)


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Bounds the full-history parse a day query walks. At ~60 bytes a line this
# is ~6 MB of text — far past any workspace repo, cheap enough to not index.
_MAX_DAY_SCAN = 100_000


def valid_day(day: str) -> bool:
    """True iff ``day`` is a YYYY-MM-DD string a day query will accept."""
    return bool(_DAY_RE.match(day or ""))


def commits_on_day(pdir: Path, day: str) -> Optional[list[dict]]:
    """Every commit whose author date falls on one ISO day, newest first.

    The timeline's dots aggregate by author day (space_index uses
    ``--date=short``), so this filters the same field rather than trusting
    ``--since``/``--until``, which cut on commit date and drift from the
    dots whenever history was rebased. No ``--shortstat``: a day query is
    one line per commit over the full log, kept cheap on purpose.
    """
    if not _DAY_RE.match(day or ""):
        return None
    if not _is_repo(pdir):
        return None
    raw = _run_git(
        pdir, "log", f"--max-count={_MAX_DAY_SCAN}",
        "--date=iso-strict", f"--pretty=format:{_LOG_FORMAT}",
    )
    if raw is None:
        return [] if _run_git(pdir, "rev-parse", "--git-dir") is not None else None
    return [c for c in _parse_log(raw) if c["date"][:10] == day]


def _parse_numstat(raw: bytes) -> dict[str, dict]:
    """``diff-tree --numstat -z`` → path → {added, deleted}.

    Records are ``added\tdeleted\tpath\0``; a rename instead emits an
    empty path and follows with ``old\0new\0``, and the churn belongs to
    the new path (the one that exists in this commit's tree). Binary files
    report ``-`` for both counts — recorded as None rather than 0, because
    "no text lines changed" and "we cannot count lines" are different
    facts and the UI draws them differently.
    """
    out: dict[str, dict] = {}
    fields = raw.split(b"\x00")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if not rec:
            i += 1
            continue
        parts = rec.split(b"\t")
        if len(parts) < 3:
            i += 1
            continue
        added_s, deleted_s, path_b = parts[0], parts[1], parts[2]
        if path_b == b"":  # rename/copy: old and new follow as separate fields
            if i + 2 >= len(fields):
                break
            path_b = fields[i + 2]
            i += 3
        else:
            i += 1
        def _num(v: bytes) -> Optional[int]:
            try:
                return int(v)
            except ValueError:
                return None  # "-" for binary
        out[path_b.decode("utf-8", errors="replace")] = {
            "added": _num(added_s),
            "deleted": _num(deleted_s),
        }
    return out


def commit_snapshot(pdir: Path, sha: str) -> Optional[dict]:
    """The full tree at one commit, plus what that commit touched.

    Returns ``{commit, tree, touched, churn, deleted, truncated,
    total_files}``: ``churn`` maps path → {added, deleted} line counts for
    this commit, which the UI raises as terrain height —
    ``tree`` is every blob as ``{path, size}``; ``touched`` maps path →
    A/M/R for files present in the snapshot that this commit changed;
    ``deleted`` lists paths the commit removed (absent from the tree by
    definition). ``None`` when the project is not a repo or the sha does
    not name a commit here.
    """
    ref = normalize_sha(sha)
    if ref is None or not _is_repo(pdir):
        return None

    meta_raw = _run_git(
        pdir, "log", "-1", "--date=iso-strict",
        f"--pretty=format:{_LOG_FORMAT}", ref, "--",
    )
    if not meta_raw:
        return None
    meta = _parse_log(meta_raw)
    if not meta:
        return None

    # NUL framing end to end: -z stops path quoting, so names with spaces,
    # quotes or newlines arrive intact and unambiguous.
    tree_raw = _run_git(pdir, "ls-tree", "-r", "-l", "-z", "--full-tree", ref)
    if tree_raw is None:
        return None
    tree: list[dict] = []
    for entry in tree_raw.split(b"\x00"):
        if not entry:
            continue
        head, _, path = entry.partition(b"\t")
        fields = head.split()  # mode type sha size
        if len(fields) != 4 or fields[1] != b"blob":
            continue  # submodule commits and trees carry no size
        try:
            size = int(fields[3])
        except ValueError:
            size = 0  # "-" for odd objects
        tree.append({"path": path.decode("utf-8", errors="replace"), "size": size})

    total = len(tree)
    truncated = total > MAX_TREE_ENTRIES
    if truncated:
        # Keep the largest files: they carry the treemap's shape, and the
        # flag tells the UI the tail was dropped rather than absent.
        tree.sort(key=lambda e: e["size"], reverse=True)
        tree = tree[:MAX_TREE_ENTRIES]

    # --root makes the very first commit diff against the empty tree
    # instead of producing nothing.
    diff_raw = _run_git(
        pdir, "diff-tree", "--no-commit-id", "--name-status",
        "-r", "-z", "--root", ref, "--",
    )
    touched: dict[str, str] = {}
    deleted: list[str] = []
    if diff_raw:
        fields = [f for f in diff_raw.split(b"\x00")]
        i = 0
        while i < len(fields):
            status = fields[i].decode("utf-8", errors="replace")
            if not status:
                i += 1
                continue
            code = status[0]
            if code in ("R", "C") and i + 2 < len(fields):
                # rename/copy: status, old path, new path
                new = fields[i + 2].decode("utf-8", errors="replace")
                touched[new] = "R"
                i += 3
            elif i + 1 < len(fields):
                path = fields[i + 1].decode("utf-8", errors="replace")
                if code == "D":
                    deleted.append(path)
                elif code in ("A", "M", "T"):
                    touched[path] = "A" if code == "A" else "M"
                i += 2
            else:
                break

    # A second cheap pass: name-status answers "what kind of change",
    # numstat answers "how big". Both are one diff-tree over the same
    # commit, so the pair costs no walk the UI could avoid.
    churn = _parse_numstat(
        _run_git(
            pdir, "diff-tree", "--no-commit-id", "--numstat",
            "-r", "-z", "--root", ref, "--",
        ) or b""
    )

    return {
        "commit": meta[0],
        "tree": tree,
        "touched": touched,
        "churn": {p: c for p, c in churn.items() if p in touched},
        "deleted": deleted,
        "truncated": truncated,
        "total_files": total,
    }


def _safe_relative_path(relative_path: str) -> Optional[str]:
    """The same path rules project_layout enforces, for a git pathspec."""
    rel = relative_path or ""
    if "\x00" in rel or rel.startswith(("/", "\\")):
        return None
    rel = rel.replace("\\", "/").strip("/")
    if not rel:
        return None
    parts = rel.split("/")
    if any(p in ("..", ".") or p == "" for p in parts):
        return None
    return rel


def file_at_commit(
    pdir: Path, sha: str, relative_path: str, *, max_bytes: int
) -> Optional[dict]:
    """One blob's content at one commit: {name, size_bytes, truncated, content}.

    ``None`` for a bad sha, an unsafe path, a non-repo, or a path absent
    from that commit's tree. Raises ``ValueError`` only for a malformed
    relative path, mirroring project_layout.read_project_file.
    """
    ref = normalize_sha(sha)
    rel = _safe_relative_path(relative_path)
    if rel is None:
        raise ValueError("relative_path is malformed")
    if ref is None or not _is_repo(pdir):
        return None
    raw = _run_git(pdir, "show", f"{ref}:{rel}")
    if raw is None:
        return None
    truncated = len(raw) > max_bytes
    content = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "name": rel.rsplit("/", 1)[-1],
        "size_bytes": len(raw),
        "truncated": truncated,
        "content": content,
    }
