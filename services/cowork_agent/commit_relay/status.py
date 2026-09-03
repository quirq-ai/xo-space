"""In-memory relay status. Volatile by design: restarts empty and repopulates
on the first tick. Single-process writer (the poller task); readers get copies.

Two read shapes:
- snapshot(): everything, for GET /api/relay/status.
- feed_view(): a stable projection (no timestamps, no counters) for a
  change-published feed. `on_change` callbacks fire when it changes.

`recent` records TRANSITIONS, not states: shared_with_you (repo entered
`available`), fetched, error, revoked (repo left membership). These four kinds
are the notification vocabulary a frontend can map to toasts.
"""
from __future__ import annotations

import copy
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

FEED_RECENT_LIMIT = 20

_state: dict = {}
_callbacks: list[Callable[[], None]] = []
_last_feed: str | None = None


def reset() -> None:
    """Fresh state (process start and tests)."""
    global _last_feed
    _state.clear()
    _state.update({
        "enabled": True,
        "workspace_configured": True,
        "reason": None,               # disabled | no_workspace_id | no_auth | None
        "cadence": "parked",          # parked | running
        "last_poll_at": None,
        "last_poll_ok": None,
        "repos": {},                  # repo -> {project, shared, available, last_fetch_at,
                                      #          fetched, pending_github, last_error}
        "recent": deque(maxlen=50),   # [{at, repo, kind, detail}]
    })
    _last_feed = None


reset()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo(repo: str) -> dict:
    return _state["repos"].setdefault(repo, {
        "project": None, "shared": False, "available": False,
        "last_fetch_at": None, "fetched": 0,
        "pending_github": False, "last_error": None,
    })


def _event(repo: str, kind: str, detail: str = "") -> None:
    _state["recent"].append({"at": _now(), "repo": repo, "kind": kind, "detail": detail})


# ── writers ──────────────────────────────────────────────────────────────────

def set_parked(reason: str) -> None:
    _state["cadence"] = "parked"
    _state["reason"] = reason
    _state["enabled"] = reason != "disabled"
    _state["workspace_configured"] = reason != "no_workspace_id"


def record_poll(ok: bool, membership: set | None = None, local: dict | None = None) -> None:
    _state["last_poll_at"] = _now()
    _state["last_poll_ok"] = ok
    _state["enabled"] = True
    _state["workspace_configured"] = True
    _state["reason"] = None
    _state["cadence"] = "running"
    if membership is None:
        return
    local = local or {}
    for repo, project in local.items():
        _repo(repo)["project"] = project
    for repo, r in list(_state["repos"].items()):
        now_shared = repo in membership
        if r["shared"] and not now_shared:
            _event(repo, "revoked", "repo left membership")
            r["available"] = False
        r["shared"] = now_shared
        if repo in local:
            r["available"] = False
    for repo in membership:
        r = _repo(repo)
        r["shared"] = True
        if repo in local:
            r["available"] = False


def record_available(repo: str) -> None:
    r = _repo(repo)
    if not r["available"]:
        r["available"] = True
        _event(repo, "shared_with_you", "shared with this workspace, not cloned here")


def record_fetch(repo: str, project: str, n: int) -> None:
    r = _repo(repo)
    r.update(project=project, last_fetch_at=_now(), fetched=r["fetched"] + n,
             pending_github=False, last_error=None)
    _event(repo, "fetched", f"{n} commit(s)")


def record_synced(repo: str, project: str) -> None:
    _repo(repo).update(project=project, pending_github=False, last_error=None)


def record_repo_error(repo: str, project, err: str, pending_github: bool = False) -> None:
    r = _repo(repo)
    if project:
        r["project"] = project
    r["last_error"] = err
    r["pending_github"] = pending_github
    _event(repo, "error", err)


# ── readers ──────────────────────────────────────────────────────────────────

def member_repos() -> set[str]:
    return {repo for repo, r in _state["repos"].items() if r.get("shared")}


def snapshot() -> dict:
    snap = copy.deepcopy({k: v for k, v in _state.items() if k != "recent"})
    snap["recent"] = list(_state["recent"])
    return snap


def feed_view() -> dict:
    """Stable projection: changes only when something a person would care
    about changed. No timestamps, no counters."""
    repos = {
        repo: {"project": r["project"], "shared": r["shared"],
               "available": r["available"], "last_error": r["last_error"]}
        for repo, r in sorted(_state["repos"].items())
    }
    recent = [{"repo": e["repo"], "kind": e["kind"], "detail": e["detail"]}
              for e in list(_state["recent"])[-FEED_RECENT_LIMIT:]]
    return {"cadence": _state["cadence"], "reason": _state["reason"],
            "repos": repos, "recent": recent}


# ── change hook ──────────────────────────────────────────────────────────────

def on_change(callback: Callable[[], None]) -> None:
    """Register a callback for 'feed_view changed'. The events feed registers
    its request_refresh here when it lands; with no callbacks this is free."""
    _callbacks.append(callback)


def notify_if_changed() -> bool:
    """Call at the end of a tick. True iff feed_view differs from last time."""
    global _last_feed
    current = json.dumps(feed_view(), sort_keys=True)
    if current == _last_feed:
        return False
    _last_feed = current
    for cb in list(_callbacks):
        try:
            cb()
        except Exception as exc:  # noqa: BLE001 — a listener must not break the loop
            log.warning("commit_relay: on_change listener failed: %s", exc)
    return True
