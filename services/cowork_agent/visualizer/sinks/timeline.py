"""``timeline.jsonl`` sink — append-only event log with rotation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent.visualizer.atomic_write import append_jsonl
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


_TIMELINE_FILE = Path("timeline.jsonl")
_ROTATE_BYTES = 8 * 1024 * 1024  # 8 MB
_MAX_ROTATIONS_KEEP = 5


def _emit_event(ev: Event) -> Optional[dict]:
    """Translate one internal event to the timeline-schema vocab.

    Returns ``None`` for events that don't correspond to a timeline
    type — caller skips them.
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


def _rotate_if_needed(root: Path) -> None:
    path = root / _TIMELINE_FILE
    if not path.is_file():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _ROTATE_BYTES:
        return

    # Atomic rename to a timestamped rotation.
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_name(f"timeline.{stamp}.jsonl")
    try:
        path.rename(rotated)
    except OSError as exc:
        logger.warning("timeline rotate failed: %s", exc)
        return

    # Prune older rotations.
    rotations = sorted(path.parent.glob("timeline.*.jsonl"))
    for old in rotations[:-_MAX_ROTATIONS_KEEP]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("timeline rotation prune failed for %s: %s", old, exc)


def apply(root: Path, events: Iterable[Event]) -> list[dict]:
    """Append timeline events for this project's events."""
    _rotate_if_needed(root)

    lines: list[dict] = []
    for ev in events:
        rendered = _emit_event(ev)
        if rendered is not None:
            lines.append(rendered)

    if not lines:
        return []

    append_jsonl(root / _TIMELINE_FILE, lines)
    return lines


def apply_quiet(root: Optional[Path], events: Iterable[Event]) -> list[dict]:
    """:func:`apply`, for a caller whose write has already succeeded."""
    if root is None:
        return []
    try:
        return apply(root, events)
    except Exception:  # noqa: BLE001 - see the docstring; never fail the write
        logger.warning("timeline append failed for %s", root, exc_info=True)
        return []
