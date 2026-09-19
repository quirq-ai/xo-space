"""
rclone — the shared engine behind the file-storage connectors.

Not a connector itself: gdrive and onedrive each supply an
:class:`RcloneProvider` descriptor and let :class:`RcloneConnector` run the
generic ``rclone authorize`` dance, session lifecycle, and CLI plumbing.
``oauth_lock`` arbitrates the single OAuth callback port (:53682) between them.

Provider-specific code belongs in that provider's package, never here.
"""

from .connector import (
    OAUTH_TIMEOUT,
    RCLONE_CONFIG_PATH,
    RCLONE_OAUTH_PORT,
    SESSION_TTL,
    RcloneConnector,
    RcloneProvider,
    RcloneSession,
    ensure_rclone_running,
    rclone_available,
)
from .oauth_lock import (
    OAUTH_LIVENESS_WINDOW,
    cancel_all_active_oauth,
    has_active_oauth,
    register_sessions,
)

__all__ = [
    "OAUTH_LIVENESS_WINDOW",
    "OAUTH_TIMEOUT",
    "RCLONE_CONFIG_PATH",
    "RCLONE_OAUTH_PORT",
    "SESSION_TTL",
    "RcloneConnector",
    "RcloneProvider",
    "RcloneSession",
    "cancel_all_active_oauth",
    "ensure_rclone_running",
    "has_active_oauth",
    "rclone_available",
    "register_sessions",
]
