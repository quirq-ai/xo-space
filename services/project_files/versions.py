"""A file's versions, as the Files view steps through them (contract §4.1,
§4.2): ``GET /versions`` and ``GET /file?version=``.

The records are the version store's (``services.project_files.edits``).
``head`` is what git has at ``HEAD`` for the path, so a tracked file always
has a first version even before anything was recorded; it is read with
XO's hardened git (``services.git_safe``: no hooks, no fsmonitor, no
transport, no conversion: ``cat-file blob`` gives the exact bytes).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from services.git_safe import git_bytes, git_streaming
from services.project_files import edits
from services.project_files.address import (
    EDIT_MAX_BYTES,
    VERSION_PREFIX,
    FileRefused,
    Target,
    invalid_path_code,
    locate,
    project_root,
)
from services.project_files.read import (
    BOM,
    PREVIEW_MAX_BYTES,
    SNIFF_BYTES,
    FileText,
    kind_of,
    line_endings,
    previewable,
    unsupported,
)

LIMIT_MAX = 200
#: A version token is ``sha256:`` and 64 hex digits.
_VERSION_LEN = len(VERSION_PREFIX) + 64


def version_not_found() -> FileRefused:
    return FileRefused("version_not_found", "This version isn't kept any more (it was pruned, or it was a "
                                            "credential file's, which Space never keeps for long).", status=404)


def _target(project_id: str, relative_path: str) -> Target:
    try:
        return locate(project_id, relative_path)
    except FileRefused as exc:
        raise invalid_path_code(exc) from None


# ── git's HEAD ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Head:
    commit: str
    version: str
    folder: Path
    spec: str
    ceiling: str


async def head_of(root: Path, real_rel: str) -> Head | None:
    """The version git has at ``HEAD`` for a project file (in the
    repository that owns its folder: the project's, a worktree's or a
    nested one, never one above the project); None outside git, or for a
    path ``HEAD`` doesn't have."""
    folder = (root / real_rel).parent
    while folder != root and not folder.is_dir():
        folder = folder.parent
    if not folder.is_dir():
        return None
    env = {"GIT_CEILING_DIRECTORIES": str(root.parent)}
    spec = "HEAD:./" + (root / real_rel).relative_to(folder).as_posix()
    code, out = await git_bytes(folder, "rev-parse", "HEAD", spec, env=env)
    lines = out.decode("ascii", errors="replace").split()
    if code != 0 or len(lines) != 2:
        return None
    code, top = await git_bytes(folder, "rev-parse", "--show-toplevel", env=env)
    if code != 0 or not Path(os.fsdecode(top.strip())).resolve().is_relative_to(root):
        return None
    digest = hashlib.sha256()
    try:
        async with git_streaming(folder, "cat-file", "blob", lines[1], env=env) as proc:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(1 << 20):
                digest.update(chunk)
            if await proc.wait() != 0:
                return None
    except OSError:
        return None
    return Head(lines[0], VERSION_PREFIX + digest.hexdigest(), folder, lines[1], env["GIT_CEILING_DIRECTORIES"])


async def _head_bytes(head: Head, limit: int) -> bytes | None:
    """The blob's bytes (``spec`` is its object id, so they are the ones
    hashed), cut after ``limit + 1``."""
    code, out = await git_bytes(head.folder, "cat-file", "blob", head.spec,
                                env={"GIT_CEILING_DIRECTORIES": head.ceiling})
    return out[: limit + 1] if code == 0 else None


# ── GET /versions ─────────────────────────────────────────────────────────────


def _item(project_id: str, edit: edits.Edit, n: int | None, now: dict[str, str | None], root: Path
          ) -> dict[str, Any]:
    if edit.real not in now:
        found = edits.content_at(root / edit.real)
        now[edit.real] = found.version if found else None
    return {"id": edit.id, "n": n, "path": edit.path, "worktree": edit.worktree, "kind": edit.kind,
            "version": edit.to_version, "prev_version": edit.from_version, "at": edit.at, "by": dict(edit.by),
            "size_bytes": edit.size_bytes, "content_kept": edits.is_kept(project_id, edit),
            "old_path": edit.old_path, "current": now[edit.real] == edit.to_version, "restores": edit.restores}


def _page(items: list[edits.Edit], before: str | None, limit: int) -> tuple[int, list[edits.Edit], bool]:
    start = 0
    if before:
        index = next((i for i, e in enumerate(items) if e.id == before), None)
        if index is None:
            raise FileRefused("invalid_before", "before must be the id of a version in this list.", status=400)
        start = index + 1
    page = items[start:start + limit]
    return start, page, start + limit < len(items)


async def listing(project_id: str, relative_path: str | None, *, limit: int = 50, before: str | None = None,
                  since: datetime | None = None) -> dict[str, Any]:
    """``GET /versions``: one file's versions newest first (or, with
    ``since`` and no path, every record in the project since then)."""
    limit = max(1, min(limit, LIMIT_MAX))
    if relative_path is None:
        if since is None:
            raise FileRefused("invalid_path", "relative_path is required unless since is given.", status=400)
        pid, root = project_root(project_id)
        found = await asyncio.to_thread(edits.history, pid, since=since)
        _start, page, more = _page(found, before, limit)
        now: dict[str, str | None] = {}
        items = await asyncio.to_thread(lambda: [_item(pid, e, None, now, root) for e in page])
        return {"relative_path": None, "versions": items, "head": None, "more": more}
    target = _target(project_id, relative_path)
    every = await asyncio.to_thread(edits.history, target.project_id, target.real_rel)
    total = len(every)                                   # n counts from the oldest record kept
    numbers = {e.id: total - i for i, e in enumerate(every)}
    found = [e for e in every if since is None or e.when >= since]
    start, page, more = _page(found, before, limit)
    now = {}
    items = await asyncio.to_thread(
        lambda: [_item(target.project_id, e, numbers[e.id], now, target.root) for e in page])
    head = await head_of(target.root, target.real_rel)
    return {"relative_path": target.rel, "versions": items,
            "head": {"commit": head.commit, "version": head.version} if head else None, "more": more}


# ── GET /file?version= ────────────────────────────────────────────────────────


async def read_version(project_id: str, relative_path: str, version: str) -> FileText:
    """One past version of a file, read only (``not_editable_reason:
    "historical"``): from the store when one of the path's records names
    it, else from git when it is ``HEAD``'s. 404 ``version_not_found``."""
    target = locate(project_id, relative_path)
    if not version.startswith(VERSION_PREFIX) or len(version) != _VERSION_LEN:
        raise version_not_found()
    named = await asyncio.to_thread(lambda: any(
        version in (e.to_version, e.from_version) for e in edits.history(target.project_id, target.real_rel)))
    data = await asyncio.to_thread(edits.kept_version, target.project_id, version) if named else None
    if data is None:
        head = await head_of(target.root, target.real_rel)
        if head is not None and head.version == version:
            data = await _head_bytes(head, EDIT_MAX_BYTES)
    if data is None:
        raise version_not_found()
    if not previewable(target.rel, data[:SNIFF_BYTES]):
        raise unsupported(target.rel)
    truncated = len(data) > EDIT_MAX_BYTES
    shown = data[:PREVIEW_MAX_BYTES] if truncated else data
    bom = shown.startswith(BOM)
    body = shown[len(BOM):] if bom else shown
    return FileText(
        project_id=target.project_id,
        relative_path=target.rel,
        name=target.name,
        kind=kind_of(target.name),
        size_bytes=len(data),
        modified_at=None,
        truncated=truncated,
        content=body.decode("utf-8", errors="replace"),
        version=version,
        editable=False,
        not_editable_reason="historical",
        place=target.place.label,
        bom=bom,
        line_endings=line_endings(shown),
    )
