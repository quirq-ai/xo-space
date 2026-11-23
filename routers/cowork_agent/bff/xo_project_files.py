"""Project file routes (save, changes, edits, undo, versions, file ops); every write needs the UI writer key."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra, refused_response
from routers.cowork_agent.bff.read_host import require_read_host
from routers.cowork_agent.bff.ui_writer import check_ui_writer
from services.errors import ServiceError
from services.project_files import changes as project_changes
from services.project_files import edits as edit_log
from services.project_files import ops, versions
from services.project_files.address import FileRefused, invalid_path_code, project_root
from services.project_files.save import Saved, save, undo
from services.project_files.watch import touch
from services.timestamps import parse_ts

router = APIRouter(dependencies=[Depends(require_read_host)])


_SAVING_IS_THE_UI_S = ("Saving project files is only for the XO UI (its proxy adds X-XO-UI-Writer). Agents edit "
                       "files with their own tools.")


def _require_writer(presented: Optional[str]) -> None:
    check_ui_writer(presented, _SAVING_IS_THE_UI_S)


def _saved(result: Saved) -> dict[str, Any]:
    return {
        "relative_path": result.relative_path,
        "version": result.version,
        "size_bytes": result.size_bytes,
        "saved_at": result.saved_at,
        "edit_id": result.edit_id,
        "unchanged": result.unchanged,
    }


# ── save ──────────────────────────────────────────────────────────────────────


class SaveRequest(ForbidExtra):
    relative_path: str = Field(min_length=1, max_length=4096)
    content: str
    #: The version the edit started from (``/file``'s ``version``); null to
    #: create a file that must not exist yet.
    base_version: Optional[str] = Field(default=..., max_length=80)
    confirm: Optional[Literal["runs_code"]] = None


@router.put("/api/xo-projects/{project_id}/file")
def save_project_file(project_id: str, body: SaveRequest,
                      x_xo_ui_writer: Optional[str] = Header(default=None),
                      x_xo_restore_of: Optional[str] = Header(default=None, max_length=80)) -> Any:
    """Save one file if it is still at ``base_version``; ``X-XO-Restore-Of`` records it as a restore of that version."""
    _require_writer(x_xo_ui_writer)
    touch(project_id)
    try:
        return _saved(save(project_id, body.relative_path, body.content, body.base_version,
                           confirm=body.confirm, restores=x_xo_restore_of))
    except ServiceError as exc:
        return refused_response(exc)


# ── changes ───────────────────────────────────────────────────────────────────


@router.get("/api/xo-projects/{project_id}/changes")
async def project_changes_route(project_id: str, worktree: Optional[str] = None,
                                path: Optional[str] = Query(default=None, max_length=4096)) -> Any:
    """What a thread's worktree changed against its merge base with the
    default branch (or, without ``worktree``, what is uncommitted in the
    project's checkout). ``root`` + each file's ``path`` is the
    project-relative path ``/file`` takes. ``path`` limits the answer to
    one file, with a larger patch cap."""
    touch(project_id)
    try:
        return await project_changes.changes(project_id, worktree, path)
    except ServiceError as exc:
        return refused_response(exc)


# ── edits ─────────────────────────────────────────────────────────────────────


@router.get("/api/xo-projects/{project_id}/edits")
def project_edits(project_id: str, relative_path: Optional[str] = None, worktree: Optional[str] = None,
                  since: Optional[str] = None, limit: int = Query(default=50, ge=1, le=edit_log.LIST_MAX)) -> Any:
    """The person's edits, newest first: each with ``still_current`` (the
    file is still as it left it) and ``undoable``."""
    try:
        pid, root = project_root(project_id)
    except ServiceError as exc:
        return refused_response(exc)
    after = parse_ts(since) if since else None
    if since and after is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_since",
                                                     "message": "since must be an ISO-8601 time."})
    items = edit_log.listing(pid, root, path=relative_path, worktree=worktree, since=after, limit=limit)
    return {"project_id": pid, "items": items, "total": len(items)}


@router.post("/api/xo-projects/{project_id}/edits/{edit_id}/undo")
def undo_project_edit(project_id: str, edit_id: str,
                      x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Undo one edit (put back its version, move a rename back, or restore a delete from the trash)."""
    _require_writer(x_xo_ui_writer)
    touch(project_id)
    try:
        return _saved(undo(project_id, edit_id))
    except ServiceError as exc:
        return refused_response(exc)


# ── versions ──────────────────────────────────────────────────────────────────


@router.get("/api/xo-projects/{project_id}/versions")
async def project_versions(project_id: str, relative_path: Optional[str] = Query(default=None, max_length=4096),
                           limit: int = Query(default=50, ge=1, le=versions.LIMIT_MAX),
                           before: Optional[str] = Query(default=None, max_length=80),
                           since: Optional[str] = None) -> Any:
    """A file's versions newest first; ``before`` pages back, ``since`` without a path lists the whole project."""
    after = parse_ts(since) if since else None
    if since and after is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_since",
                                                     "message": "since must be an ISO-8601 time."})
    touch(project_id)
    try:
        return await versions.listing(project_id, relative_path, limit=limit, before=before, since=after)
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)


# ── file operations ───────────────────────────────────────────────────────────


class RenameRequest(ForbidExtra):
    relative_path: str = Field(min_length=1, max_length=4096)
    new_relative_path: str = Field(min_length=1, max_length=4096)
    confirm: Optional[Literal["runs_code"]] = None


class DeleteRequest(ForbidExtra):
    relative_path: str = Field(min_length=1, max_length=4096)
    base_version: Optional[str] = Field(default=None, max_length=80)
    confirm: Optional[Literal["runs_code"]] = None


class MkdirRequest(ForbidExtra):
    relative_path: str = Field(min_length=1, max_length=4096)
    confirm: Optional[Literal["runs_code"]] = None


@router.post("/api/xo-projects/{project_id}/files/rename")
def rename_project_file(project_id: str, body: RenameRequest,
                        x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Rename or move a file or folder, making missing parent folders."""
    _require_writer(x_xo_ui_writer)
    touch(project_id)
    try:
        moved = ops.rename(project_id, body.relative_path, body.new_relative_path, confirm=body.confirm)
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)
    return {"relative_path": moved.relative_path, "old_relative_path": moved.old_relative_path,
            "kind": moved.kind, "edit_id": moved.edit_id, "version": moved.version}


@router.post("/api/xo-projects/{project_id}/files/delete")
def delete_project_file(project_id: str, body: DeleteRequest,
                        x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Move a file or folder into Space's trash (never into the project); undoable via ``/edits/{id}/undo``."""
    _require_writer(x_xo_ui_writer)
    touch(project_id)
    try:
        done = ops.delete(project_id, body.relative_path, base_version=body.base_version, confirm=body.confirm)
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)
    return {"edit_id": done.edit_id, "kind": done.kind, "count": done.count}


@router.post("/api/xo-projects/{project_id}/files/mkdir")
def make_project_folder(project_id: str, body: MkdirRequest,
                        x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Make a folder: 200 ``{relative_path}``."""
    _require_writer(x_xo_ui_writer)
    touch(project_id)
    try:
        return {"relative_path": ops.mkdir(project_id, body.relative_path, confirm=body.confirm)}
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)
