"""Swarm calls for the XO GitHub App: the install URL, and minting a short-lived
installation token from the grant the install callback handed back. Never
raises; callers get a SwarmResult."""
from __future__ import annotations

from ._http import SwarmResult, request


async def install_url(return_to: str | None) -> SwarmResult:
    params = {"return_to": return_to} if return_to else None
    return await request("GET", "/github-app/install-url", params=params)


async def installation_token(grant: str) -> SwarmResult:
    """``data`` on success: ``{token, expires_at, permissions, repository_selection}``.
    403 means the grant is invalid, expired or belongs to another XO user; 409
    means the app was uninstalled or suspended on GitHub."""
    return await request("POST", "/github-app/token", headers={"X-GitHub-App-Grant": grant})
