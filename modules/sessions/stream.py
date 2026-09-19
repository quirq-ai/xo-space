"""``GET /api/sessions/stream/events``: sessions as they start, across the Space.

One feed over every project's timeline and the Space log, narrowed to the
session lifecycle types (``events.TYPES``: ``session.started``,
``session.closed``), so the Agents tab can show sessions as they start.
Each line gains ``project``, the runtime key of the log it came from (the
pid, or the folder name of a project without one) or ``"space"``. ``since``
replays what landed after that stamp first; ``types`` may narrow the feed
further but never widen it past the lifecycle types (a type outside them is
400 ``invalid_value``).

The logs are the ones ``modules.timeline`` writes,
``projects/<pid>/timeline.jsonl`` and ``projects/timeline.jsonl``, resolved
from ``project_layout`` the way its store resolves them and tailed through
``services.storage.eventlog``: ``modules.timeline.service`` exposes reads,
not a follower, and a module reaches another only through its service.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent import project_layout
from services.errors import ServiceError
from services.storage.eventlog import EventLog, follow_many

from . import events

logger = logging.getLogger(__name__)

TIMELINE_FILE = "timeline.jsonl"
LIFECYCLE = frozenset(events.TYPES)


def _logs() -> dict[str, EventLog]:
    """``{runtime key: log}`` for every project folder under the runtime
    home holding a live timeline, plus ``"space"`` for the Space log."""
    out: dict[str, EventLog] = {}
    root: Path = project_layout.xo_runtime_root()
    entries: list[Path] = []
    if root.is_dir():
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            logger.warning("sessions: could not list %s: %s", root, exc)
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and (entry / TIMELINE_FILE).is_file():
            out[entry.name] = EventLog(entry / TIMELINE_FILE)
    out["space"] = EventLog(project_layout.workspace_timeline_path())
    return out


def check_types(types: Optional[Iterable[str]]) -> frozenset[str]:
    """The lifecycle types a feed follows: all of them, or the subset the
    caller named. A name outside them is refused (400)."""
    if types is None:
        return LIFECYCLE
    names = {str(t).strip() for t in types if str(t).strip()}
    if not names:
        return LIFECYCLE
    unknown = names - LIFECYCLE
    if unknown:
        raise ServiceError("invalid_value",
                           f"unknown session lifecycle type(s): {sorted(unknown)}; expected {sorted(LIFECYCLE)}")
    return frozenset(names)


def follow(since=None, types=None):
    return follow_many(_logs(), since=since, types=check_types(types), tag="project")


STREAMS = {"events": follow}
