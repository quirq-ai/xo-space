from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

_SESSION_TTL = float(os.getenv("XO_SESSION_TTL", str(12 * 60 * 60)))


@dataclass
class _Entry:
    user_id: str
    expires_at: float


_SESSIONS: dict[str, _Entry] = {}


def _prune(now: float) -> None:
    expired = [sid for sid, e in _SESSIONS.items() if e.expires_at <= now]
    for sid in expired:
        _SESSIONS.pop(sid, None)


def register(user_id: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    # Stores the bare XO **account** id, not a workspace-scoped principal. That
    # is deliberate: `_SESSIONS` is per-process and in-memory, so a session id
    # minted in one workspace does not exist in another workspace's process and
    # cannot be replayed across pods. Scoping happens once, at read, in
    # composio.identity.resolve_user_from_bearer. Do not scope here too.
    #
    # The caller's XO access token is deliberately NOT stored. Nothing reads it
    # back, and holding a live credential for the process lifetime is exposure
    # without a purpose. Add it back only alongside a real consumer.
    if not user_id:
        return None
    now = time.monotonic()
    _prune(now)
    session_id = secrets.token_urlsafe(32)
    _SESSIONS[session_id] = _Entry(
        user_id=str(user_id),
        expires_at=now + (ttl_seconds if ttl_seconds and ttl_seconds > 0 else _SESSION_TTL),
    )
    log.info("session_identity: registered session for user=%s", user_id)
    return session_id


async def mint(xo_access_token: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    if not xo_access_token:
        return None
    from services.cowork_agent.connectors.composio.identity import _validate_token

    user_id = await _validate_token(xo_access_token)
    if not user_id:
        return None
    return register(user_id, ttl_seconds=ttl_seconds)


def resolve(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    _prune(time.monotonic())
    entry = _SESSIONS.get(session_id)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _SESSIONS.pop(session_id, None)
        return None
    return entry.user_id
