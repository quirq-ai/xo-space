"""What a claude session knows about its account: the login it started with
and the rate limits claude reports (storm ch 02 §2.3, §6).

* ``login_fingerprint``: a digest of claude's own credential state (the login
  in ``~/.claude/.credentials.json`` and the token variables today's auth
  function reads). A session compares it at each turn boundary; a change
  means the person connected or disconnected a subscription, and the engine
  recycles the session (§0.79). Only digests are kept, never a credential.
* ``account_fingerprint``: a stable, non-secret label of the account claude
  runs on, so the engine keys its pool by agent × account (ch 07 §5.1).
* ``UsageGuard``: the last rate-limit state per window, one per account,
  shared by that account's sessions. While a window is rejected and its reset is still
  ahead, ``send`` refuses with ``rate_limited``. A report with no number keeps the
  last one for the same window and reset, and each paid-usage fact keeps its last
  value until claude says another.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.claude_code.lifecycle.login import (  # noqa: F401 - re-exported
    CREDENTIAL_FILES, TOKEN_VARS, account_fingerprint, api_key_login, login_identity)
from services.cowork_agent.adapters.contract import PaidUsage, RuntimeFailure, UsageSnapshot, UsageWindow


def login_fingerprint(claude_home: Path, environ: Mapping[str, str] | None = None) -> str:
    """A digest that changes when the login changes, but not when claude
    refreshes its short-lived access token: the refresh token (or, without
    one, the access token) and the plan, plus the token variables. Blocking:
    call it in a thread."""
    environ = os.environ if environ is None else environ
    digest = hashlib.sha256()
    for name in CREDENTIAL_FILES:
        digest.update(name.encode() + b"\0" + login_identity(claude_home / name) + b"\0")
    for var in TOKEN_VARS:
        digest.update(var.encode() + b"=" + environ.get(var, "").encode() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class _Window:
    status: str                         # ok | warning | limited
    resets_at: str | None
    utilization: float | None
    overage: bool


class UsageGuard:
    """The last rate-limit state claude reported, per window."""

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}
        self._paid = PaidUsage()
        self._updated_at: str | None = None

    def update(self, *, status: str, window: str | None, resets_at: str | None, utilization: float | None,
               overage: bool, paid: PaidUsage | None = None) -> None:
        name = window or "default"
        old = self._windows.get(name)
        if utilization is None and old is not None and old.resets_at == resets_at:
            utilization = old.utilization
        self._windows[name] = _Window(status, resets_at, utilization, overage)
        if paid is not None:
            self._paid = replace(self._paid, **{f.name: v for f in fields(PaidUsage)
                                                if (v := getattr(paid, f.name)) is not None})
        self._updated_at = _now()

    def check(self) -> None:
        """Raise ``rate_limited`` while a window is rejected until a reset
        still ahead."""
        now = datetime.now(timezone.utc)
        for name, w in self._windows.items():
            reset = _parse(w.resets_at)
            if w.status == "limited" and reset is not None and reset > now:
                raise RuntimeFailure("rate_limited", f"claude's {name} limit is reached until {w.resets_at}",
                                     retryable=True, retry_at=reset)

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            windows=tuple(UsageWindow(name=name, status=w.status, utilization=w.utilization, resets_at=w.resets_at)
                          for name, w in sorted(self._windows.items())),
            overage=any(w.overage for w in self._windows.values()), updated_at=self._updated_at,
            paid=self._paid if self._paid != PaidUsage() else None)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
