"""Git history of one file inside a project, for the previewer.

The previewer's version picker answers two questions only the project's
own git repository can: "who edited this file, and when" (the version
list, via ``git log --follow``), and "what did it say back then" (one
version's content, via ``git show``). These helpers shape that output;
they never write to the repository.

Path validation is delegated to ``project_layout.read_project_file``
(called with ``max_bytes=0``), so the address space stays exactly the
one every other project endpoint enforces: project id plus a
project-relative path, resolved inside the project root. On top of
that, the *repository* the history is read from must itself live inside
the project: a project folder that is not a repo, sitting inside some
larger checkout, must not leak the outer repository's history into the
UI — that history was never part of the project's address space.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from services.cowork_agent.project_layout import project_dir, read_project_file

GIT_TIMEOUT_SECONDS = 10
# One version's content — same ceiling as the live file preview
# (PREVIEW_MAX_BYTES in the BFF).
VERSION_MAX_CHARS = 256 * 1024
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{4,40}")
# Fields split by unit separators, records by a record separator: none of
# the four can appear in a hash, a name, an ISO date, or a one-line subject
# the way a subject could contain any printable delimiter.
_LOG_FORMAT = "%x1e%H%x1f%an%x1f%aI%x1f%s"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """Run git, or return ``None`` when git itself is unavailable."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _repo_toplevel(file_dir: Path, project_root: Path) -> Path | None:
    """The repo root owning ``file_dir``, iff it is inside the project."""
    proc = _git(["rev-parse", "--show-toplevel"], cwd=file_dir)
    if proc is None or proc.returncode != 0:
        return None
    toplevel = Path(proc.stdout.strip())
    try:
        toplevel.resolve().relative_to(project_root)
    except ValueError:
        return None
    return toplevel


def _parse_stat(value: str) -> int | None:
    """A numstat count: an int, or ``None`` for binary's ``-``."""
    try:
        return int(value)
    except ValueError:
        return None


def _numstat_path(raw: str) -> str:
    """The file's name *at that commit*, from a numstat path field.

    numstat spells a rename ``old => new`` — or, with a shared prefix,
    ``dir/{old => new}/file`` — and the new side is the name the commit
    left the file with, which is the name ``git show`` must be asked
    about when diffing that commit later.
    """
    if "{" in raw and " => " in raw:
        return re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", raw).replace("//", "/")
    if " => " in raw:
        return raw.split(" => ")[-1]
    return raw


def _parse_log(stdout: str) -> list[dict]:
    """Split ``--format=_LOG_FORMAT --numstat`` output into commit dicts.

    Each \\x1e record is a header line (hash, author, ISO date, subject)
    followed by numstat lines; ``--follow`` limits the log to one path,
    so the first numstat line is the file's own +/- for that commit. A
    commit that only renames the file has no numstat counts — its entry
    keeps ``None`` there, which the UI reads as "nothing to show".
    """
    items: list[dict] = []
    for record in stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        head, _, stats = record.partition("\n")
        fields = head.split("\x1f")
        if len(fields) != 4:
            continue
        commit_hash, author, date, subject = fields
        additions = deletions = path = None
        for line in stats.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                additions = _parse_stat(parts[0])
                deletions = _parse_stat(parts[1])
                path = _numstat_path("\t".join(parts[2:]))
                break
        items.append(
            {
                "hash": commit_hash,
                "short_hash": commit_hash[:9],
                "author": author,
                "date": date,
                "subject": subject,
                "additions": additions,
                "deletions": deletions,
                "path": path,
            }
        )
    return items


def _locate_repo_file(name: str, relative_path: str) -> dict | None:
    """Resolve a project file and the repository owning it, if any.

    Validation is delegated to ``read_project_file`` (see the module
    docstring); ``toplevel`` and ``in_repo`` are ``None`` when no
    repository inside the project owns the file.
    """
    meta = read_project_file(name, relative_path, max_bytes=0)
    if meta is None:
        return None
    project_id = meta["project_id"]
    rel = meta["relative_path"]
    root = project_dir(project_id).resolve()
    target = root / rel
    toplevel = _repo_toplevel(target.parent, root)
    in_repo = (
        target.resolve().relative_to(toplevel.resolve()).as_posix()
        if toplevel is not None
        else None
    )
    return {
        "project_id": project_id,
        "rel": rel,
        "toplevel": toplevel,
        "in_repo": in_repo,
    }


def file_git_history(name: str, relative_path: str, *, limit: int = 50) -> dict | None:
    """Commits that touched one project file, newest first.

    Returns ``None`` when the project or the file does not exist, and
    raises ``ValueError`` for an unsafe ``relative_path`` — the same
    contract as :func:`project_layout.read_project_file`, which performs
    the validation. A project with no git repository (or with git
    missing from the host) reports ``is_repo: False`` rather than
    erroring: for the previewer that is a state, not a failure.
    """
    located = _locate_repo_file(name, relative_path)
    if located is None:
        return None

    result = {
        "project_id": located["project_id"],
        "relative_path": located["rel"],
        "is_repo": False,
        "items": [],
    }
    toplevel = located["toplevel"]
    if toplevel is None:
        return result
    result["is_repo"] = True

    in_repo = located["in_repo"]
    proc = _git(
        [
            "log",
            "--follow",
            "--numstat",
            "-n",
            str(limit),
            f"--format={_LOG_FORMAT}",
            "--",
            in_repo,
        ],
        cwd=toplevel,
    )
    # An empty repo (no HEAD yet) makes git log fail; an untracked file
    # makes it succeed with no output. Both are honestly "no commits".
    if proc is not None and proc.returncode == 0:
        result["items"] = _parse_log(proc.stdout)
    return result


def _safe_repo_pathspec(value: str) -> str:
    """A historical path handed back by a client, made safe for git.

    It came from this module's own history payload, but by the time it
    returns it is client input: it must stay a relative, option-free,
    traversal-free path. It is only ever used as a pathspec *inside* the
    repository, so the blast radius of a novel bypass is showing a diff
    of some other file in the same repo — content the same client can
    already read — but there is no reason to accept garbage.
    """
    if (
        not value
        or "\x00" in value
        or value.startswith(("/", "\\", "-"))
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("commit_path is not a safe repository path")
    return value


def read_file_at_commit(
    name: str,
    relative_path: str,
    commit: str,
    *,
    commit_path: str | None = None,
    max_chars: int = VERSION_MAX_CHARS,
) -> dict | None:
    """One version of a project file: its content at one commit.

    ``relative_path`` is the file as it exists *today* — it anchors
    validation and repo discovery. ``commit_path`` is the file's name at
    that commit (the ``path`` field of the matching history item), which
    differs from today's name across renames; without it, ``git show``
    would be asked about a path the old commit never knew.

    Returns ``None`` when the project or the file does not exist, and
    raises ``ValueError`` for an unsafe path or a malformed commit.
    ``content`` is ``None`` when the version cannot be produced — no
    repository owns the file, the commit is unknown, or the path was not
    in that commit's tree — and the ``is_repo`` flag says which of those
    it was.
    """
    if not _COMMIT_RE.fullmatch(commit or ""):
        raise ValueError("commit must be an abbreviated or full hex hash")
    located = _locate_repo_file(name, relative_path)
    if located is None:
        return None

    pathspec = (
        _safe_repo_pathspec(commit_path)
        if commit_path is not None
        else located["in_repo"]
    )
    result = {
        "project_id": located["project_id"],
        "relative_path": located["rel"],
        "commit": commit,
        "name": (pathspec or located["rel"]).rsplit("/", 1)[-1],
        "is_repo": False,
        "content": None,
        "truncated": False,
    }
    toplevel = located["toplevel"]
    if toplevel is None:
        return result
    result["is_repo"] = True

    proc = _git(["show", f"{commit}:{pathspec}"], cwd=toplevel)
    if proc is None or proc.returncode != 0:
        return result
    content = proc.stdout
    if len(content) > max_chars:
        content = content[:max_chars]
        result["truncated"] = True
    result["content"] = content
    return result
