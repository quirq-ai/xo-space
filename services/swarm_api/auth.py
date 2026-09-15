"""Swarm calls for identity: the browser-auth handshake (start / status /
consume) and token validation. The handshake runs unauthenticated by
definition, since it is how a session token is obtained; validation carries
the token. Token *storage* stays in routers/auth/auth.py; this module only
talks to the swarm."""
from __future__ import annotations

import os

from ._http import SwarmResult, request


def _path(env: str, default: str) -> str:
    return (os.getenv(env, "") or "").strip() or default


async def browser_auth_start(scopes: str | None, client_reference: str | None) -> SwarmResult:
    return await request("POST", _path("XO_AUTH_START_PATH", "/auth/browser/start"),
                         json={"scopes": scopes, "client_reference": client_reference}, auth=False)


async def browser_auth_status(auth_session_id: str, poll_token: str) -> SwarmResult:
    return await request("GET", _path("XO_AUTH_STATUS_PATH", "/auth/browser/status") + "/" + auth_session_id,
                         params={"poll_token": poll_token}, auth=False)


async def browser_auth_consume(auth_session_id: str, poll_token: str) -> SwarmResult:
    return await request("POST", _path("XO_AUTH_CONSUME_PATH", "/auth/browser/consume"),
                         json={"auth_session_id": auth_session_id, "poll_token": poll_token}, auth=False)


async def get_user_id() -> SwarmResult:
    """Validate the stored token; the swarm answers with the user it belongs to."""
    return await request("GET", _path("XO_GET_USER_ID_PATH", "/get-user-id"))
