"""The terminal gate and limits, read from the environment on every call.

Off unless ``QUIRQ_TERMINAL_ENABLED`` is true (DEVELOPING.md §9: a new
behaviour gets its own ``QUIRQ_*`` variable with a safe default; the cloud
template and ``install.sh`` opt in).
"""

from __future__ import annotations

import os
import sys

_TRUE = {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, low: int, high: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return min(max(value, low), high)


def enabled() -> bool:
    return (os.getenv("QUIRQ_TERMINAL_ENABLED", "") or "").strip().lower() in _TRUE


def unavailable_reason() -> str | None:
    """Why terminals cannot be opened here, in words for the UI; None when they can."""
    if sys.platform.startswith("win") or not hasattr(os, "login_tty"):
        return "Terminals need Linux or macOS."
    if not enabled():
        return "Terminals are off. Set QUIRQ_TERMINAL_ENABLED=1 and restart the server to turn them on."
    return None


def max_sessions() -> int:
    """How many terminals may run at once."""
    return _int("QUIRQ_TERMINAL_MAX_SESSIONS", 8, low=1, high=64)


def detached_ttl_seconds() -> int:
    """A terminal nobody has watched for this long is closed; 0 keeps it forever."""
    return _int("QUIRQ_TERMINAL_DETACHED_TTL_SECONDS", 24 * 3600, low=0, high=365 * 24 * 3600)


def shell() -> list[str]:
    """The program a terminal runs: ``QUIRQ_TERMINAL_SHELL``, else the login
    shell (``$SHELL``), else ``/bin/bash``; started as a login shell (``-l``)."""
    configured = (os.getenv("QUIRQ_TERMINAL_SHELL", "") or "").strip()
    program = configured or (os.getenv("SHELL", "") or "").strip() or "/bin/bash"
    return [program, "-l"]


#: Output kept per terminal for the next viewer.
SCROLLBACK_BYTES = 256 * 1024
#: Read budget while someone watches (more than a browser can draw) and while
#: nobody does (enough for a dev server's log; a flood is slowed, not lost).
ATTACHED_RATE = 2 << 20
DETACHED_RATE = 256 << 10
#: An attach ticket is good once, for this long.
TICKET_TTL_SECONDS = 30.0
#: An exited terminal stays listed this long so its result can be seen.
EXITED_KEEP_SECONDS = 3600
