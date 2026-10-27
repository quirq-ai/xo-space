"""GET /api/xo-projects — BFF list of user projects.

Sits on top of services/cowork_agent/project_layout helpers. Strips the
``path`` field so the frontend never sees absolute filesystem
locations; merges scaffolded and unscaffolded directories into one
sorted list (newest first) and marks each entry with ``unscaffolded``
so the UI can prompt to complete setup.

See docs/bff-endpoints-design.md §9.1 for the full contract.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routers.cowork_agent.bff.filters import (
    PROJECT_SYSTEM_LEAVES,
    is_hidden_name,
    is_root_only_hidden,
)
from services.cowork_agent import scopes
from services.cowork_agent.file_history import file_git_history, read_file_at_commit
from services.cowork_agent.project_layout import (
    list_project_tree,
    list_projects,
    list_unscaffolded_dirs,
    project_dir,
    project_dir_exists,
    project_file_path,
    relative_path_suffix,
    read_project_file,
)

router = APIRouter()


class Project(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    unscaffolded: bool


class ListProjectsResponse(BaseModel):
    items: list[Project]
    total: int


def _to_iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# A project's own docs are the only place a human sentence about it exists —
# .xo/project.json's description is empty for every project the API did not
# create. Read the first real line of the usual suspects so the list can say
# what a project IS, not just when it was created. Bounded: 3 files, 2 KB
# each, first 40 lines.
#
# AGENTS.md is deliberately NOT in this list: it is the scaffold's operating
# contract, so its opening line is identical in every project and would put
# the same sentence on every row — the exact failure this replaces.
_DESC_FILES = ("README.md", "PROJECT.md", "OBJECTIVES.md")
_DESC_MAX = 160


def _described(project_id: str) -> Optional[str]:
    from services.cowork_agent.project_layout import project_dir

    root = project_dir(project_id)
    for candidate in _DESC_FILES:
        path = root / candidate
        try:
            if not path.is_file():
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)
        except OSError:
            continue
        # Take the whole first paragraph, not the first line: markdown wraps
        # at ~80 columns, so one line ends mid-sentence ("…in the tradition
        # of") and reads like a truncation bug.
        para: list[str] = []
        for line in head.splitlines()[:40]:
            text = line.strip()
            if not text:
                if para:
                    break
                continue
            # skip headings, badges, html, front matter, tables, quotes, code
            if text.startswith(("#", "!", "<", "---", "|", ">", "```")):
                if para:
                    break
                continue
            if text.startswith(("- ", "* ", "+ ")):
                if para:
                    break
                continue
            para.append(text)
        if not para:
            continue
        text = " ".join(" ".join(para).split())
        # The row shows plain text, so strip the markdown that survives a
        # raw paragraph grab: links, emphasis, code ticks.
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = text.replace("**", "").replace("`", "").strip("*_ ")
        if len(text) < 12:
            continue
        if len(text) > _DESC_MAX:
            cut = text[:_DESC_MAX].rsplit(" ", 1)[0].rstrip(" ,;:.")
            text = cut + "…"
        return text
    return None


def _shape_scaffolded(entry: dict) -> Project:
    name = str(entry.get("name") or "")
    display = entry.get("display_name") or name
    return Project(
        id=name,
        display_name=str(display),
        description=(entry.get("description") or _described(name) or None),
        created_at=(entry.get("created_at") or None),
        unscaffolded=False,
    )


def _shape_unscaffolded(entry: dict) -> Project:
    name = str(entry.get("name") or "")
    return Project(
        id=name,
        display_name=name,
        description=None,
        created_at=_to_iso_utc(entry.get("mtime")),
        unscaffolded=True,
    )


def _sort_newest_first(items: list[Project]) -> list[Project]:
    """Newest first by created_at; nulls last; alphabetical tiebreak."""
    with_ts = sorted(
        [p for p in items if p.created_at],
        key=lambda p: p.id,
    )
    with_ts.sort(key=lambda p: p.created_at or "", reverse=True)
    without_ts = sorted(
        [p for p in items if not p.created_at],
        key=lambda p: p.id,
    )
    return with_ts + without_ts


@router.get("/api/xo-projects", response_model=ListProjectsResponse)
def list_xo_projects() -> ListProjectsResponse:
    try:
        scopes.resolve_scope("xo-projects")  # validates scope exists
        scaffolded = list_projects()
        unscaffolded = list_unscaffolded_dirs()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scope_unavailable",
                "message": "Project directory is not readable.",
            },
        ) from exc

    items: list[Project] = []
    for entry in scaffolded:
        if (entry.get("name") or "") in PROJECT_SYSTEM_LEAVES:
            continue
        items.append(_shape_scaffolded(entry))
    for entry in unscaffolded:
        if (entry.get("name") or "") in PROJECT_SYSTEM_LEAVES:
            continue
        items.append(_shape_unscaffolded(entry))

    items = _sort_newest_first(items)
    return ListProjectsResponse(items=items, total=len(items))


# ── /api/xo-projects/{id}/tree ────────────────────────────────────────────────


class TreeEntry(BaseModel):
    """One row of a project's file explorer.

    ``size_bytes`` is set for files, ``entries`` for directories, and both are
    optional: a broken symlink or a file deleted mid-listing still gets a row
    with a name, just without detail.
    """

    name: str
    relative_path: str
    is_dir: bool = False
    size_bytes: Optional[int] = None
    modified_at: Optional[str] = None
    entries: Optional[int] = None


class ProjectTreeResponse(BaseModel):
    project_id: str
    relative_path: str
    parent_relative_path: Optional[str] = None
    dirs: list[TreeEntry]
    files: list[TreeEntry]


def _filter_tree_entries(
    entries: list[dict],
    at_root: bool,
    *,
    is_dir: bool,
) -> list[TreeEntry]:
    out: list[TreeEntry] = []
    for e in entries:
        name = e.get("name") or ""
        if is_hidden_name(name):
            continue
        if at_root and is_root_only_hidden(name):
            continue
        out.append(
            TreeEntry(
                name=name,
                relative_path=e.get("relative_path") or "",
                is_dir=is_dir,
                size_bytes=e.get("size_bytes"),
                modified_at=_to_iso_utc(e.get("modified_at")),
                entries=e.get("entries"),
            )
        )
    return out


@router.get("/api/xo-projects/{project_id}/tree", response_model=ProjectTreeResponse)
def project_tree(project_id: str, relative_path: str = "") -> ProjectTreeResponse:
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )

    try:
        raw = list_project_tree(project_id, relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_relative_path",
                "message": "relative_path is malformed or escapes the project root.",
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scope_unavailable",
                "message": "Project directory is not readable.",
            },
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "directory_not_found", "message": "Directory not found in project."},
        )

    at_root = (raw["relative_path"] or "") == ""
    return ProjectTreeResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        parent_relative_path=raw["parent_relative_path"],
        dirs=_filter_tree_entries(raw["dirs"], at_root, is_dir=True),
        files=_filter_tree_entries(raw["files"], at_root, is_dir=False),
    )


# ── /api/xo-projects/{id}/file ────────────────────────────────────────────────
#
# Preview one text file from inside a project. Deliberately NOT the older
# POST /api/files/content: that takes an absolute host path, and the Space UI
# never sees absolute paths (see the module docstring). Here the project id
# plus a project-relative path is the whole address space, validated by the
# same helper the tree listing uses.

PREVIEW_MAX_BYTES = 256 * 1024
# Text this UI can present honestly: rendered markdown, sandboxed HTML, and
# plain text. Anything else (an image, a 40 MB parquet) gets a "no preview"
# state from its metadata rather than a wall of replacement characters.
PREVIEW_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".mdx",
        ".html", ".htm",
        ".txt", ".text", ".rst", ".log",
        ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example",
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".sh", ".sql",
        ".go", ".rs", ".java", ".c", ".h", ".cpp",
    }
)


def _preview_kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith((".md", ".markdown", ".mdx")):
        return "markdown"
    if lower.endswith((".html", ".htm")):
        return "html"
    return "text"


class FilePreviewResponse(BaseModel):
    project_id: str
    relative_path: str
    name: str
    kind: str
    size_bytes: int
    modified_at: Optional[str] = None
    truncated: bool
    content: str


def _version_response(
    project_id: str,
    relative_path: str,
    commit: str,
    commit_path: Optional[str],
) -> FilePreviewResponse:
    """The /file response for a historical version of the file."""
    try:
        raw = read_file_at_commit(
            project_id, relative_path, commit, commit_path=commit_path
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_version_request",
                "message": "commit or commit_path is malformed.",
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "File is not readable."},
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "File not found in project."},
        )
    if raw["content"] is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "version_not_found",
                "message": (
                    "No repository owns this file."
                    if not raw["is_repo"]
                    else "This commit has no version of the file."
                ),
            },
        )

    return FilePreviewResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        name=raw["name"],
        kind=_preview_kind(raw["name"]),
        size_bytes=len(raw["content"].encode("utf-8")),
        modified_at=None,
        truncated=raw["truncated"],
        content=raw["content"],
    )


@router.get(
    "/api/xo-projects/{project_id}/file",
    response_model=FilePreviewResponse,
)
def project_file(
    project_id: str,
    relative_path: str,
    commit: Optional[str] = None,
    commit_path: Optional[str] = None,
) -> FilePreviewResponse:
    """Return one previewable text file's content.

    With ``commit`` (and, across renames, ``commit_path`` — the ``path``
    field of the matching /file-history item) the content comes from
    that commit instead of the working tree, so the previewer's version
    picker can show the document as it was.
    """
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    # The gate judges the name the content will carry: the historical
    # name when a version is asked for, today's name otherwise.
    gate_name = commit_path if (commit and commit_path) else relative_path
    suffix = relative_path_suffix(gate_name or "").lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "preview_unsupported",
                "message": f"No text preview for {suffix or 'this file type'}.",
            },
        )

    if commit is not None:
        return _version_response(project_id, relative_path, commit, commit_path)

    try:
        raw = read_project_file(
            project_id, relative_path, max_bytes=PREVIEW_MAX_BYTES
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_relative_path",
                "message": "relative_path is malformed or escapes the project root.",
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "File is not readable."},
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "File not found in project."},
        )

    return FilePreviewResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        name=raw["name"],
        kind=_preview_kind(raw["name"]),
        size_bytes=raw["size_bytes"],
        modified_at=_to_iso_utc(raw["modified_at"]),
        truncated=raw["truncated"],
        content=raw["content"],
    )


# ── /api/xo-projects/{id}/image ───────────────────────────────────────────────
#
# One image's bytes, for the image viewer: the same address space as /file
# (project id + project-relative path, the same path helper), for the image
# types a browser shows. The type comes from the suffix, never from sniffing,
# and the headers keep the browser from running anything: nosniff and a
# sandboxing CSP, so an SVG opened on its own runs no script.

IMAGE_MAX_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}
_IMAGE_CHUNK = 64 * 1024


def _image_chunks(fh):
    try:
        while chunk := fh.read(_IMAGE_CHUNK):
            yield chunk
    finally:
        fh.close()


def _project_or_404(project_id: str) -> None:
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )


_FILE_NOT_FOUND = {"code": "file_not_found", "message": "File not found in project."}


def _locate(project_id: str, relative_path: str) -> dict:
    """The project file to serve, or the 400/404 the caller should answer."""
    try:
        found = project_file_path(project_id, relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_relative_path",
                "message": "relative_path is malformed or escapes the project root.",
            },
        ) from exc
    if found is None:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)
    return found


def _serve(
    found: dict, *, max_bytes: int, too_large: dict, media_type: str,
    disposition: str, csp: str,
) -> StreamingResponse:
    """Stream ``found`` as it is, under ``max_bytes``, with headers that keep
    the browser from running it."""
    try:
        fh = open(found["path"], "rb")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "File is not readable."},
        ) from exc
    size = os.fstat(fh.fileno()).st_size
    if size > max_bytes:
        fh.close()
        raise HTTPException(status_code=413, detail=too_large)
    return StreamingResponse(
        _image_chunks(fh),
        media_type=media_type,
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(found['name'], safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": csp,
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "private, no-cache",
        },
    )


@router.get("/api/xo-projects/{project_id}/image")
def project_image(project_id: str, relative_path: str) -> StreamingResponse:
    """Serve one image file from inside a project, as it is."""
    _project_or_404(project_id)
    suffix = relative_path_suffix(relative_path or "").lower()
    media_type = IMAGE_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "image_unsupported",
                "message": f"{suffix or 'This file'} isn't an image the viewer shows.",
            },
        )
    return _serve(
        _locate(project_id, relative_path),
        max_bytes=IMAGE_MAX_BYTES,
        too_large={
            "code": "image_too_large",
            "message": f"Images over {IMAGE_MAX_BYTES // (1024 * 1024)} MB aren't shown.",
        },
        media_type=media_type,
        disposition="inline",
        csp="sandbox; default-src 'none'; style-src 'unsafe-inline'",
    )


# ── /api/xo-projects/{id}/download ────────────────────────────────────────────
#
# One file whole, for the previewer's Download button (/file stops at
# PREVIEW_MAX_BYTES). Any type, always octet-stream as an attachment. What the
# tree hides (.env, .git, .xo, temp files) is refused, also through a link.

DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024


def _hidden(rel: str) -> bool:
    parts = rel.split("/")
    return any(is_hidden_name(p) for p in parts) or (
        len(parts) == 1 and is_root_only_hidden(parts[0])
    )


@router.get("/api/xo-projects/{project_id}/download")
def project_download(project_id: str, relative_path: str) -> StreamingResponse:
    """Serve one file from inside a project as a download, as it is."""
    _project_or_404(project_id)
    found = _locate(project_id, relative_path)
    resolved = found["path"].relative_to(project_dir(found["project_id"]).resolve())
    if _hidden(found["relative_path"]) or _hidden(resolved.as_posix()):
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)
    return _serve(
        found,
        max_bytes=DOWNLOAD_MAX_BYTES,
        too_large={
            "code": "download_too_large",
            "message": f"Files over {DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB can't be downloaded.",
        },
        media_type="application/octet-stream",
        disposition="attachment",
        csp="sandbox; default-src 'none'",
    )


# ── /api/xo-projects/{id}/file-history ────────────────────────────────────────
#
# The previewer's History pane: commits that touched one file, newest first.
# Same address space as /file — project id + project-relative path — with the
# git specifics (repo discovery, --follow, numstat) in
# services/cowork_agent/file_history.

HISTORY_MAX_COMMITS = 200
HISTORY_DEFAULT_COMMITS = 50


class FileHistoryCommit(BaseModel):
    """One commit touching the file.

    ``additions``/``deletions`` are ``None`` when git has no counts to
    give: a binary file, or a commit that only renamed it. ``path`` is
    the file's name at that commit — echo it back as ``commit_path``
    back as ``commit_path`` when asking /file for that version, so
    renamed files resolve under the name the commit knew.
    """

    hash: str
    short_hash: str
    author: str
    date: Optional[str] = None
    subject: str
    additions: Optional[int] = None
    deletions: Optional[int] = None
    path: Optional[str] = None


class FileHistoryResponse(BaseModel):
    project_id: str
    relative_path: str
    is_repo: bool
    items: list[FileHistoryCommit]
    total: int


@router.get(
    "/api/xo-projects/{project_id}/file-history",
    response_model=FileHistoryResponse,
)
def project_file_history(
    project_id: str, relative_path: str, limit: int = HISTORY_DEFAULT_COMMITS
) -> FileHistoryResponse:
    """Return the git log of one project file's edits."""
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )

    try:
        raw = file_git_history(
            project_id, relative_path, limit=max(1, min(limit, HISTORY_MAX_COMMITS))
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_relative_path",
                "message": "relative_path is malformed or escapes the project root.",
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "File is not readable."},
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "File not found in project."},
        )

    return FileHistoryResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        is_repo=raw["is_repo"],
        items=[FileHistoryCommit(**item) for item in raw["items"]],
        total=len(raw["items"]),
    )
