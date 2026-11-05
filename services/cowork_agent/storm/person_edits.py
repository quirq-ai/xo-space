"""Telling an agent what the person changed in its folder (owner's choice B,
5 Nov 2026).

The person can edit a project's files in XO's Files view; Space keeps the
edit log (``services.project_files.edits``) and saves the files. Storm never
writes a file for the person: it only reads that log, so its agents hear
about an edit whatever starts their next turn (the person's message, a
coordinator's relay, a decision, a wake or a schedule) and from whichever
device the edit came.

* ``PersonEdits`` is the engine's ``TurnNote``: at the top of a new turn's
  input, a ``<system-note>`` naming the files the person changed in the
  session's folder since its previous turn took its input, so the agent
  reads them again before changing them. Coordinators don't get it: they
  only read and delegate, and their folder is the person's own.
* The integrity watch asks ``written_by_person`` before it reports a change
  to an agent's config: a change the person made and confirmed in Files is
  theirs, not the session's.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from services.cowork_agent.adapters.contract import SessionSpec
from services.cowork_agent.storm.chats.envelope import escape_text
from services.project_files.edits import edits_since
from services.timestamps import parse_ts

if TYPE_CHECKING:
    from services.cowork_agent.storm.engine import Engine
    from services.cowork_agent.storm.engine.session import Meta

#: How many files a note names; the rest are counted.
NAMED = 20
#: The roles told (a job run is a thread).
TOLD_ROLES = ("plain", "thread", "job")


def note_text(found: Sequence[dict[str, Any]]) -> str | None:
    """The note for these edits (``edits_since``'s entries), escaped."""
    if not found:
        return None
    named = []
    for entry in found[:NAMED]:
        what = "removed" if not entry.get("exists", True) else "created" if entry.get("kind") == "create" else \
            f"{entry['edits']} edits" if entry.get("edits", 1) > 1 else "edited"
        named.append(f"{entry['path']} ({what}, last at {entry.get('last_at')})")
    more = f", and {len(found) - NAMED} more" if len(found) > NAMED else ""
    text = ("The person changed these files in XO's Files view since your last turn; what is on disk is their "
            f"version, so read a file again before you change it: {'; '.join(named)}{more}.")
    return f"<system-note>{escape_text(text)}</system-note>"


class PersonEdits:
    """``ports.TurnNote``: the person's edits since the session's last turn.

    The bound is the moment the previous turn's note was gathered, kept per
    session and moved only once that turn's input reached the agent
    (``delivered``): an edit is told once and never missed, whether it came
    while a turn ran, while input was added to it, or while a note was
    being put together (the log is read under its lock, and an edit's time
    is taken under the same lock). After a restart the memory is gone, so
    the bound is the last turn's start less ``RESTART_MARGIN``: an edit
    may then be told twice, never missed."""

    RESTART_MARGIN = timedelta(seconds=10)

    def __init__(self) -> None:
        self._told: dict[str, datetime] = {}
        self._pending: dict[str, datetime] = {}

    async def note(self, engine: "Engine", meta: "Meta", spec: SessionSpec, since: str) -> str | None:
        if meta.role not in TOLD_ROLES or not spec.cwd:
            return None
        bound = self._told.get(meta.id)
        if bound is None:
            started = parse_ts(since)
            if started is None:
                return None
            bound = started - self.RESTART_MARGIN
        gathered = datetime.now(timezone.utc)
        found = await asyncio.to_thread(edits_since, Path(spec.cwd), bound)
        self._pending[meta.id] = gathered
        return note_text(found)

    def delivered(self, meta: "Meta") -> None:
        if (gathered := self._pending.pop(meta.id, None)) is not None:
            self._told[meta.id] = gathered
