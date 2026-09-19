"""
Backup orchestration — single-project and all-projects.

Glue layer over `tarball`, `crypto`, `github`, `manifest`, `config`.
Endpoints in `api/xo_projects_sync/routes.py` call the
``backup_one`` / ``backup_all`` entry points; everything else here is
internal.

Staging: every call uses its own `tempfile.TemporaryDirectory` and
shallow-clones the project's GitHub repo into it. No persistent staging
directory exists between calls — disk usage is zero when idle.

Concurrency: a per-project `asyncio.Lock` serializes concurrent backups
of the same project (which would otherwise race on the remote's HEAD).
Different projects can back up in parallel; the global module lock from
the previous design is gone.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from services.cowork_agent.project_layout import list_projects, project_dir

from . import crypto, github, manifest, tarball
from .config import MAX_VERSIONS_PER_PROJECT, SyncConfig, repo_name_for


@dataclass
class BackupResult:
    project_id: str
    snapshot_id: str
    size_bytes: int
    sha256: str
    parts: int
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _lock_for(project_id: str) -> asyncio.Lock:
    return _project_locks[project_id]


# Concurrency cap for `backup_all`. Each unit of work is a full
# tar → gpg → split → push pipeline; gpg is CPU-heavy, so we cap
# lower than the list-only path. Tune via this constant if a real
# host shows headroom.
_BULK_CONCURRENCY = 4


# ── Public entry points ──────────────────────────────────────────────────────


async def backup_one(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    note: str | None = None,
) -> BackupResult:
    """Back up a single xo-project. Lazy-creates the per-project repo if missing.

    Holds the per-project lock for the duration so concurrent backups of
    the same project never race on the remote.
    """
    async with _lock_for(project_id):
        return await _backup_one_locked(project_id, cfg=cfg, auth=auth, owner=owner, note=note)


async def backup_all(
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    note: str | None = None,
) -> list[BackupResult]:
    """Back up every local xo-project. One repo per project.

    Runs up to `_BULK_CONCURRENCY` projects in parallel — each project
    targets its own repo and holds its own per-project lock, so there's
    no shared state across the parallel branches. Result order matches
    `list_projects()` order (gather preserves input order).
    On a project-level error, records the error in that project's
    result and continues with the rest — caller can decide how to
    surface partial failures.
    """
    entries = list_projects()
    if not entries:
        return []
    sem = asyncio.Semaphore(_BULK_CONCURRENCY)

    async def _one(pid: str) -> BackupResult:
        async with sem:
            try:
                return await backup_one(pid, cfg=cfg, auth=auth, owner=owner, note=note)
            except Exception as exc:
                return BackupResult(
                    project_id=pid, snapshot_id="", size_bytes=0, sha256="",
                    parts=0, ok=False, error=f"{type(exc).__name__}: {exc}",
                )

    pids = [entry["name"] for entry in entries]
    return await asyncio.gather(*(_one(pid) for pid in pids))


# ── Internal: locked single-project path ─────────────────────────────────────


async def _backup_one_locked(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    note: str | None,
) -> BackupResult:
    src = project_dir(project_id)
    if not src.is_dir():
        raise FileNotFoundError(
            f"Project {project_id!r} not found at {src}. "
            f"Check xo-projects-root or scaffold it first."
        )
    assert cfg.passphrase is not None  # caller validated cfg.configured

    repo_name = repo_name_for(project_id)
    # Lazy repo creation: first backup of a project may need to make the
    # remote. Subsequent backups skip this branch.
    if not await github.repo_exists(owner, repo_name, auth=auth):
        await github.create_repo(owner, repo_name, auth=auth)
    repo_url = f"https://github.com/{owner}/{repo_name}.git"

    snapshot_id = manifest.utc_timestamp_id()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        clone_dir = tmp_root / "clone"
        await github.shallow_clone(repo_url, clone_dir, auth=auth)

        snapshot_dir = clone_dir / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=False)

        # Tar → gpg → split. Keep the plaintext tarball outside the
        # clone tree so it can never accidentally land in a commit.
        plain_tarball = tmp_root / "snapshot.tar.gz"
        await tarball.build_tarball(src, plain_tarball)
        parts = await crypto.encrypt_to_chunks(plain_tarball, snapshot_dir, cfg.passphrase)

        sha = manifest.sha256_files_concat(parts)
        encrypted_size = sum(p.stat().st_size for p in parts)
        mani = manifest.SnapshotManifest(
            project_id=project_id,
            snapshot_id=snapshot_id,
            created_at=manifest.utc_iso_now(),
            size_bytes=encrypted_size,
            sha256=sha,
            parts=[p.name for p in parts],
            gitignore_respected=True,
            note=note or None,
        )
        mani.write(snapshot_dir)

        _prune_old_snapshots(clone_dir)

        pushed = await github.commit_and_push_in(
            clone_dir,
            _commit_message(project_id, snapshot_id, note),
            auth=auth,
        )

    result = BackupResult(
        project_id=project_id,
        snapshot_id=snapshot_id,
        size_bytes=encrypted_size,
        sha256=sha,
        parts=len(parts),
    )
    if not pushed:
        # Shouldn't happen — we just wrote new files — but surface it
        # so silent no-ops don't masquerade as successes.
        result.ok = False
        result.error = "Nothing to commit; clone working tree unchanged after backup write"
    return result


def _commit_message(project_id: str, snapshot_id: str, note: str | None) -> str:
    base = f"Backup: {project_id} @ {snapshot_id}"
    if note:
        # Trim to a sensible single-line commit subject.
        clean = note.strip().replace("\n", " ")[:120]
        if clean:
            return f"{base} — {clean}"
    return base


def _prune_old_snapshots(clone_dir: Path) -> None:
    """Delete oldest timestamped subdirs at the clone root so at most MAX_VERSIONS_PER_PROJECT remain."""
    if not clone_dir.is_dir():
        return
    timestamps = sorted(
        (d for d in clone_dir.iterdir() if d.is_dir() and _looks_like_timestamp(d.name)),
        key=lambda d: d.name,
    )
    excess = len(timestamps) - MAX_VERSIONS_PER_PROJECT
    if excess <= 0:
        return
    for old in timestamps[:excess]:
        shutil.rmtree(old, ignore_errors=True)


def _looks_like_timestamp(name: str) -> bool:
    # YYYYMMDD-HHMMSS — 15 chars total, digit-dash-digit pattern.
    return (
        len(name) == 15
        and name[:8].isdigit()
        and name[8] == "-"
        and name[9:].isdigit()
    )
