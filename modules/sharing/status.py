"""Relay status, on disk: ``sharing/state.json`` and ``sharing/events.jsonl``.

The snapshot (parked reason, cadence, last poll, one entry per repo) is a
:class:`services.storage.document.Document` written through ``modify`` on
every ``record_*`` call, so a restart starts from the last snapshot instead
of an empty one. The transitions are lines of an
:class:`services.storage.eventlog.EventLog` (``{ts, type: "sharing.<kind>",
repo, project, detail}``): the stream follows them and ``recent`` reads
them back. Single-process writer (the relay task); readers get fresh
copies from disk, never a shared object.

Two read shapes:
- snapshot(): everything, for GET /api/project-sharing/status.
- feed_view(): a stable projection (no timestamps, no counters) for a
  change-published feed. `on_change` callbacks fire when it changes.

`recent` records TRANSITIONS, not states: shared_with_you (repo entered
`available`), fetched, error, revoked (repo left membership), cloned and
clone_failed. These kinds are the notification vocabulary a frontend can
map to toasts (``events.TYPES`` lists them with their prefix).

A ``state.json`` that is not readable is served empty and never rewritten:
a ``record_*`` call raises the storage layer's 409 (its wire message names
the document, not the path) and the tick that called it logs and retries.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from services.timestamps import now_iso

from . import store

log = logging.getLogger(__name__)

FEED_RECENT_LIMIT = 20
#: How many transitions ``snapshot()["recent"]`` carries (the log keeps more).
RECENT_LIMIT = 50

_callbacks: list[Callable[[], None]] = []
_last_feed: str | None = None


def reset() -> None:
    """Forget what this process remembered: the change detector. The files
    are the truth, so a restart keeps the snapshot; a test gets its clean
    slate from its sandbox, not from here."""
    global _last_feed
    _last_feed = None


reset()


def _now() -> str:
    return now_iso()


def _repo(doc: dict, repo: str) -> dict:
    return doc["repos"].setdefault(repo, store.repo_defaults())


# ── the write path ───────────────────────────────────────────────────────────

Transition = tuple[str, str, str]


def _change(fn: Callable[[dict, list[Transition]], None]) -> dict:
    """One locked read-modify-write of ``state.json``. ``fn(doc, events)``
    edits the document in place and appends transitions to ``events`` as
    ``(repo, kind, detail)``; the file is written only when the document
    changed, and the transitions land in ``events.jsonl`` afterwards, each
    naming the project the document holds for its repo."""
    events: list[Transition] = []

    def apply(doc: dict) -> bool:
        before = json.dumps(doc, sort_keys=True, default=str)
        fn(doc, events)
        return json.dumps(doc, sort_keys=True, default=str) != before

    doc = store.state_document().modify(apply)
    if events:
        stamp = _now()
        store.events_log().append([
            {"ts": stamp, "type": store.EVENT_PREFIX + kind, "repo": repo,
             "project": (doc["repos"].get(repo) or {}).get("project"), "detail": detail}
            for repo, kind, detail in events
        ])
    return doc


# ── writers ──────────────────────────────────────────────────────────────────

def set_parked(reason: str) -> None:
    def fn(doc: dict, _events: list[Transition]) -> None:
        doc["cadence"] = "parked"
        doc["reason"] = reason
        doc["enabled"] = reason != "disabled"
        doc["workspace_configured"] = reason != "no_workspace_id"

    _change(fn)


def record_poll(ok: bool, membership: set | None = None, local: dict | None = None,
                members: dict | None = None) -> None:
    """`members` maps repo -> active member count as the swarm reported it this
    tick (owner included). Missing for a repo, or an older swarm that sends
    none, leaves the count unknown (None) rather than pretending to know."""
    def fn(doc: dict, events: list[Transition]) -> None:
        doc["last_poll_at"] = _now()
        doc["last_poll_ok"] = ok
        doc["enabled"] = True
        doc["workspace_configured"] = True
        doc["reason"] = None
        doc["cadence"] = "running"
        if membership is None:
            return
        local_map = local or {}
        counts = members or {}
        for repo, project in local_map.items():
            _repo(doc, repo)["project"] = project
        for repo, r in list(doc["repos"].items()):
            now_shared = repo in membership
            if r["shared"] and not now_shared:
                events.append((repo, "revoked", "repo left membership"))
                r["available"] = False
            r["shared"] = now_shared
            if not now_shared:
                r["members"] = None
            if repo in local_map:
                r["available"] = False
        for repo in membership:
            r = _repo(doc, repo)
            r["shared"] = True
            n = counts.get(repo)
            r["members"] = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None
            if repo in local_map:
                r["available"] = False

    _change(fn)


def record_available(repo: str) -> None:
    def fn(doc: dict, events: list[Transition]) -> None:
        r = _repo(doc, repo)
        if not r["available"]:
            r["available"] = True
            events.append((repo, "shared_with_you", "shared with this workspace, not cloned here"))

    _change(fn)


def record_fetch(repo: str, project: str, n: int) -> None:
    def fn(doc: dict, events: list[Transition]) -> None:
        r = _repo(doc, repo)
        r.update(project=project, last_fetch_at=_now(), fetched=r["fetched"] + n,
                 pending_github=False, last_error=None)
        events.append((repo, "fetched", f"{n} commit(s)"))

    _change(fn)


def record_synced(repo: str, project: str) -> None:
    def fn(doc: dict, _events: list[Transition]) -> None:
        _repo(doc, repo).update(project=project, pending_github=False, last_error=None)

    _change(fn)


def record_clone_started(repo: str) -> None:
    def fn(doc: dict, _events: list[Transition]) -> None:
        r = _repo(doc, repo)
        prev = r.get("clone") or {}
        r["clone"] = {"state": "cloning", "detail": "", "at": _now(),
                      "attempts": int(prev.get("attempts") or 0) + 1,
                      "had_token": False, "next_retry_at": None}

    _change(fn)


def record_clone_result(repo: str, state: str, detail: str = "", *,
                        project: str | None = None, had_token: bool = False,
                        next_retry_at: float | None = None) -> None:
    """`cloned` / `already` clear the clone field (the folder now exists and
    the normal scan takes over); the three failure states keep it, with the
    retry decision the poller computed."""
    def fn(doc: dict, events: list[Transition]) -> None:
        r = _repo(doc, repo)
        if project:
            r["project"] = project
        if state in ("cloned", "already"):
            r["clone"] = None
            r["available"] = False
            if state == "cloned":
                events.append((repo, "cloned", f"cloned into {project}"))
            return
        prev = r.get("clone") or {}
        r["clone"] = {"state": state, "detail": detail, "at": _now(),
                      "attempts": int(prev.get("attempts") or 0),
                      "had_token": had_token, "next_retry_at": next_retry_at}
        events.append((repo, "clone_failed", f"{state}: {detail}" if detail else state))

    _change(fn)


def record_repo_error(repo: str, project, err: str, pending_github: bool = False) -> None:
    def fn(doc: dict, events: list[Transition]) -> None:
        r = _repo(doc, repo)
        if project:
            r["project"] = project
        r["last_error"] = err
        r["pending_github"] = pending_github
        events.append((repo, "error", err))

    _change(fn)


# ── readers ──────────────────────────────────────────────────────────────────

def _document() -> dict:
    """The snapshot as ``state.json`` holds it: a fresh dict per call, the
    empty document when the file is absent or unreadable (the storage
    layer warned; it is never rewritten from here)."""
    doc, _ok = store.state_document().read()
    return doc


def member_repos() -> set[str]:
    return {repo for repo, r in _document()["repos"].items() if r.get("shared")}


def recent(limit: int = RECENT_LIMIT) -> list[dict]:
    """The newest ``limit`` transitions, oldest first, in the shape the
    Inbox feeder and the legacy view read: ``{at, repo, kind, detail,
    project}`` (``kind`` without the ``sharing.`` prefix)."""
    lines = store.events_log().tail(limit=max(1, int(limit)))
    out: list[dict] = []
    for line in reversed(lines):
        kind = str(line.get("type") or "")
        if kind.startswith(store.EVENT_PREFIX):
            kind = kind[len(store.EVENT_PREFIX):]
        out.append({"at": line.get("ts"), "repo": line.get("repo"), "kind": kind,
                    "detail": str(line.get("detail") or ""), "project": line.get("project")})
    return out


def snapshot() -> dict:
    """Everything, as a copy: the document's fields (``schema`` and
    ``updated_at`` left out) plus ``recent``."""
    snap = {k: v for k, v in _document().items() if k not in ("schema", "updated_at")}
    snap["recent"] = recent()
    return snap


def feed_view() -> dict:
    """Stable projection: changes only when something a person would care
    about changed. No timestamps, no counters."""
    doc = _document()
    repos = {
        repo: {"project": r["project"], "shared": r["shared"], "members": r.get("members"),
               "available": r["available"], "last_error": r["last_error"],
               "clone": ({"state": r["clone"]["state"], "detail": r["clone"]["detail"]}
                         if r.get("clone") else None)}
        for repo, r in sorted(doc["repos"].items())
    }
    latest = [{"repo": e["repo"], "kind": e["kind"], "detail": e["detail"]}
              for e in recent()[-FEED_RECENT_LIMIT:]]
    return {"cadence": doc["cadence"], "reason": doc["reason"],
            "repos": repos, "recent": latest}


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
        except Exception as exc:  # noqa: BLE001 - a listener must not break the loop
            log.warning("sharing: on_change listener failed: %s", exc)
    return True
