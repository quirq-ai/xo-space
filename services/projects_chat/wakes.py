"""Each agent session's queue of things to tell it on its next turn.

A wake is an envelope (``<wake>`` or ``<relay>`` text from ``envelope.py``)
waiting for its session. Wakes are files, so a restart loses none:
``<session dir>/inbox/<priority>-<ns>.json``, taken in priority order
(``now``, ``next``, ``later``) and then oldest first. Taking a wake deletes
its file and appends a ``wake.delivered`` line to the session's
``events.jsonl``.

The same text queued twice for one session within 30 s is dropped (a
duplicate delivery). Listeners registered with :func:`on_enqueue` are called
after every enqueue; the turn engine uses that to start a turn without
polling.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from services.projects_chat import store
from services.storage.atomic_write import create_json_exclusive

log = logging.getLogger(__name__)

PRIORITIES = {"now": 0, "next": 1, "later": 2}
DEDUPE_WINDOW_S = 30.0

_listeners: list[Callable[[Optional[str], str], None]] = []


def on_enqueue(listener: Callable[[Optional[str], str], None]) -> None:
    """Call ``listener(project, session_id)`` after every enqueue. Idempotent."""
    if listener not in _listeners:
        _listeners.append(listener)


def _inbox(project: Optional[str], session_id: str) -> Path:
    return store._session_dir(project, session_id) / "inbox"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def enqueue(project: Optional[str], session_id: str, *, reason: str, envelope: str,
            priority: str = "next", meta: Optional[dict] = None) -> Optional[dict]:
    """Queue one wake for a session. Returns it, or ``None`` when it was a duplicate."""
    if priority not in PRIORITIES:
        raise store.StoreError("invalid_priority", f"priority must be one of {', '.join(PRIORITIES)}.")
    if store.get_session(project, session_id) is None:
        raise store.StoreError("no_such_session", f"There is no session {session_id!r}.", status=404)
    folder = _inbox(project, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    digest = _digest(envelope)
    now = time.time()
    for existing in pending(project, session_id):
        if existing.get("sha256") == digest and now - existing.get("created_unix", 0) < DEDUPE_WINDOW_S:
            log.info("projects chat: dropped a duplicate wake for %s", session_id)
            return None
    ns = time.time_ns()
    wake = {
        "schema": 1,
        "id": f"wake_{ns}",
        "session_id": session_id,
        "reason": reason,
        "priority": priority,
        "envelope": envelope,
        "sha256": digest,
        "meta": meta or {},
        "created_at": store.now_ms_iso(),
        "created_unix": now,
    }
    path = folder / f"{PRIORITIES[priority]}-{ns}.json"
    while not create_json_exclusive(path, wake):
        ns += 1
        path = folder / f"{PRIORITIES[priority]}-{ns}.json"
        wake["id"] = f"wake_{ns}"
    for listener in list(_listeners):
        try:
            listener(project, session_id)
        except Exception:  # noqa: BLE001 - a listener must never lose the wake
            log.exception("projects chat: a wake listener failed")
    return wake


def pending(project: Optional[str], session_id: str) -> list[dict]:
    """Waiting wakes, in the order they will be delivered."""
    folder = _inbox(project, session_id)
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            wake = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("projects chat: unreadable wake %s left in place", path)
            continue
        if isinstance(wake, dict):
            wake["_path"] = str(path)
            out.append(wake)
    return out


def take(project: Optional[str], session_id: str, *, limit: Optional[int] = None) -> list[dict]:
    """Remove and return the next wakes (all, or ``limit``), marking them delivered."""
    taken = []
    for wake in pending(project, session_id)[:limit]:
        path = Path(wake.pop("_path"))
        try:
            path.unlink()
        except FileNotFoundError:
            continue  # another taker got it
        taken.append(wake)
        store.append_session_event(project, session_id, "wake.delivered", wake_id=wake["id"],
                                   reason=wake.get("reason"), priority=wake.get("priority"))
    return taken


def sessions_with_pending() -> list[tuple[Optional[str], str]]:
    """Every ``(project, session_id)`` with a waiting wake (boot recovery)."""
    out = []
    for project in store._candidate_projects():
        for session in store.list_sessions(project):
            if session.get("retired_at") is None and pending(project, session["id"]):
                out.append((project, session["id"]))
    return out
