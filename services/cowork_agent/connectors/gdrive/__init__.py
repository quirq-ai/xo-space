"""
Google Drive connector (rclone-backed).

``provider`` holds the Drive-specific descriptor and file operations; the
generic rclone plumbing lives in the sibling ``rclone`` package.
"""

from .provider import (
    RCLONE_CONFIG_PATH,
    RcloneSession,
    cancel_session,
    create_remote_session,
    delete_remote,
    delete_remote_folder,
    ensure_rclone_running,
    get_session,
    list_drive_remotes,
    list_remote_folders,
    mkdir_remote_path,
    rclone_available,
    upload_file_to_remote,
    validate_remote_name,
)

__all__ = [
    "RCLONE_CONFIG_PATH",
    "RcloneSession",
    "cancel_session",
    "create_remote_session",
    "delete_remote",
    "delete_remote_folder",
    "ensure_rclone_running",
    "get_session",
    "list_drive_remotes",
    "list_remote_folders",
    "mkdir_remote_path",
    "rclone_available",
    "upload_file_to_remote",
    "validate_remote_name",
]
