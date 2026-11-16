"""GET /api/coworker/xo-projects/{project_id}/version: one project file's
bytes at a commit (``services/coworker/versions``).

The Changes view shows a changed image before (``commit`` = /changes'
``base_commit``) and after (the core's /image, the working copy), and
downloads a binary as a commit had it. An image (by its name's suffix) is
answered inline as that image type; anything else as an
``application/octet-stream`` attachment. The same caps, headers, Host
check and hidden-file rule as the core's /image and /download.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from routers.cowork_agent.bff.errors import http_error
from routers.cowork_agent.bff.read_host import require_read_host
from routers.cowork_agent.bff.xo_projects import (
    DOWNLOAD_MAX_BYTES,
    IMAGE_MAX_BYTES,
    IMAGE_TYPES,
    is_hidden_path,
)
from services.coworker.versions import bytes_at_commit
from services.cowork_agent.project_layout import project_dir_exists, relative_path_suffix
from services.errors import ServiceError
from services.project_files.address import FileRefused, clean_relative
from services.project_files.history import safe_repo_path

router = APIRouter(dependencies=[Depends(require_read_host)])

_IMAGE_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
_FILE_CSP = "sandbox; default-src 'none'"


@router.get("/api/coworker/xo-projects/{project_id}/version")
def file_version(project_id: str, relative_path: str = Query(min_length=1, max_length=4096),
                 commit: str = Query(min_length=1, max_length=64),
                 commit_path: Optional[str] = Query(default=None, max_length=4096),
                 hidden: bool = False) -> Response:
    """The file at ``relative_path`` (project-relative, as /file and /image
    take it; a thread's file is ``.xo/worktrees/<name>/…``) as ``commit``
    (a hash, or ``HEAD``) has it; ``commit_path`` is its name at that commit
    when it was renamed since (/changes' ``old_path``, relative to the
    worktree). ``hidden=true`` serves hidden files, as on /download.

    200 with the bytes; 400 ``invalid_version_request`` /
    ``invalid_relative_path``; 403 ``outside_project`` / ``host_not_allowed``;
    404 ``project_not_found`` / ``file_not_found`` (hidden) /
    ``version_not_found``; 413 ``image_too_large`` (25 MB) /
    ``download_too_large`` (100 MB)."""
    if not project_dir_exists(project_id):
        raise HTTPException(status_code=404, detail={"code": "project_not_found", "message": "Project not found."})
    try:
        names = [clean_relative(relative_path)] + ([safe_repo_path(commit_path)] if commit_path else [])
    except ServiceError as exc:
        raise http_error(exc) from exc
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_version_request",
                                                     "message": "commit_path is malformed."}) from None
    if not hidden and any(is_hidden_path(n) for n in names):
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "File not found in project."})
    media_type = IMAGE_TYPES.get(relative_path_suffix(commit_path or relative_path).lower())
    if media_type is not None:
        cap, too_large = IMAGE_MAX_BYTES, FileRefused(
            "image_too_large", f"Images over {IMAGE_MAX_BYTES // (1024 * 1024)} MB aren't shown.", status=413)
    else:
        cap, too_large = DOWNLOAD_MAX_BYTES, FileRefused(
            "download_too_large", f"Files over {DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB can't be downloaded.",
            status=413)
    try:
        found = bytes_at_commit(project_id, relative_path, commit, commit_path=commit_path, max_bytes=cap,
                                too_large=too_large)
    except ServiceError as exc:
        raise http_error(exc) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"code": "scope_unavailable",
                                                     "message": "File is not readable."}) from exc
    inline = media_type is not None
    return Response(
        content=found.data,
        media_type=media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; "
                                   f"filename*=UTF-8''{quote(found.name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _IMAGE_CSP if inline else _FILE_CSP,
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "private, max-age=31536000, immutable" if found.immutable else "private, no-cache",
            "X-XO-Commit": found.commit,
        },
    )
