"""
Cross-connector OAuth port lock.

rclone's OAuth callback server binds to a single hardcoded port (53682) for
every backend that uses its bundled OAuth client (Google Drive, OneDrive, ...).
Only ONE OAuth flow can be active at a time across ALL connectors — if a
second `rclone authorize` subprocess starts while another is still waiting for
its callback, the second one will fail to bind :53682 and crash silently.

Each connector module (gdrive.provider, onedrive.provider, ...) registers a
zero-arg getter that returns its current sessions. `has_active_oauth()` scans
all registered iterables for a session whose `status == "awaiting_oauth"`.
"""

import time
from typing import Any, Awaitable, Callable, Iterable

# A session is treated as "live" only while its OAuth window is open. Past this
# many seconds since `oauth_started_at`, the session is considered dead and the
# lock auto-releases — even if its background task hasn't yet flipped the status.
OAUTH_LIVENESS_WINDOW = 300  # 5 min, matches connectors' OAUTH_TIMEOUT

_session_iterables: list[Callable[[], Iterable[Any]]] = []
_session_cancellers: list[Callable[[str], Awaitable[None]]] = []


def register_sessions(
    getter: Callable[[], Iterable[Any]],
    canceller: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Connector modules call this at import time with `lambda: _sessions.values()`
    and optionally an async `cancel_session(session_id)` for cross-connector takeover."""
    if getter not in _session_iterables:
        _session_iterables.append(getter)
    if canceller is not None and canceller not in _session_cancellers:
        _session_cancellers.append(canceller)


def has_active_oauth() -> bool:
    now = time.time()
    for getter in _session_iterables:
        try:
            for session in getter():
                if getattr(session, "status", None) != "awaiting_oauth":
                    continue
                started = getattr(session, "oauth_started_at", None)
                # Sessions without a timestamp (older code paths) or within the
                # liveness window block. Anything older is considered dead.
                if started is None or now - started < OAUTH_LIVENESS_WINDOW:
                    return True
        except Exception:
            continue
    return False


async def cancel_all_active_oauth() -> None:
    """Cancel every `awaiting_oauth` session across all registered connectors."""
    pairs: list[tuple[Callable[[str], Awaitable[None]], str]] = []
    for getter, canceller in zip(_session_iterables, _session_cancellers):
        try:
            for session in getter():
                if getattr(session, "status", None) == "awaiting_oauth":
                    sid = getattr(session, "session_id", None)
                    if sid:
                        pairs.append((canceller, sid))
        except Exception:
            continue
    for canceller, sid in pairs:
        try:
            await canceller(sid)
        except Exception:
            pass
