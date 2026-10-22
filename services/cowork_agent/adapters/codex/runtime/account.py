"""What a codex session knows about its account: the login it started with
and the rate limits codex reports (storm ch 03 §3.4, §9).

* ``login_fingerprint``: a digest of who codex is logged in as
  (``$CODEX_HOME/auth.json``: its mode, the ChatGPT account id, the API key)
  and the key variables. A session compares it at each turn boundary; a
  change means the person connected, disconnected or switched the codex
  login, and the engine recycles the session (§0.79). codex rotates its
  refresh token on every refresh, so the tokens themselves are left out:
  a refresh is not a new login. Only digests are kept, never a credential.
* ``account_fingerprint``: a stable, non-secret label of the account, so the
  engine keys its pool by agent × account (ch 07 §5.1).
* ``UsageGuard``: the last rate-limit state per window, one per account,
  shared by that account's sessions. While a window is used up, has no
  credits to spill into, and its reset is still ahead, ``send`` refuses with
  ``rate_limited``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.adapters.codex.runtime.env import AUTH_VARS
from services.cowork_agent.adapters.contract import RuntimeFailure, UsageSnapshot, UsageWindow

AUTH_FILE = "auth.json"
#: The account's own limit (``RateLimitSnapshot.limitId``); a snapshot of
#: another limit (one model's own) never closes the account's pool.
ACCOUNT_LIMIT = "codex"
#: A window at or past this share used is a warning (the engine's pool warns at 80 %).
WARN_AT = 0.8


def _auth(codex_home: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((codex_home / AUTH_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _identity(codex_home: Path) -> str:
    auth = _auth(codex_home)
    if auth is None:
        return "absent"
    if auth.get("unreadable"):
        return "unreadable"
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    key = auth.get("OPENAI_API_KEY")
    return "\0".join((str(auth.get("auth_mode") or ""), str(tokens.get("account_id") or ""),
                      hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) and key else ""))


def login_fingerprint(codex_home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Changes when the login changes, not when codex refreshes its tokens.
    Blocking: call it in a thread."""
    environ = os.environ if environ is None else environ
    digest = hashlib.sha256(_identity(codex_home).encode() + b"\0")
    for var in AUTH_VARS:
        digest.update(var.encode() + b"=" + environ.get(var, "").encode() + b"\0")
    return digest.hexdigest()


def account_fingerprint(codex_home: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Which account codex runs on, as ``acct-<12 hex>``; None when nothing
    logs it in. Blocking: call it in a thread."""
    environ = os.environ if environ is None else environ
    identity = _identity(codex_home)
    if identity in ("absent", "unreadable"):
        key = next((environ[v] for v in AUTH_VARS if environ.get(v)), None)
        identity = f"key:{hashlib.sha256(key.encode()).hexdigest()}" if key else ""
    return f"acct-{hashlib.sha256(identity.encode()).hexdigest()[:12]}" if identity else None


def logged_in(codex_home: Path, environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    auth = _auth(codex_home)
    if auth and not auth.get("unreadable"):
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        if tokens.get("refresh_token") or tokens.get("access_token") or auth.get("OPENAI_API_KEY"):
            return True
    return any(environ.get(v) for v in AUTH_VARS)


# ── rate limits (account/rateLimits/*) ────────────────────────────────────


@dataclass(frozen=True)
class Window:
    """One window of codex's rate limits, in the contract's terms."""

    name: str
    status: str                         # ok | warning | limited
    utilization: float | None
    resets_at: str | None
    overage: bool


def iso_from_epoch(seconds: object) -> str | None:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def windows(snapshot: object) -> list[Window]:
    """``RateLimitSnapshot`` (``primary``, ``secondary``, ``credits``) as
    windows. A window used up while the account has credits spills into
    paid usage: it is ``overage`` (the engine pauses new turns and asks,
    ch 03 §13 item 4); without credits it is ``limited`` until its reset."""
    if not isinstance(snapshot, Mapping) or snapshot.get("limitId") not in (None, ACCOUNT_LIMIT):
        return []
    credits = snapshot.get("credits") if isinstance(snapshot.get("credits"), Mapping) else {}
    has_credits = bool(credits.get("hasCredits")) or bool(credits.get("unlimited"))
    reached = snapshot.get("rateLimitReachedType")
    out = []
    for slot in ("primary", "secondary"):
        window = snapshot.get(slot)
        if not isinstance(window, Mapping):
            continue
        used = window.get("usedPercent")
        utilization = min(max(float(used) / 100, 0.0), 1.0) if isinstance(used, (int, float)) else None
        minutes = window.get("windowDurationMins")
        name = _window_name(minutes) if isinstance(minutes, int) else slot
        full = utilization is not None and utilization >= 1.0
        if full and has_credits:
            status, overage = "ok", True
        elif full or (reached and slot == "primary" and not has_credits):
            status, overage = "limited", False
        elif utilization is not None and utilization >= WARN_AT:
            status, overage = "warning", False
        else:
            status, overage = "ok", False
        out.append(Window(name=name, status=status, utilization=utilization,
                          resets_at=iso_from_epoch(window.get("resetsAt")), overage=overage))
    return out


#: How much each state weighs on the pool: the worst window is the account's.
_SEVERITY = {"ok": 0, "warning": 1, "limited": 2}


def worst(found: list[Window]) -> Window | None:
    """The window that decides the account's pool: one in paid usage first,
    then a used-up one, then a warning, then the fullest. The engine keeps one
    state per pool, set by each event, so a calmer window must never be
    reported after a worse one."""
    return max(found, key=lambda w: (w.overage, _SEVERITY.get(w.status, 0), w.utilization or 0.0), default=None)


def _window_name(minutes: int) -> str:
    if minutes == 300:
        return "five_hour"
    if minutes == 10080:
        return "weekly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}_day"
    if minutes % 60 == 0:
        return f"{minutes // 60}_hour"
    return f"{minutes}_minute"


class UsageGuard:
    """The last rate-limit state codex reported, per window."""

    def __init__(self) -> None:
        self._windows: dict[str, Window] = {}
        self._plan: str | None = None
        self._updated_at: str | None = None

    def update(self, snapshot: object) -> list[Window]:
        found = windows(snapshot)
        for window in found:
            self._windows[window.name] = window
        if isinstance(snapshot, Mapping) and isinstance(snapshot.get("planType"), str):
            self._plan = snapshot["planType"]
        if found:
            self._updated_at = _now()
        return found

    def check(self) -> None:
        now = datetime.now(timezone.utc)
        for name, w in self._windows.items():
            reset = _parse(w.resets_at)
            if w.status == "limited" and reset is not None and reset > now:
                raise RuntimeFailure("rate_limited", f"codex's {name} limit is reached until {w.resets_at}",
                                     retryable=True, retry_at=reset)

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            windows=tuple(UsageWindow(name=name, status=w.status, utilization=w.utilization, resets_at=w.resets_at)
                          for name, w in sorted(self._windows.items())),
            plan=self._plan, overage=any(w.overage for w in self._windows.values()), updated_at=self._updated_at)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
