"""
Microsoft OneDrive connector via rclone (rclone backend type `onedrive`).

The generic rclone plumbing and the OAuth-flow engine live in
:mod:`services.cowork_agent.connectors.rclone`. This module supplies only the
OneDrive-specific descriptor:

  1. Subprocess: `rclone authorize --auth-no-open-browser onedrive`
  2. Auth URL is hosted on Microsoft (login.microsoftonline.com / login.live.com).
  3. A working onedrive remote needs more fields than gdrive. After capturing
     the OAuth token, we call Microsoft Graph (`GET /v1.0/me/drive`) to discover
     `drive_id` + `drive_type`, then write all five fields to rclone.conf:
         [name]
         type       = onedrive
         region     = global
         token      = {json}
         drive_id   = <from graph>
         drive_type = personal | business | documentLibrary

The `rclone.conf` file and rclone's :53682 OAuth callback are shared with the
gdrive connector; the cross-connector OAuth lock serialises the flows.
"""

import json
import logging
import re
from typing import Optional

import httpx

from ..rclone.connector import (
    RcloneConnector,
    RcloneProvider,
    RcloneSession,  # noqa: F401  (exported for callers that type-hint sessions)
    ensure_rclone_running,  # noqa: F401  (re-exported for symmetry / callers)
    rclone_available,  # noqa: F401  (re-exported for the onedrive router)
)

log = logging.getLogger(__name__)

__all__ = [
    "ensure_rclone_running",
    "rclone_available",
    "list_onedrive_remotes",
    "validate_remote_name",
    "create_remote_session",
    "cancel_session",
    "delete_remote",
    "get_session",
]

# ---------------------------------------------------------------------------
# OneDrive provider descriptor
# ---------------------------------------------------------------------------

_AUTH_URL_RE = re.compile(
    r"https?://\S+(?:auth\?state|login\.microsoftonline\.com|login\.live\.com|oauth2/.+/authorize)\S*"
)


async def _resolve_default_drive(token_json: str) -> tuple[str, str]:
    """Call Microsoft Graph /me/drive with the freshly captured OAuth token to
    discover the user's default drive id + type. rclone needs both fields in
    rclone.conf for a fully working onedrive remote."""
    try:
        token_obj = json.loads(token_json)
    except Exception as exc:
        raise RuntimeError(f"Could not parse OAuth token JSON: {exc}") from exc

    access_token = token_obj.get("access_token")
    if not access_token:
        raise RuntimeError("OAuth token JSON had no access_token field.")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/me/drive",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Microsoft Graph /me/drive failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()

    drive_id = body.get("id") or ""
    drive_type = body.get("driveType") or "personal"
    if not drive_id:
        raise RuntimeError("Microsoft Graph /me/drive response had no drive id.")
    return drive_id, drive_type


async def _build_config_section(name: str, token_json: str) -> str:
    """Resolve drive_id/drive_type via Graph, then build the full INI section.

    Raises with the user-facing 'Could not look up your OneDrive' message so the
    flow fails with the same text it did before extraction."""
    try:
        drive_id, drive_type = await _resolve_default_drive(token_json)
    except Exception as exc:
        raise RuntimeError(f"Could not look up your OneDrive: {exc}") from exc

    log.info("OneDrive: resolved drive_id=%s… drive_type=%s", drive_id[:16], drive_type)
    return (
        f"\n[{name}]\n"
        f"type = onedrive\n"
        f"region = global\n"
        f"token = {token_json}\n"
        f"drive_id = {drive_id}\n"
        f"drive_type = {drive_type}\n"
    )


def _remote_summary(name: str, cfg: dict) -> Optional[dict]:
    if cfg.get("type") != "onedrive":
        return None
    return {
        "name": name,
        "type": "onedrive",
        "drive_type": cfg.get("drive_type", ""),
        "region": cfg.get("region", "global"),
        "complete": bool(cfg.get("token")) and bool(cfg.get("drive_id")),
    }


_PROVIDER = RcloneProvider(
    backend="onedrive",
    authorize_args=(),
    label="OneDrive",
    provider_name="Microsoft",
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
list_onedrive_remotes = _connector.list_remotes
