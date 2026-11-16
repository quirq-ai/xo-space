"""Editing a project's files, and what a thread changed (Space's own API).

* ``PUT  /api/xo-projects/{id}/file``: the guarded save;
* ``GET  /api/xo-projects/{id}/changes``: what a worktree (or the project's
  checkout) changed, with git's patch per file;
* ``GET  /api/xo-projects/{id}/edits``: the edit log, newest first;
* ``POST /api/xo-projects/{id}/edits/{edit_id}/undo``: put back what an
  edit replaced.

``GET /file`` (with ``version`` and ``editable``) stays in ``xo_projects``.
The logic is in ``services/project_files``; these handlers only translate.
The two writes need the UI writer key (``X-XO-UI-Writer``,
``services/project_files/writer_key``): agents have their own file tools,
and a save is logged as the person's.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.bff.read_host import require_read_host
from services.errors import ServiceError
from services.project_files import changes as project_changes
from services.project_files import edits as edit_log
from services.project_files import writer_key
from services.project_files.address import FileRefused, project_root
from services.project_files.save import Saved, save, undo
from services.timestamps import parse_ts

router = APIRouter(dependencies=[Depends(require_read_host)])


def _refused(exc: ServiceError) -> JSONResponse:
    """The ``{"detail": {code, message, …}}`` every Space route answers with,
    plus what a refusal carries (a conflict's ``current``)."""
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if isinstance(exc, FileRefused):
        detail.update(exc.extra)
    return JSONResponse(status_code=exc.status, content={"detail": detail})


def _require_writer(presented: Optional[str]) -> None:
    if not writer_key.matches(presented):
        raise HTTPException(status_code=403, detail={
            "code": "ui_only",
            "message": "Saving project files is only for the XO UI (its proxy adds X-XO-UI-Writer). Agents edit "
                       "files with their own tools.",
        })


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
                      x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Save one file if it is still at ``base_version``.

    200 ``{relative_path, version, size_bytes, saved_at, edit_id,
    unchanged}``. 409 ``file_changed`` with ``current`` (``{version, content,
    bom, line_endings}``, or null when the file is gone; ``content`` only for
    text up to 1 MB), 409 ``file_exists``, 409 ``place_changed``, 428
    ``confirm_required``, 403 ``ui_only`` / ``read_only_place`` /
    ``outside_project`` / ``not_writable``, 404, 413 ``too_large``, 415
    ``not_text``."""
    _require_writer(x_xo_ui_writer)
    try:
        return _saved(save(project_id, body.relative_path, body.content, body.base_version,
                           confirm=body.confirm))
    except ServiceError as exc:
        return _refused(exc)


# ── changes ───────────────────────────────────────────────────────────────────


@router.get("/api/xo-projects/{project_id}/changes")
async def project_changes_route(project_id: str, worktree: Optional[str] = None,
                                path: Optional[str] = Query(default=None, max_length=4096)) -> Any:
    """What a thread's worktree changed against its merge base with the
    default branch (or, without ``worktree``, what is uncommitted in the
    project's checkout). ``root`` + each file's ``path`` is the
    project-relative path ``/file`` takes. ``path`` limits the answer to
    one file, with a larger patch cap."""
    try:
        return await project_changes.changes(project_id, worktree, path)
    except ServiceError as exc:
        return _refused(exc)


# ── edits ─────────────────────────────────────────────────────────────────────


@router.get("/api/xo-projects/{project_id}/edits")
def project_edits(project_id: str, relative_path: Optional[str] = None, worktree: Optional[str] = None,
                  since: Optional[str] = None, limit: int = Query(default=50, ge=1, le=edit_log.LIST_MAX)) -> Any:
    """The person's edits, newest first: each with ``still_current`` (the
    file is still as it left it) and ``undoable``."""
    try:
        pid, root = project_root(project_id)
    except ServiceError as exc:
        return _refused(exc)
    after = parse_ts(since) if since else None
    if since and after is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_since",
                                                     "message": "since must be an ISO-8601 time."})
    items = edit_log.listing(pid, root, path=relative_path, worktree=worktree, since=after, limit=limit)
    return {"project_id": pid, "items": items, "total": len(items)}


@router.post("/api/xo-projects/{project_id}/edits/{edit_id}/undo")
def undo_project_edit(project_id: str, edit_id: str,
                      x_xo_ui_writer: Optional[str] = Header(default=None)) -> Any:
    """Put back what one edit replaced, if the file is still as that edit
    left it: the save's answers, plus 404 ``edit_not_found`` and 410
    ``not_undoable`` (the earlier version is no longer kept)."""
    _require_writer(x_xo_ui_writer)
    try:
        return _saved(undo(project_id, edit_id))
    except ServiceError as exc:
        return _refused(exc)
