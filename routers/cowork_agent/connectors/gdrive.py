"""
REST routes for the Google Drive connector.

Exposes:
  GET  /api/connectors/gdrive/remotes
  POST /api/connectors/gdrive/remotes
  GET  /api/connectors/gdrive/sessions/{session_id}
  DELETE /api/connectors/gdrive/remotes/{name}
  POST /api/connectors/gdrive/sessions/{session_id}/cancel
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from services.cowork_agent.connectors.gdrive import (
    cancel_session,
    create_remote_session,
    delete_remote,
    delete_remote_folder,
    get_session,
    list_drive_remotes,
    list_remote_folders,
    mkdir_remote_path,
    rclone_available,
    upload_file_to_remote,
    validate_remote_name,
)

log = logging.getLogger(__name__)
router = APIRouter()

# v1 cap: large enough for ordinary docs/images, conservative enough that the
# Coder edge proxy and Next.js dev rewrite stay reliable.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB


# ---------------------------------------------------------------------------
# GET /api/connectors/gdrive/remotes
# ---------------------------------------------------------------------------

@router.get("/api/connectors/gdrive/remotes")
async def get_gdrive_remotes() -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(
            503,
            detail="Could not reach rclone daemon. Check that rclone is installed and running.",
        )
    remotes = await list_drive_remotes()
    return JSONResponse({"remotes": remotes})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/remotes
# ---------------------------------------------------------------------------

class CreateRemoteBody(BaseModel):
    name: str
    force: bool = False


@router.post("/api/connectors/gdrive/remotes")
async def create_gdrive_remote(body: CreateRemoteBody) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone daemon.")

    # Validate name
    err = await validate_remote_name(body.name)
    if err:
        raise HTTPException(400, detail=err)

    try:
        session = await create_remote_session(body.name, force=body.force)
    except RuntimeError as exc:
        # Concurrent flow
        raise HTTPException(409, detail=str(exc)) from exc

    return JSONResponse(
        {"session_id": session.session_id, "status": "pending"},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/gdrive/sessions/{session_id}
# ---------------------------------------------------------------------------

@router.get("/api/connectors/gdrive/sessions/{session_id}")
async def poll_gdrive_session(session_id: str) -> JSONResponse:
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found or expired.")

    payload: dict = {"status": session.status}
    if session.status == "awaiting_oauth" and session.auth_url:
        payload["auth_url"] = session.auth_url
        payload["needs_manual_code"] = session.needs_manual_code
    if session.status == "completed":
        payload["remote_name"] = session.remote_name
    if session.status == "failed" and session.error:
        payload["error"] = session.error

    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# DELETE /api/connectors/gdrive/remotes/{name}
# ---------------------------------------------------------------------------

@router.delete("/api/connectors/gdrive/remotes/{name}")
async def remove_gdrive_remote(name: str) -> Response:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone daemon.")
    try:
        await delete_remote(name)
    except Exception as exc:
        raise HTTPException(500, detail=str(exc)) from exc
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/sessions/{session_id}/cancel
# ---------------------------------------------------------------------------

@router.post("/api/connectors/gdrive/sessions/{session_id}/cancel")
async def cancel_gdrive_session(session_id: str) -> JSONResponse:
    await cancel_session(session_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/sessions/{session_id}/submit
# Body: {"code": "<paste from URL bar>"}
# ---------------------------------------------------------------------------

class SubmitCodeBody(BaseModel):
    code: str


@router.post("/api/connectors/gdrive/sessions/{session_id}/submit")
async def submit_gdrive_code(session_id: str, body: SubmitCodeBody) -> JSONResponse:
    """Receive the redirect URL / verification code the user pasted from the browser."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found or expired.")
    if session.status != "awaiting_oauth":
        raise HTTPException(400, detail="Session is not waiting for a verification code.")

    # Accept either the full redirect URL or just the bare code
    code = body.code.strip()
    import re
    m = re.search(r"[?&]code=([^&]+)", code)
    if m:
        code = m.group(1)

    session.verification_input = code
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/remotes/{name}/mkdir
# Body: {"path": "<folder path on remote>"}
# ---------------------------------------------------------------------------

class MkdirBody(BaseModel):
    path: str


@router.post("/api/connectors/gdrive/remotes/{name}/mkdir")
async def mkdir_gdrive_remote(name: str, body: MkdirBody) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone.")

    remotes = await list_drive_remotes()
    if not any(r.get("name") == name for r in remotes):
        raise HTTPException(404, detail=f"Remote '{name}' not found.")

    try:
        await mkdir_remote_path(name, body.path)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    return JSONResponse({"ok": True, "path": body.path})


# ---------------------------------------------------------------------------
# GET /api/connectors/gdrive/remotes/{name}/folders
# Lists top-level folders visible to rclone on the remote.
# ---------------------------------------------------------------------------

@router.get("/api/connectors/gdrive/remotes/{name}/folders")
async def list_gdrive_remote_folders(name: str) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone.")

    remotes = await list_drive_remotes()
    if not any(r.get("name") == name for r in remotes):
        raise HTTPException(404, detail=f"Remote '{name}' not found.")

    try:
        folders = await list_remote_folders(name)
    except RuntimeError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    return JSONResponse({"folders": folders})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/remotes/{name}/rmdir
# Body: {"path": "<folder path>"} — purges folder + rclone-visible contents.
# ---------------------------------------------------------------------------

class RmdirBody(BaseModel):
    path: str


@router.post("/api/connectors/gdrive/remotes/{name}/rmdir")
async def rmdir_gdrive_remote(name: str, body: RmdirBody) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone.")

    remotes = await list_drive_remotes()
    if not any(r.get("name") == name for r in remotes):
        raise HTTPException(404, detail=f"Remote '{name}' not found.")

    try:
        await delete_remote_folder(name, body.path)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    return JSONResponse({"ok": True, "path": body.path})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/remotes/{name}/upload?path=<folder>&filename=<f>
# Body is the raw file bytes (Content-Type: application/octet-stream).
# Streams to `rclone rcat` stdin — no disk spool, no RAM buffer.
# ---------------------------------------------------------------------------

@router.post("/api/connectors/gdrive/remotes/{name}/upload")
async def upload_to_gdrive_remote(
    name: str,
    request: Request,
    path: str = "",
    filename: str = "",
) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone.")

    remotes = await list_drive_remotes()
    if not any(r.get("name") == name for r in remotes):
        raise HTTPException(404, detail=f"Remote '{name}' not found.")

    size_hdr = request.headers.get("content-length")
    size: int | None = None
    if size_hdr and size_hdr.isdigit():
        size = int(size_hdr)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB cap.",
            )

    try:
        final_path = await upload_file_to_remote(
            name, path, filename, size, request.stream(),
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, detail=f"rclone upload failed: {exc}") from exc

    return JSONResponse({"ok": True, "path": final_path, "size": size})
