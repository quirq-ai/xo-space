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
  ahead, ``send`` refuses with ``rate_limited``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from services.cowork_agent.adapters.contract import RuntimeFailure, UsageSnapshot, UsageWindow

#: Today's auth function reads these (``ClaudeCodeAdapter.apply_auth``).
TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
CREDENTIAL_FILES = (".credentials.json", "credentials.json")


def login_fingerprint(claude_home: Path, environ: Mapping[str, str] | None = None) -> str:
    """A digest that changes when the login changes, but not when claude
    refreshes its short-lived access token: the refresh token (or, without
    one, the access token) and the plan, plus the token variables. Blocking:
    call it in a thread."""
    environ = os.environ if environ is None else environ
    digest = hashlib.sha256()
    for name in CREDENTIAL_FILES:
        digest.update(name.encode() + b"\0" + _login_identity(claude_home / name) + b"\0")
    for var in TOKEN_VARS:
        digest.update(var.encode() + b"=" + environ.get(var, "").encode() + b"\0")
    return digest.hexdigest()


def account_fingerprint(claude_home: Path, home: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Which account claude runs on, as ``acct-<12 hex>``, a digest that names
    no credential: a token variable that logs it in (today's auth function
    reads them first), else claude's own account id (``oauthAccount`` in
    ``~/.claude.json``), else the login in its credentials file. None when
    nothing logs claude in. Blocking: call it in a thread."""
    environ = os.environ if environ is None else environ
    source = next((f"{var}={environ[var]}" for var in TOKEN_VARS if environ.get(var)), None)
    if source is None:
        source = _oauth_account(home / ".claude.json")
    if source is None:
        identity = next((i for name in CREDENTIAL_FILES
                         if (i := _login_identity(claude_home / name)) not in (b"absent", b"unreadable")), None)
        source = identity.decode(errors="replace") if identity else None
    return f"acct-{hashlib.sha256(source.encode()).hexdigest()[:12]}" if source else None


def _oauth_account(config: Path) -> str | None:
    try:
        account = json.loads(config.read_text(encoding="utf-8")).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    return f"oauth:{uuid}" if isinstance(uuid, str) and uuid else None


def _login_identity(path: Path) -> bytes:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return b"absent"
    except (OSError, ValueError):
        return b"unreadable"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        oauth = data if isinstance(data, dict) else {}
    key = oauth.get("refreshToken") or oauth.get("accessToken") or ""
    return f"{key}\0{oauth.get('subscriptionType')}".encode()


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
        self._updated_at: str | None = None

    def update(self, *, status: str, window: str | None, resets_at: str | None, utilization: float | None,
               overage: bool) -> None:
        self._windows[window or "default"] = _Window(status, resets_at, utilization, overage)
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
            overage=any(w.overage for w in self._windows.values()), updated_at=self._updated_at)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
