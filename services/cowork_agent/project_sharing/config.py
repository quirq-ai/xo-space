"""The relay's only reader of environment and auth.

Every other relay module asks here, so the Setup runtime allowlist
(runtime_config.RUNTIME_CONFIG_KEYS) has one file to point at if these
controls ever become UI-editable. Values are read on each call (cheap) so a
sign-in un-parks the loop without a restart; an .env edit still needs one.
"""
from __future__ import annotations

import os
import random

DEFAULT_INTERVAL = 60.0
MIN_INTERVAL = 5.0
DEFAULT_JITTER = 0.2


def workspace_id() -> str | None:
    return (os.getenv("XO_PROJECT_ID", "") or "").strip() or None


def enabled() -> bool:
    raw = (os.getenv("PROJECT_SHARING_ENABLED", "true") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def watch_branch() -> str:
    return (os.getenv("PROJECT_SHARING_WATCH_BRANCH", "main") or "main").strip() or "main"


def poll_interval() -> float:
    raw = (os.getenv("PROJECT_SHARING_POLL_INTERVAL_SECONDS", "") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL
    except ValueError:
        value = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, value)


def jitter_ratio() -> float:
    raw = (os.getenv("PROJECT_SHARING_POLL_JITTER_RATIO", "") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_JITTER
    except ValueError:
        value = DEFAULT_JITTER
    return min(0.9, max(0.0, value))


def jittered_interval() -> float:
    base = poll_interval()
    ratio = jitter_ratio()
    return max(1.0, base * (1.0 + random.uniform(-ratio, ratio)))


def auth_token() -> str | None:
    """Bearer token for swarm. Lazy import: routers.auth pulls in the FastAPI
    app's auth state, which unit tests must not load. Same accessor usage sync
    uses, so "signed in" means one thing everywhere."""
    try:
        from routers.auth.auth import get_auth_token
        return get_auth_token() or None
    except Exception:  # noqa: BLE001 — no auth module == not signed in
        return None


def parked_reason() -> str | None:
    """Why the loop must not touch the network, or None to run.
    Priority: disabled > no_workspace_id > no_auth."""
    if not enabled():
        return "disabled"
    if workspace_id() is None:
        return "no_workspace_id"
    if auth_token() is None:
        return "no_auth"
    return None
