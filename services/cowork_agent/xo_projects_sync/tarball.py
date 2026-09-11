"""Tar.gz building + extraction for project snapshots."""

from __future__ import annotations

import fnmatch
import os
import tarfile
from pathlib import Path

from services.cowork_agent import project_layout
from utils.commands import run


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
    """The project's synced-tier directory — the one that must always travel."""
    return project_dir / project_layout.xo_dir(project_dir.name).name


def _force_included(project_dir: Path) -> list[str]:
    """Project-relative paths that go in the tarball whatever git says."""
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
    """True iff ``project_dir`` is the root of a git checkout."""
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
    res = await run(
        ["git", "-C", str(project_dir), "ls-files", "--cached", "--others", "--exclude-standard",
         "-z"],  # NUL-delimited; handles paths with newlines/spaces
        separate_stderr=True,
    )
    if res.binary_missing:
        raise FileNotFoundError(res.output)
    if res.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {project_dir}: {(res.stderr or res.output).strip()}"
        )
    # Trailing NUL is normal; filter empties.
    return [p for p in res.output.split("\x00") if p]


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

    # Final filter: mandatory excludes apply even when git included the file (a
    # tracked .env is still a leaked secret risk).
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


def _member_path_parts(value: str, *, label: str) -> tuple[str, ...]:
    """Return safe archive-relative path parts for a member or hard-link target."""
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise RuntimeError(f"tarball {label} {value!r} is absolute — refusing")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if not parts:
        raise RuntimeError(f"tarball {label} {value!r} is empty — refusing")
    if ".." in parts:
        raise RuntimeError(f"tarball {label} {value!r} contains '..' — refusing")
    return parts


def _is_descendant_of(parts: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return parts[:len(parent)] == parent


def extract_tarball(tarball_path: Path, target_dir: Path) -> None:
    """Extract ``tarball_path`` into ``target_dir``.

    Refuses unsafe member paths and hard-link targets before writing anything.
    Symlink targets are preserved verbatim because project snapshots can
    legitimately contain both relative and absolute links; no later member may
    write through a symlink in the archive. Extraction explicitly uses
    tarfile's ``tar`` filter so this policy does not change with Python's
    default extraction filter.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target_resolved = target_dir.resolve()
    with tarfile.open(tarball_path, "r:gz") as tar:
        symlink_paths: list[tuple[str, ...]] = []
        for member in tar.getmembers():
            member_parts = _member_path_parts(member.name, label="member")
            if any(_is_descendant_of(member_parts, link) for link in symlink_paths):
                raise RuntimeError(
                    f"tarball member {member.name!r} writes through a symlink — refusing"
                )

            if member.islnk():
                link_parts = _member_path_parts(member.linkname, label="hard-link target")
                if any(_is_descendant_of(link_parts, link) for link in symlink_paths):
                    raise RuntimeError(
                        f"tarball hard-link target {member.linkname!r} crosses a symlink — refusing"
                    )

            # A symbolic link's target is intentionally not constrained to
            # target_dir: tracked project links and virtualenv links may point
            # outside it. The member-path check above prevents later archive
            # entries from using such a link to write outside target_dir.
            if member.issym():
                symlink_paths.append(member_parts)

            dest = (target_dir / member.name).resolve()
            try:
                dest.relative_to(target_resolved)
            except ValueError:
                raise RuntimeError(
                    f"tarball member {member.name!r} would extract outside {target_dir} — refusing"
                )

        try:
            tar.extractall(target_dir, filter="tar")
        except tarfile.FilterError as exc:
            raise RuntimeError(f"tarball member rejected during extraction: {exc}") from exc
