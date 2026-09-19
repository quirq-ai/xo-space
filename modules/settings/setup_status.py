"""Read-only identity checks for Setup, using the existing auth transports.

Configured workspace metadata is separate from a verified account. GitHub
uses the same credential precedence as project backup/sync, not Composio's
independently connected accounts or a native git credential helper.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from services.cowork_agent import coder_identity
from services.cowork_agent.xo_projects_sync import github
from services.swarm_api import auth as swarm_auth

CHECK_TIMEOUT_SECONDS = 10.0


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _space_status() -> dict:
    try:
        space_id = coder_identity.xo_space_id()
        return {
            "status": "configured" if space_id else "not_configured",
            "id": space_id,
            "label": coder_identity.workspace_name(),
            "owner": coder_identity.owner_name(),
        }
    except Exception:
        return {"status": "unavailable", "id": None, "label": None, "owner": None}


async def _xo_status() -> dict:
    result = {"status": "unavailable", "user_id": None}
    try:
        # This is the same validation GET used by /xo-auth/whoami. It neither
        # mints a browser session nor adopts an unverified local user ID.
        response = await asyncio.wait_for(swarm_auth.get_user_id(), CHECK_TIMEOUT_SECONDS)
        if response.unauthenticated:
            result["status"] = "not_configured"
        elif response.status in (401, 403):
            result["status"] = "rejected"
        elif response.ok and isinstance(response.data, dict):
            user_id = _text(response.data.get("user_id"))
            if user_id:
                result.update(status="connected", user_id=user_id)
    except Exception:
        pass  # Transport/body errors can contain credentials; never return them.
    return result


async def _github_status() -> dict:
    result = {"status": "unavailable", "username": None, "source": None}
    try:
        auth = await github.resolve_auth(read_only=True)
        result["source"] = auth.source if auth.source in ("connector", "env") else None
        username = _text(await asyncio.wait_for(github.discover_owner(auth), CHECK_TIMEOUT_SECONDS))
        if username:
            result.update(status="connected", username=username)
    except github.AuthMissingError:
        result["status"] = "not_configured"
    except github.GitHubAPIError as exc:
        # 403 can be rate limiting or a permissions policy, not revoked auth.
        if exc.status == 401:
            result["status"] = "rejected"
    except Exception:
        pass
    return result


async def snapshot() -> dict:
    space = _space_status()
    xo, github_status = await asyncio.gather(_xo_status(), _github_status())
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "space": space,
        "xo": xo,
        "github": github_status,
    }
