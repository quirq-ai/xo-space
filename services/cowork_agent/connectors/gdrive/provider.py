"""
Google Drive connector via rclone (CLI mode — no daemon, no port).

The generic rclone plumbing and the OAuth-flow engine live in
:mod:`services.cowork_agent.connectors.rclone`. This module supplies only the
Drive-specific descriptor (backend ``drive`` with ``drive.file`` scope, the
Google auth-URL pattern, and the rclone.conf section shape) plus the Drive
file operations (upload / mkdir / list / delete) that its router exposes.
"""

import json
import re
from typing import AsyncIterator, Optional

from ..rclone.connector import (
    RCLONE_CONFIG_PATH,  # noqa: F401  (kept for backward-compat imports)
    RcloneConnector,
    RcloneProvider,
    RcloneSession,  # noqa: F401  (exported for callers that type-hint sessions)
    _rclone_cli,
    _rclone_cli_stdin_stream,
    _rc_post,  # noqa: F401  (re-exported: onedrive.provider + tests import it here historically)
    ensure_rclone_running,  # noqa: F401  (re-exported; server.py imports it from here)
    rclone_available,  # noqa: F401  (re-exported for the gdrive router)
)

# ---------------------------------------------------------------------------
# Drive provider descriptor
# ---------------------------------------------------------------------------

_AUTH_URL_RE = re.compile(r"https?://\S+(?:auth\?state|accounts\.google\.com/o/oauth)\S*")


async def _build_config_section(name: str, token_json: str) -> str:
    """Drive needs only type + scope + token in rclone.conf."""
    return (
        f"\n[{name}]\n"
        f"type = drive\n"
        f"scope = drive.file\n"
        f"token = {token_json}\n"
    )


def _remote_summary(name: str, cfg: dict) -> Optional[dict]:
    if cfg.get("type") != "drive":
        return None
    return {
        "name": name,
        "type": "drive",
        "scope": cfg.get("scope", "drive"),
        "complete": bool(cfg.get("token")),
    }


_PROVIDER = RcloneProvider(
    backend="drive",
    authorize_args=("--drive-scope=drive.file",),
    label="GDrive",
    provider_name="Google",
    auth_url_re=_AUTH_URL_RE,
    build_config_section=_build_config_section,
    remote_summary=_remote_summary,
)

_connector = RcloneConnector(_PROVIDER)

# ---------------------------------------------------------------------------
# Public API — bound to the connector instance (names preserved for the router)
# ---------------------------------------------------------------------------

get_session = _connector.get_session
create_remote_session = _connector.create_remote_session
cancel_session = _connector.cancel_session
delete_remote = _connector.delete_remote
validate_remote_name = _connector.validate_remote_name
list_drive_remotes = _connector.list_remotes


# ---------------------------------------------------------------------------
# Drive file operations (Drive router only) — generic rclone CLI ops on a remote
# ---------------------------------------------------------------------------

async def mkdir_remote_path(name: str, path: str) -> None:
    """Create a folder on a configured remote via `rclone mkdir <name>:<path>`.

    Raises ValueError on invalid input; lets RuntimeError from the CLI bubble up
    so the router can surface it.
    """
    cleaned = path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("Folder path is required.")
    segments = cleaned.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError("Invalid folder path.")
    await _rclone_cli("mkdir", f"{name}:{cleaned}", timeout=30)


async def delete_remote_folder(name: str, path: str) -> None:
    """Delete a folder on a configured remote via `rclone purge <name>:<path>`.

    `purge` removes the folder and all of its contents that rclone can see.
    Under drive.file scope, that's only files/folders rclone itself created.

    Raises ValueError on invalid input; lets RuntimeError from the CLI bubble up.
    """
    cleaned = path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("Folder path is required.")
    segments = cleaned.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError("Invalid folder path.")
    await _rclone_cli("purge", f"{name}:{cleaned}", timeout=60)


def _validate_upload_filename(filename: str) -> str:
    """Validate and return the cleaned filename. Raises ValueError on bad input."""
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("Filename is required.")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError("Filename must not contain '/' or '\\'.")
    if cleaned in (".", ".."):
        raise ValueError("Invalid filename.")
    if any(ord(c) < 0x20 for c in cleaned):
        raise ValueError("Filename contains control characters.")
    return cleaned


async def upload_file_to_remote(
    name: str,
    folder_path: str,
    filename: str,
    size: int | None,
    chunk_iter: AsyncIterator[bytes],
) -> str:
    """Stream `chunk_iter` to `<name>:<folder_path>/<filename>` via `rclone rcat`.

    folder_path may be empty (root). Returns the cleaned remote-relative path.
    Raises ValueError on invalid path/filename; RuntimeError on rclone failure.
    """
    safe_name = _validate_upload_filename(filename)

    cleaned_folder = folder_path.strip().lstrip("/")
    if cleaned_folder:
        if any(seg in ("", ".", "..") for seg in cleaned_folder.split("/")):
            raise ValueError("Invalid folder path.")
        rel_path = f"{cleaned_folder}/{safe_name}"
    else:
        rel_path = safe_name

    target = f"{name}:{rel_path}"
    if size is not None:
        argv: tuple[str, ...] = ("rcat", "--size", str(size), target)
    else:
        argv = ("rcat", target)

    await _rclone_cli_stdin_stream(*argv, chunk_iter=chunk_iter)
    return rel_path


async def list_remote_folders(name: str) -> list[dict]:
    """List top-level folders on a remote via `rclone lsjson --dirs-only`.

    Returns a list of {name, modified} dicts. With drive.file scope, only
    folders rclone itself created are visible.
    """
    raw = await _rclone_cli(
        "lsjson", f"{name}:", "--dirs-only", "--max-depth", "1", timeout=30
    )
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"rclone returned non-JSON output: {exc}") from exc
    return [
        {"name": it.get("Name") or it.get("Path") or "", "modified": it.get("ModTime")}
        for it in items
        if it.get("IsDir")
    ]
