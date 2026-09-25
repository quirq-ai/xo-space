"""
Workspace / filesystem endpoints under `/api/files/*`.

Covers uploads, directory listing, text & binary content reads, and
directory creation. When `scaffold:true` is passed to mkdir, the project is
built using the canonical xo-projects layout (see
``services.cowork_agent.project_layout``). Every path must resolve inside the
user's home directory (an upload may also target the xo-projects root, which a
Docker install keeps outside home). Containment is decided on resolved paths,
never on string prefixes, and an upload keeps only the file's own name.
"""

import hashlib
import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from services.cowork_agent.project_layout import (
    project_dir,
    scaffold_project,
    xo_projects_root,
)

router = APIRouter()

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _within(base: Path, target: Path) -> bool:
    """``target`` is ``base`` or inside it, once both are resolved (``..``, symlinks).

    A string prefix is not containment: ``/home/user-shared`` starts with
    ``/home/user`` yet is outside it.
    """
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _upload_name(raw: str | None) -> str:
    """Only the file's own name. A client-sent ``../x`` or ``/abs/x`` must not
    choose the folder: ``Path(dir) / "/abs/x"`` discards ``dir`` entirely."""
    name = Path(raw or "").name
    return name if name not in ("", ".", "..") else "upload"


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/api/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    workspace: str = Form(""),
):
    """Save an uploaded file into the workspace (or ~/uploads fallback)."""
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 100 MB limit")
    content_hash = hashlib.sha256(content).hexdigest()

    home = Path.home()
    dest_dir = Path(workspace).resolve() if workspace else (home / "uploads").resolve()
    if not (_within(home, dest_dir) or _within(xo_projects_root(), dest_dir)):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = _upload_name(file.filename)
    dest = dest_dir / filename
    # A symlink already sitting at that name must not lead the read or write outside.
    if not _within(dest_dir, dest):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    # Avoid overwriting — append hash suffix if name collides with different content
    if dest.exists():
        existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing_hash != content_hash:
            stem = dest.stem
            suffix = dest.suffix
            dest = dest_dir / f"{stem}_{content_hash[:8]}{suffix}"
            if not _within(dest_dir, dest):
                return JSONResponse(status_code=403, content={"detail": "Access denied"})

    dest.write_bytes(content)

    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return {
        "file_id": content_hash[:16],
        "name": dest.name,
        "path": str(dest),
        "size": len(content),
        "mime_type": mime,
        "source": "uploaded",
        "content_hash": content_hash,
    }


@router.post("/api/files/list-directory")
async def list_directory(request: Request):
    """List files and directories at a given path."""
    body = await request.json()
    raw_path = body.get("path")
    base = Path.home()

    if raw_path:
        target = Path(raw_path).resolve()
        # Prevent traversal outside home
        if not _within(base, target):
            return JSONResponse(status_code=403, content={"detail": "Access denied"})
    else:
        target = base

    if not target.is_dir():
        return JSONResponse(status_code=404, content={"detail": "Not a directory"})

    dirs = []
    files = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            else:
                files.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass

    parent = str(target.parent) if target != base else None

    return {
        "path": str(target),
        "parent": parent,
        "dirs": dirs,
        "files": files,
    }


@router.post("/api/files/content")
async def file_content(request: Request):
    """Read text content of a file."""
    body = await request.json()
    raw_path = body.get("path")
    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not _within(base, target):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "File not found"})

    try:
        content = target.read_text(errors="replace")
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"content": content, "path": str(target)}


@router.post("/api/files/content-binary")
async def file_content_binary(request: Request):
    """Read binary file and return as a downloadable response."""
    body = await request.json()
    raw_path = body.get("path")
    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not _within(base, target):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "File not found"})

    return FileResponse(str(target), filename=target.name)


@router.post("/api/files/save")
async def file_save(request: Request):
    """Write text content to a file under the user's home directory.

    Body fields:
    - ``path`` (str, required): absolute path of the file to write.
    - ``content`` (str, required): UTF-8 text content.

    Creates parent directories if missing. Intended for known workspace
    files (e.g. `IDENTITY.md`, `SOUL.md`, etc.) — for generic uploads,
    use `/api/files/upload` instead.
    """
    body = await request.json()
    raw_path = body.get("path")
    content = body.get("content")

    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})
    if content is None:
        return JSONResponse(status_code=400, content={"detail": "Missing content"})
    if not isinstance(content, str):
        return JSONResponse(status_code=400, content={"detail": "Content must be a string"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not _within(base, target):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"path": str(target), "bytes": len(data)}


@router.post("/api/files/mkdir")
async def make_directory(request: Request):
    """Create a new directory under the user's home directory.

    Body fields:
    - ``path`` (str, required): absolute path of the directory to create.
    - ``scaffold`` (bool, optional): when true, treats the target as a new
      project under ``xo-projects/`` and builds the canonical layout (see
      ``services.cowork_agent.project_layout``). The target's parent must
      resolve to ``xo_projects_root()``; otherwise the call is rejected.
    - ``display_name`` (str, optional): when scaffolding, sets the
      project's display name in ``.xo/project.json``. Falls back to the
      folder name.
    - ``description`` (str, optional): when scaffolding, sets the
      project's description in ``.xo/project.json``.
    - ``files`` (list[str], optional): additional local file paths to copy
      into the new directory. Each entry must be an absolute path that
      already exists under the user's home directory. Existing files with
      the same name are not overwritten.
    """
    body = await request.json()
    raw_path = body.get("path")
    scaffold = bool(body.get("scaffold", False))
    display_name = body.get("display_name")
    description = body.get("description")
    extra_files: list[str] = body.get("files") or []

    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not _within(base, target):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if target.exists():
        return JSONResponse(status_code=409, content={"detail": "Already exists"})

    # Scaffold paths must land directly under xo-projects root.
    if scaffold:
        projects_root = xo_projects_root()
        if target.parent.resolve() != projects_root:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"scaffold:true requires path to be a direct child of "
                        f"{projects_root}; got {target}"
                    )
                },
            )

    # Validate extra file paths before creating anything
    resolved_extras: list[Path] = []
    for raw_file in extra_files:
        fp = Path(raw_file).resolve()
        if not _within(base, fp):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Access denied: {raw_file}"},
            )
        if not fp.is_file():
            return JSONResponse(
                status_code=404,
                content={"detail": f"File not found: {raw_file}"},
            )
        resolved_extras.append(fp)

    try:
        if scaffold:
            scaffold_project(
                target.name,
                display_name=(display_name.strip() if isinstance(display_name, str) else None),
                description=(description.strip() if isinstance(description, str) else None),
            )
            target = project_dir(target.name)
        else:
            target.mkdir(parents=True, exist_ok=False)

        copied = []
        for src in resolved_extras:
            dest = target / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
            copied.append(src.name)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"path": str(target), "name": target.name, "copied": copied}
