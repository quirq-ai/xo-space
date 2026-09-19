"""The timeline sink: renders watcher events to timeline lines and hands
them to ``modules.timeline.service.emit``, which owns the logs.

A line about a project is written once, to ``projects/<pid>/timeline.jsonl``
(the runtime home this sink is given is keyed by the pid, so the pid is its
folder name). The Space view is a merge at read time; nothing is copied.
Rotation is the ``EventLog``'s (8 MB, keep 5).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from modules.timeline import service as timeline_service
from services.cowork_agent.visualizer.ingest.events import (
    WORKITEM_ACTIONS,
    Event,
    FileTouched,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
    WorkitemEvent,
)

logger = logging.getLogger(__name__)


# The runtime home is ``~/.quirq/projects/<pid>/``, so its folder name is the
# pid. A project with no pid yet is keyed by its folder name and gets none:
# its lines carry no pid, and still land in its own log under that key.
_PID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _emit_event(ev: Event) -> Optional[dict]:
    """Translate one internal event to the timeline-schema vocab.

    Returns ``None`` for events that don't correspond to a timeline
    type; the caller skips them.
    """
    base = {
        "ts": ev.ts,
        "session_id": ev.native_session_id,
        "runtime": ev.runtime,
    }
    if isinstance(ev, SessionFirstSeen):
        return {**base, "type": "session.started"}
    if isinstance(ev, TaskCreated):
        return {
            **base,
            "type": "todo.added",
            "todo": {
                "id": ev.task_id,
                "content": ev.content,
                "status": "pending",
            },
        }
    if isinstance(ev, TaskStatusChanged):
        # ``todo.completed`` is kept as its own type rather than folded into
        # the generic one: it is the type readers, docs and the ``?types=``
        # filter already know, and completion is the transition worth naming.
        if ev.status == "completed":
            return {**base, "type": "todo.completed", "todo_id": ev.task_id}
        return {
            **base,
            "type": "todo.status_changed",
            "todo_id": ev.task_id,
            "status": ev.status,
        }
    if isinstance(ev, FileTouched):
        return {
            **base,
            "type": "file.created" if ev.created else "file.edited",
            "path": ev.relative_path,
        }
    if isinstance(ev, WorkitemEvent):
        return _emit_workitem(ev)
    return None


def _emit_workitem(ev: WorkitemEvent) -> Optional[dict]:
    """Render one ``workitem.*`` line, or ``None`` if it must not exist."""
    if ev.action not in WORKITEM_ACTIONS or not ev.workitem_id:
        logger.warning(
            "dropping an unrenderable workitem event (action=%r id=%r): the "
            "timeline schema declares no branch for it",
            ev.action, ev.workitem_id,
        )
        return None

    line: dict = {"ts": ev.ts, "type": f"workitem.{ev.action}"}
    if ev.native_session_id:
        line["session_id"] = ev.native_session_id
    if ev.runtime:
        line["runtime"] = ev.runtime
    line["workitem_id"] = ev.workitem_id

    if ev.title is not None:
        line["title"] = ev.title
    if ev.kind is not None:
        line["kind"] = ev.kind
    if ev.repo and isinstance(ev.number, int):
        # Nested, because the pair only means anything together: a repo with no
        # number names no issue, and a number with no repo names somebody
        # else's.
        line["issue"] = {"repo": ev.repo, "number": ev.number}
    if ev.action == "assigned":
        # Always present, ``null`` included: clearing an assignee is half of
        # what this event exists to record, and an absent key would be
        # indistinguishable from an event that forgot to say.
        line["assignee"] = ev.assignee
    if ev.action == "closed" and ev.state_reason is not None:
        line["state_reason"] = ev.state_reason
    return line


def apply(root: Path, events: Iterable[Event], *, project_id: Optional[str] = None) -> list[dict]:
    """Render this project's events and write them, once each, through the
    timeline module.

    ``root`` is the project's runtime home; its folder name is the pid the
    lines are stamped with and filed under. A home keyed by a folder name
    (a project with no pid yet) files its lines under that name with no
    pid stamped. ``project_id`` (the project's folder name) is stamped when
    given. Returns the lines written, in envelope order.
    """
    pid = root.name if _PID_RE.fullmatch(root.name) else None
    lines: list[dict] = []
    for ev in events:
        rendered = _emit_event(ev)
        if rendered is not None:
            lines.append(rendered)
    if not lines:
        return []
    if pid is not None:
        return timeline_service.emit(lines, project_id=project_id, pid=pid)
    return timeline_service.emit(lines, project_id=project_id, key=root.name)


def apply_quiet(
    root: Optional[Path], events: Iterable[Event], *, project_id: Optional[str] = None,
) -> list[dict]:
    """:func:`apply`, for a caller whose write has already succeeded."""
    if root is None:
        return []
    try:
        return apply(root, events, project_id=project_id)
    except Exception:  # noqa: BLE001 - see the docstring; never fail the write
        logger.warning("timeline append failed for %s", root, exc_info=True)
        return []
