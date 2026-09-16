"""
Restore orchestration — single-project and all-projects.

Restore semantics:
- Without ``force=True``, refuse to overwrite an existing local project
  folder. The router turns the resulting ``ProjectExistsError`` into a
  409 with a clear suggestion.
- ``force=True`` deletes the existing folder and replaces it with the
  decrypted snapshot wholesale. No automatic pre-restore snapshot —
  the design treats "force" as a deliberate destructive action.

Bulk restore is independent-per-project: a 409 (or any other error) on
one project does NOT abort the rest. The result list carries one entry
per project with ``ok`` and an optional ``error`` field, so the caller
can surface partial failures cleanly.

Also reads the remote-side snapshot index for the `/projects` endpoint.

Staging: every operation creates its own `tempfile.TemporaryDirectory`
and shallow-clones the project's repo into it. No persistent staging.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from services.cowork_agent.project_layout import project_dir
from services.xo_structure import ensure_xo_structure

from . import crypto, github, manifest
from .config import SyncConfig, repo_name_for


@dataclass
class RestoreResult:
    project_id: str
    restored_from: str | None
    target: str | None
    ok: bool = True
    error: str | None = None
    error_code: str | None = None  # "exists" | "not_found" | "verify_failed" | "decrypt_failed" | …

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectExistsError(RuntimeError):
    """Local project folder already exists and ``force`` was not set."""


class SnapshotNotFoundError(RuntimeError):
    """The requested snapshot (or any snapshot for the project) is not on the remote."""


class ChecksumMismatchError(RuntimeError):
    """Concatenated parts' sha256 did not match the manifest. Abort before decrypt."""


_project_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _lock_for(project_id: str) -> asyncio.Lock:
    return _project_locks[project_id]


# Concurrency cap for `list_remote_projects`. Each unit of work is a
# shallow clone + manifest read; bound to keep fd / subprocess use
# reasonable and to stay polite to GitHub.
_LIST_CONCURRENCY = 8

# Concurrency cap for `restore_all`. Each unit of work is a shallow
# clone + integrity check + gpg decrypt + extract + atomic move. gpg
# and the extract step are CPU-heavy, so cap lower than the list path.
_BULK_CONCURRENCY = 4


# ── Public: listing ──────────────────────────────────────────────────────────


@dataclass
class SnapshotSummary:
    id: str
    created_at: str
    size_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProjectSummary:
    project_id: str
    snapshots: list[SnapshotSummary]
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "error": self.error,
        }


async def list_remote_projects(
    *,
    auth: github.GitHubAuth,
    owner: str,
) -> list[ProjectSummary]:
    """Enumerate every `xo-project-*` repo the user owns and list its snapshots.

    For each project: shallow-clone into a tempdir, read snapshot
    manifests, delete the tempdir. Returns projects sorted by id;
    snapshots within each sorted newest-first.
    """
    project_ids = await github.list_xo_project_repos(auth)
    if not project_ids:
        return []

    # Parallelize the per-repo shallow clones with a bounded semaphore.
    # No per-project lock: this is read-only (own tempdir, own clone,
    # discarded on completion). `asyncio.gather` preserves input order,
    # so the result is still sorted by project_id.
    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def _one(pid: str) -> ProjectSummary | None:
        repo_url = f"https://github.com/{owner}/{repo_name_for(pid)}.git"
        async with sem:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    clone_dir = Path(tmp) / "clone"
                    await github.shallow_clone(repo_url, clone_dir, auth=auth)
                    snapshots = _list_project_snapshots(clone_dir)
            except Exception as exc:
                # Surface unreachable / unreadable repos as entries
                # with an error so the caller can show "1 backup is
                # unreachable" instead of silently dropping the project.
                return ProjectSummary(
                    project_id=pid, snapshots=[],
                    error=f"{type(exc).__name__}: {exc}",
                )
        if not snapshots:
            return None
        return ProjectSummary(project_id=pid, snapshots=snapshots)

    results = await asyncio.gather(*(_one(pid) for pid in project_ids))
    return [r for r in results if r is not None]


def _list_project_snapshots(clone_dir: Path) -> list[SnapshotSummary]:
    """Inspect each timestamped subdir at the clone root; return summaries newest-first.

    Folders without a valid manifest are silently skipped — they're
    either in-progress writes or corruption; either way they're not
    safe to advertise as restorable.
    """
    summaries: list[SnapshotSummary] = []
    for snap_dir in sorted(clone_dir.iterdir(), reverse=True):
        if not snap_dir.is_dir() or snap_dir.name.startswith("."):
            continue
        try:
            mani = manifest.SnapshotManifest.read(snap_dir)
        except (FileNotFoundError, ValueError, KeyError):
            continue
        summaries.append(SnapshotSummary(
            id=mani.snapshot_id,
            created_at=mani.created_at,
            size_bytes=mani.size_bytes,
        ))
    return summaries


# ── Public: restore ──────────────────────────────────────────────────────────


async def restore_one(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    snapshot_id: str | None = None,
    force: bool = False,
) -> RestoreResult:
    """Restore a single project from its `xo-project-<id>` repo.

    Acquires the per-project lock for the duration so concurrent
    restores of the same project don't race on the local target.
    """
    async with _lock_for(project_id):
        return await _restore_one_locked(
            project_id, cfg=cfg, auth=auth, owner=owner,
            snapshot_id=snapshot_id, force=force,
        )


async def restore_all(
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    snapshot_id_map: dict[str, str] | None = None,
    force: bool = False,
) -> list[RestoreResult]:
    """Restore every project that has a backup repo on GitHub.

    ``snapshot_id_map`` lets the caller pin specific versions per project
    (e.g., for partial rollback); projects not in the map use the latest
    snapshot. ``force`` applies uniformly to all projects.
    """
    snapshot_id_map = snapshot_id_map or {}
    project_ids = await github.list_xo_project_repos(auth)
    if not project_ids:
        return []

    # Parallelize per-project restores with a bounded semaphore. Each
    # project has its own repo and its own per-project lock, so the
    # branches don't share state; the cap exists to limit concurrent
    # gpg/extract CPU pressure.
    sem = asyncio.Semaphore(_BULK_CONCURRENCY)

    async def _one(pid: str) -> RestoreResult:
        async with sem:
            try:
                return await restore_one(
                    pid, cfg=cfg, auth=auth, owner=owner,
                    snapshot_id=snapshot_id_map.get(pid),
                    force=force,
                )
            except Exception as exc:
                return _error_result(pid, exc)

    return await asyncio.gather(*(_one(pid) for pid in project_ids))


# ── Internal: locked single-project path ─────────────────────────────────────


async def _restore_one_locked(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    owner: str,
    snapshot_id: str | None,
    force: bool,
) -> RestoreResult:
    """All restore logic runs here. Caller is responsible for the per-project lock."""
    assert cfg.passphrase is not None  # caller validated cfg.configured

    repo_name = repo_name_for(project_id)
    if not await github.repo_exists(owner, repo_name, auth=auth):
        raise SnapshotNotFoundError(
            f"No backup repo for project {project_id!r} (expected {owner}/{repo_name})."
        )
    repo_url = f"https://github.com/{owner}/{repo_name}.git"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        clone_dir = tmp_root / "clone"
        await github.shallow_clone(repo_url, clone_dir, auth=auth)

        resolved_snapshot_id = snapshot_id or _newest_snapshot_id(clone_dir)
        if resolved_snapshot_id is None:
            raise SnapshotNotFoundError(
                f"Project {project_id!r} has no valid snapshots in backup repo."
            )

        snap_dir = clone_dir / resolved_snapshot_id
        if not snap_dir.is_dir():
            raise SnapshotNotFoundError(
                f"Snapshot {resolved_snapshot_id!r} not found for project {project_id!r}."
            )

        mani = manifest.SnapshotManifest.read(snap_dir)

        # Verify integrity BEFORE deciding to overwrite local state. A bad
        # snapshot must not block a working local copy.
        parts = [snap_dir / name for name in mani.parts]
        missing = [p for p in parts if not p.is_file()]
        if missing:
            raise SnapshotNotFoundError(
                f"Snapshot {resolved_snapshot_id} is missing parts: {[p.name for p in missing]}"
            )
        actual_sha = manifest.sha256_files_concat(parts)
        if actual_sha != mani.sha256:
            raise ChecksumMismatchError(
                f"Snapshot {resolved_snapshot_id} sha256 mismatch (manifest={mani.sha256[:12]} actual={actual_sha[:12]}). "
                "Possible corruption — refusing to decrypt."
            )

        target = project_dir(project_id)
        if target.exists() and not force:
            raise ProjectExistsError(
                f"Project folder already exists at {target}. "
                "Pass force=true to overwrite; existing local data will be lost."
            )

        # Decrypt + extract into a sibling temp dir first, then atomic
        # move into place. Two reasons:
        # 1. If decrypt fails (wrong passphrase), the local project is untouched.
        # 2. If extract fails halfway, we don't leave a half-written project tree.
        plaintext_tar = tmp_root / "snapshot.tar.gz"
        try:
            await crypto.decrypt_from_chunks(parts, plaintext_tar, cfg.passphrase)
        except crypto.GpgFailedError as exc:
            raise RuntimeError(f"Snapshot decrypt failed (wrong passphrase?): {exc}")

        extracted = tmp_root / "extracted"
        # tarball.extract_tarball is sync; safe inside async because tarfile
        # releases the GIL on its underlying IO.
        from .tarball import extract_tarball  # local import to avoid cycle warnings
        extract_tarball(plaintext_tar, extracted)

        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(target))
        # The same .xo/ as every other project, whatever the snapshot's age.
        # Additive only, and nothing raises: the restore is already in place.
        ensure_xo_structure(project_id)

    return RestoreResult(
        project_id=project_id,
        restored_from=resolved_snapshot_id,
        target=str(target),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _newest_snapshot_id(clone_dir: Path) -> str | None:
    candidates = sorted(
        (d for d in clone_dir.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda d: d.name,
        reverse=True,
    )
    for d in candidates:
        try:
            mani = manifest.SnapshotManifest.read(d)
            return mani.snapshot_id
        except (FileNotFoundError, ValueError, KeyError):
            continue
    return None


def _error_result(project_id: str, exc: BaseException) -> RestoreResult:
    code = {
        ProjectExistsError: "exists",
        SnapshotNotFoundError: "not_found",
        ChecksumMismatchError: "verify_failed",
    }.get(type(exc), "error")
    return RestoreResult(
        project_id=project_id,
        restored_from=None,
        target=None,
        ok=False,
        error=str(exc),
        error_code=code,
    )
