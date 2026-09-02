"""Persist the last observed Claude native-login failure without storing CLI output."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

AUTH_FAILURE_FILE = Path("/tmp/xo-claude-auth-failure.json")


def auth_failure_reason(detail: str | None) -> str | None:
    """Classify CLI output that conclusively shows native authentication failed."""
    message = (detail or "").lower()
    if "oauth session expired" in message:
        return "session_expired"
    if any(marker in message for marker in (
        "failed to authenticate",
        "authentication required",
        "not logged in",
    )):
        return "authentication_failed"
    return None


def record_auth_failure(detail: str | None) -> str | None:
    """Record a classified failure, returning its reason; ignore unrelated errors."""
    reason = auth_failure_reason(detail)
    if reason is None:
        return None
    try:
        AUTH_FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=AUTH_FAILURE_FILE.parent, delete=False,
            prefix=f".{AUTH_FAILURE_FILE.name}.", suffix=".tmp",
        ) as tmp:
            json.dump({"reason": reason}, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, AUTH_FAILURE_FILE)
    except OSError:
        # The auth result must never depend on whether this diagnostic state can
        # be persisted (for example, in a read-only temporary filesystem).
        return None
    return reason


def last_auth_failure_reason() -> str | None:
    """Return the last recorded native-login failure reason, if any."""
    try:
        data = json.loads(AUTH_FAILURE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if reason in {"session_expired", "authentication_failed"} else None


def clear_auth_failure() -> None:
    """Forget a previous failure after a successful login or authenticated call."""
    try:
        AUTH_FAILURE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
