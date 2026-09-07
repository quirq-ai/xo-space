"""Session ids this pod has been handed — a local record, not a mint.

The UI needs *some* bearer on the Composio routes, because those are the one part of this
server that refuse to act without knowing the request came from a vouched-for tab. It does
not need an identity: this backend serves exactly one XO account and runs in exactly one
workspace, so the tenant key is a constant fetched from xo-swarm-api
(:func:`.state.principal`), not something a session id selects.

**The ids are minted by xo-swarm-api** (``POST /auth/session/self``,
``auth/session_identity.py`` over there), which is where authentication lives. This module
records what the proxy route ``GET /xo-auth/session/self`` was handed, so that checking an
id on the *next* request stays a dict lookup — the check runs on the MCP proxy's hot path,
on ``initialize``, ``tools/list`` and every ``tools/call``, and must not become a round
trip to the swarm.

What that costs, stated plainly: this record is a cache with a TTL, so an id revoked at
the swarm keeps working here until it expires. The bound is the TTL, and the swarm remains
the authority — ``GET /auth/session/resolve`` answers definitively for anything that needs
a definitive answer.

The table is in-memory and per-process on purpose. Persisting an id would outlive the
process whose credential vouched for it, and an id minted for one workspace must not mean
anything in another.

The raw XO token is never stored here, and never was.
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
