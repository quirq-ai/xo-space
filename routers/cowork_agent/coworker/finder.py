"""Coworker-only routes listing every path of a root (/paths) and searching its lines (/search)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from routers.cowork_agent.bff.errors import refused_response
from routers.cowork_agent.bff.read_host import require_read_host
from services.coworker import finder
from services.errors import ServiceError
from services.project_files.address import FileRefused, invalid_path_code
from services.project_files.watch import touch

router = APIRouter(dependencies=[Depends(require_read_host)])


@router.get("/api/coworker/xo-projects/{project_id}/paths")
async def project_paths(project_id: str, worktree: Optional[str] = Query(default=None, max_length=255),
                        stamp: Optional[str] = Query(default=None, max_length=80)) -> Any:
    """Every non-ignored file of the checkout or ``worktree``; ``{unchanged: true, stamp}`` if ``stamp`` holds."""
    touch(project_id)
    try:
        return await finder.paths(project_id, worktree, stamp)
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)


@router.get("/api/coworker/xo-projects/{project_id}/search")
async def project_search(project_id: str, q: Optional[str] = Query(default=None, max_length=4096),
                         worktree: Optional[str] = Query(default=None, max_length=255), regex: bool = False,
                         case: bool = False, word: bool = False,
                         glob: Optional[str] = Query(default=None, max_length=4096),
                         limit: int = Query(default=finder.LIMIT_DEFAULT, ge=1, le=finder.LIMIT_MAX)) -> Any:
    """Every matching line of a root's text files, within a time budget; 400 for an empty or bad query."""
    touch(project_id)
    try:
        query = finder.parse_query(q, regex=regex, case=case, word=word, glob=glob, limit=limit)
        return await finder.search(project_id, query, worktree)
    except ServiceError as exc:
        return refused_response(invalid_path_code(exc) if isinstance(exc, FileRefused) else exc)
