"""
Tar.gz building + extraction for project snapshots.

Three decisions worth understanding before editing:

1. **How `.gitignore` is honoured.** If the project is a git repo (has
   a `.git` entry — a directory in a normal clone, a *file* in a
   worktree or submodule) we defer to `git ls-files --cached --others
   --exclude-standard` for the inclusion list. That gives us exact
   gitignore semantics (negation, `**`, nested ignore files) without
   pulling in a dependency. If the project is NOT a git repo, we walk
   the tree and apply only the mandatory-exclude list below — the
   user's own `.gitignore` is silently ignored in that case. Document
   this clearly so users who care about size will `git init` their
   project.

2. **…except the synced tier, which is force-included** (syncplan T21).
   `<project>/.xo/` carries `project.json`, and `project.json` carries
   the `pid` — the identity that makes a restored copy the SAME project
   rather than a new one. `project_template/AGENTS.md` tells every agent
   that directory is "gitignored", so an agent acting on that instruction
   inside a repo drops it from `git ls-files` and the backup silently
   loses the pid; restore then mints a fresh UUID and the two copies
   diverge, unrecoverably and with no error anywhere. Un-ignoring
   blanket `.gitignore` lines (`visualizer/migrate.py`) only covers the
   blanket forms — `.xo/*`, `**/.xo/` and a negation all survive it — so
   `_force_included` walking the directory itself is the actual
   guarantee. Mandatory excludes still apply to what it finds.

3. **Mandatory excludes always apply.** Even inside a git repo where
   `node_modules/` is tracked (uncommon but valid), we still skip
   secrets and build artefacts at backup time. The blob is encrypted
   but a leaked passphrase shouldn't also leak `.env`. The list lives
   here as `MANDATORY_EXCLUDE_NAMES` / `MANDATORY_EXCLUDE_PATTERNS`;
   keep them in sync with the design doc.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import tarfile
from pathlib import Path

from services.cowork_agent import project_layout


# Path-component names that always cause a file or directory to be skipped.
# Compared against the *basename* of each path component, so this matches
# `.env` at any depth, not just at project root.
MANDATORY_EXCLUDE_NAMES = frozenset({
    ".env",
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
})

# fnmatch-style patterns applied to basenames; catches things like .env.local,
# .env.production, *.sock.
MANDATORY_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".env.*",
    "*.sock",
)


def _is_excluded(name: str) -> bool:
    if name in MANDATORY_EXCLUDE_NAMES:
        return True
    return any(fnmatch.fnmatchcase(name, pat) for pat in MANDATORY_EXCLUDE_PATTERNS)


def _path_has_excluded_component(rel_parts: tuple[str, ...]) -> bool:
    """Reject if any path component (dir or file) is on the exclude list.

    e.g. `node_modules/foo/index.js` is rejected because `node_modules`
    appears as a component, even though `index.js` itself is fine.
    """
    return any(_is_excluded(part) for part in rel_parts)


def _synced_tier_dir(project_dir: Path) -> Path:
    """The project's synced-tier directory — the one that must always travel.

    Its name comes from ``project_layout``, which owns the on-disk layout
    decision (``tests/test_path_chokepoint_guard.py``); this module never
    spells it. Resolved against the ``project_dir`` we were handed rather than
    against the projects root, so a backup of a directory outside the root —
    every test, and any future caller — still finds it.
    """
    return project_dir / project_layout.xo_dir(project_dir.name).name


def _force_included(project_dir: Path) -> list[str]:
    """Project-relative paths that go in the tarball whatever git says.

    This is the guarantee described at the top of the module. It walks the
    synced tier directly instead of asking git about it, so no ``.gitignore``
    form — blanket, ``.xo/*``, ``**/.xo/``, a negation, a nested ignore file —
    can take ``project.json`` (and with it the ``pid``) out of a backup.

    Mandatory excludes are NOT applied here: ``build_tarball`` applies them to
    the merged list, so a stray ``.env`` inside the synced tier is still
    dropped. Symlinked subtrees are not descended into (``os.walk`` default) —
    a link out of the project is not this project's content.
    """
    root = _synced_tier_dir(project_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    base_len = len(project_dir.parts)
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir_parts = Path(dirpath).parts[base_len:]
        for fname in filenames:
            found.append("/".join((*rel_dir_parts, fname)))
    return found


def _is_git_repo(project_dir: Path) -> bool:
    """True iff ``project_dir`` is the root of a git checkout.

    ``.git`` is a directory in a normal clone and a **file** holding
    ``gitdir: <path>`` in a worktree or a submodule. ``is_dir()`` read the
    second as "not a repo" and fell back to the tree walk, which honours no
    ``.gitignore`` at all — so a worktree checkout silently tarred everything
    the user had excluded.

    The file's first bytes are read rather than any ``.git`` file being
    accepted: a stray non-git ``.git`` file then stays on the walk path
    instead of turning a backup into a hard ``git ls-files`` failure.
    """
    marker = project_dir / ".git"
    if marker.is_dir():
        return True
    try:
        with marker.open("rb") as handle:
            return handle.read(8).startswith(b"gitdir:")
    except OSError:
        return False


async def _git_ls_files(project_dir: Path) -> list[str]:
    """Tracked + untracked-but-not-ignored files, as project-relative paths."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C", str(project_dir),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",  # NUL-delimited; handles paths with newlines/spaces
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {project_dir}: {stderr.decode('utf-8', 'replace').strip()}"
        )
    # Trailing NUL is normal; filter empties.
    return [p for p in stdout.decode("utf-8", "replace").split("\x00") if p]


def _walk_files(project_dir: Path) -> list[str]:
    """Walk the tree applying mandatory excludes only. Returns project-relative paths."""
    out: list[str] = []
    base_len = len(project_dir.parts)
    for dirpath, dirnames, filenames in os.walk(project_dir):
        # Mutate dirnames in place so os.walk skips excluded subtrees.
        dirnames[:] = [d for d in dirnames if not _is_excluded(d)]
        rel_dir_parts = Path(dirpath).parts[base_len:]
        for name in filenames:
            if _is_excluded(name):
                continue
            out.append("/".join((*rel_dir_parts, name)))
    return out


async def build_tarball(project_dir: Path, output_path: Path) -> int:
    """Write a gzipped tar of ``project_dir`` to ``output_path``.

    The tar's member names are relative to the project root (the
    directory's basename does NOT appear as a top-level prefix). Extract
    therefore writes content directly into the target directory.

    Returns the size in bytes of the resulting tarball.
    """
    if not project_dir.is_dir():
        raise FileNotFoundError(f"project_dir not found: {project_dir}")

    is_git_repo = _is_git_repo(project_dir)
    if is_git_repo:
        candidate_rel_paths = await _git_ls_files(project_dir)
    else:
        candidate_rel_paths = _walk_files(project_dir)
    # Force-include before the filter, so the mandatory excludes still apply.
    candidate_rel_paths = [*candidate_rel_paths, *_force_included(project_dir)]

    # Final filter: mandatory excludes apply even when git included the file
    # (a tracked .env is still a leaked secret risk). De-duplicated because the
    # force-include overlaps whatever git already listed.
    seen: set[str] = set()
    relative_paths: list[str] = []
    for rel in candidate_rel_paths:
        parts = tuple(rel.split("/"))
        if _path_has_excluded_component(parts):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        relative_paths.append(rel)

    relative_paths.sort()  # deterministic ordering for reproducible-ish tars

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz", compresslevel=6) as tar:
        for rel in relative_paths:
            abs_path = project_dir / rel
            # `arcname` controls the path inside the tar. Use forward slashes
            # for cross-platform predictability.
            tar.add(abs_path, arcname=rel, recursive=False)
    return output_path.stat().st_size


def extract_tarball(tarball_path: Path, target_dir: Path) -> None:
    """Extract ``tarball_path`` into ``target_dir``.

    Refuses any member whose resolved destination escapes ``target_dir``
    (tar-slip defence). Member permissions are preserved; ownership is
    not (running process owns everything).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target_resolved = target_dir.resolve()
    with tarfile.open(tarball_path, "r:gz") as tar:
        for member in tar.getmembers():
            dest = (target_dir / member.name).resolve()
            try:
                dest.relative_to(target_resolved)
            except ValueError:
                raise RuntimeError(
                    f"tarball member {member.name!r} would extract outside {target_dir} — refusing"
                )
        tar.extractall(target_dir)
