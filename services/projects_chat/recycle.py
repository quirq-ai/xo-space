"""Keeping a coordinator usable forever: retire an incarnation, start a fresh one with a handoff.

A coordinator's transcript grows with every turn, and so does the cost of the
next one. Rather than compacting one session forever, the coordinator is
replaced, between turns, when one of these is true (each one an env knob):

- it has taken ``XO_COORDINATOR_MAX_TURNS`` turns (default 30);
- it is older than ``XO_COORDINATOR_MAX_AGE_H`` hours (default 24);
- its last turn's context passed ``XO_COORDINATOR_MAX_CONTEXT_TOKENS``
  (default 120,000: input, cache reads and cache writes together);
- its role's prompt or settings changed since it started (the backend's
  fingerprint), since a running session keeps the prompt it started with.

The successor is a new session (a new incarnation number) that never greets
and starts from a ``<handoff>``: the chat, its memory index, its threads
with their cards, and the recent timeline. The conversation is the store,
not any one session, so nothing the owner sees changes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from services.projects_chat import envelope, memory, store, wakes
from services.timestamps import parse_ts

log = logging.getLogger(__name__)

TIMELINE_IN_HANDOFF = 30
THREADS_IN_HANDOFF = 20


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def retire_reason(session: dict, fingerprint: Optional[str]) -> Optional[str]:
    """Why this coordinator should be replaced before its next turn, or ``None``."""
    if not session["role"].endswith("coordinator") or not session.get("turns"):
        return None
    if session.get("turns", 0) >= _num("XO_COORDINATOR_MAX_TURNS", 30):
        return "turns"
    started = parse_ts(session.get("created_at"))
    if started and (datetime.now(timezone.utc) - started).total_seconds() > _num("XO_COORDINATOR_MAX_AGE_H", 24) * 3600:
        return "age"
    if (session.get("last_context_tokens") or 0) > _num("XO_COORDINATOR_MAX_CONTEXT_TOKENS", 120000):
        return "context"
    if fingerprint and session.get("prompt_fingerprint") and session["prompt_fingerprint"] != fingerprint:
        return "upgrade"
    return None


def build_handoff(project: Optional[str], predecessor: dict) -> str:
    chat = store.read_chat(project) or {"id": store.chat(project).id, "kind": store.chat(project).kind,
                                              "project_id": project}
    threads = store.list_threads(project)[:THREADS_IN_HANDOFF]
    open_needs = [f"{t.get('title')}: {(t.get('card') or {}).get('state')}" for t in threads
                  if (t.get("card") or {}).get("state") in ("needs_reply", "needs_approval", "failed")]
    index = memory.read(project)["content"] or None
    return envelope.handoff(predecessor=predecessor["id"], chat=chat,
                            instructions=(chat.get("settings") or {}).get("instructions"), memory_index=index,
                            threads=threads, timeline=store.read_messages(project, limit=TIMELINE_IN_HANDOFF),
                            pending=open_needs or None)


def replace(project: Optional[str], session: dict, reason: str) -> tuple[dict, str]:
    """Retire ``session`` and start its successor. Returns ``(successor, handoff text)``."""
    store.update_session(project, session["id"], retired_at=store.now_ms_iso(), retire_reason=reason,
                         status="retired")
    store.append_session_event(project, session["id"], "session.retired", reason=reason)
    successor = store.create_session(project, role=session["role"], cwd=session["cwd"],
                                     incarnation=int(session.get("incarnation") or 1) + 1)
    successor.pop("token", None)
    store.update_session(project, successor["id"], predecessor=session["id"])
    for wake in wakes.take(project, session["id"]):  # anything queued for the old one goes to its successor
        wakes.enqueue(project, successor["id"], reason=wake.get("reason") or "relay", envelope=wake["envelope"],
                      priority=wake.get("priority") or "next", meta=wake.get("meta"))
    handoff = build_handoff(project, session)
    store.append_session_event(project, successor["id"], "session.handoff", predecessor=session["id"],
                               reason=reason, chars=len(handoff))
    log.info("projects chat: %s retired (%s); %s takes over", session["id"], reason, successor["id"])
    return store.get_session(project, successor["id"]), handoff
