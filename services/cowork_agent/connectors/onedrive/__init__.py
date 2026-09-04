"""
OneDrive connector (rclone-backed).

``provider`` holds the OneDrive-specific descriptor; the generic rclone
plumbing lives in the sibling ``rclone`` package.
"""

from .provider import (
    RcloneSession,
    cancel_session,
    create_remote_session,
    delete_remote,
    ensure_rclone_running,
    get_session,
    list_onedrive_remotes,
    rclone_available,
    validate_remote_name,
)

__all__ = [
    "RcloneSession",
    "cancel_session",
    "create_remote_session",
    "delete_remote",
    "ensure_rclone_running",
    "get_session",
    "list_onedrive_remotes",
    "rclone_available",
    "validate_remote_name",
]
