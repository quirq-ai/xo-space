"""Session ids this pod has been handed — a local record, not a mint.

Ids are minted by xo-swarm-api (``POST /auth/session/self``); this records what
``GET /xo-auth/session/self`` was handed so the MCP proxy's hot path can check one with a
dict lookup instead of a round trip.

A cache with a TTL, so an id revoked at the swarm keeps working here until it expires;
``GET /auth/session/resolve`` is the authority when that matters. In-memory and
per-process on purpose. The raw XO token is never stored here.
"""

from __future__ import annotations

import os
import time
from typing import Optional

_SESSION_TTL = float(os.getenv("XO_SESSION_TTL", str(12 * 60 * 60)))

# session id -> expiry (monotonic)
_SESSIONS: dict[str, float] = {}


def _prune(now: float) -> None:
    for sid in [s for s, exp in _SESSIONS.items() if exp <= now]:
        _SESSIONS.pop(sid, None)


def remember(session_id: str, ttl_seconds: Optional[float] = None) -> str:
    """Record an id minted by xo-swarm-api, so later requests can check it locally.

    ``ttl_seconds`` is the swarm's own ``expires_in``: the local record must never outlive
    the session it stands for. Unusable values fall back to the default rather than to
    zero, which would make the id useless the moment it was handed out.
    """
    now = time.monotonic()
    _prune(now)
    try:
        ttl = _SESSION_TTL if ttl_seconds is None else float(ttl_seconds)
    except (TypeError, ValueError):
        ttl = _SESSION_TTL
    if ttl <= 0:
        ttl = _SESSION_TTL
    _SESSIONS[session_id] = now + ttl
    return session_id


def is_valid(session_id: Optional[str]) -> bool:
    """True iff this id was handed to this process and has not expired."""
    if not session_id:
        return False
    now = time.monotonic()
    expires_at = _SESSIONS.get(session_id)
    if expires_at is None:
        return False
    if expires_at <= now:
        _SESSIONS.pop(session_id, None)
        return False
    return True
