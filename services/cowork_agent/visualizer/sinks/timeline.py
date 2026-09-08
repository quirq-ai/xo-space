"""``timeline.jsonl`` sink — append-only event log with rotation.

Translates the watcher's internal event taxonomy into the timeline
schema's vocabulary (docs/watcher-design.md §3.8):

* :class:`SessionFirstSeen`  → ``session.started``
* :class:`TaskCreated`       → ``todo.added``
* :class:`TaskStatusChanged` (``completed``) → ``todo.completed``
* :class:`TaskStatusChanged` (anything else) → ``todo.status_changed``
* :class:`FileTouched` (created) → ``file.created``
* :class:`FileTouched` (not created) → ``file.edited``
* :class:`WorkitemEvent` → ``workitem.<action>`` for the eight actions
  in :data:`~...ingest.events.WORKITEM_ACTIONS`, and **nothing at all**
  for anything else (workitems-plan §8)

Other internal events (``MessageObserved``, ``UsageObserved``,
``ToolUseObserved``) don't map to any timeline type and are silently
dropped here. Those events live on as counters in
:mod:`sessions_augment` and aggregates in :mod:`stats`.

**Where todo events come from.** Not from ingestion any more: the
todos HTTP API is the single source
(:mod:`~services.cowork_agent.visualizer.todos_store`, syncplan §7 T7)
and the watcher drops task events before the fan-out, so a timeline
line exists exactly when the todo exists in ``todos.json`` — on every
backend, not just the one runtime whose transcript carried them. The
consequence for this module is that the API's request thread appends
here alongside the watcher tick; each append is a single write to an
``O_APPEND`` handle, so lines interleave but never tear.

Every status transition is carried. Until T7 only ``completed``
survived, so a todo moving to ``in_progress`` — the transition a
reader most wants to see live — left no trace at all.

**Where workitem events come from.** The same shape, on a second pair of
documents: ``workitems_store`` and ``workitem_claims`` are the only
writers of ``workitems.json`` / ``claims.json``, so they are the only
event source, and a workitem line exists exactly when the write that
caused it succeeded — whichever route reached the store. That takes this
file's appender count to four, which is workitems-plan §8's forcing
function for defect O-D: ``atomic_write.append_jsonl`` now states what
appending concurrently does and does not guarantee.

``workitem.claimed`` / ``.released`` are the load-bearing pair.
``in_progress`` is derived from a live claim and never stored (§5.4), so
those two lines are the only record that the state was ever held — "what
did this agent work on last Tuesday" becomes answerable from the log,
without a flag that could have been wrong.

Rotation: when ``timeline.jsonl`` exceeds 8 MB the sink renames it
to ``timeline.<UTC-iso>.jsonl`` and starts fresh. Older rotations
beyond 5 are deleted. The atomic rename means BFF readers either
see the old or the new file; never both half-written.
"""

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
        # ``todo.completed`` is kept as its own type rather than folded
        # into the generic one: it is the type readers, docs and the
        # ``?types=`` filter already know, and completion is the
        # transition worth naming.
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
    """Render one ``workitem.*`` line, or ``None`` if it must not exist.

    **The vocabulary is closed on purpose.** ``timeline.schema.json`` is
    a ``oneOf`` over declared branches, and defect R3a was this module
    emitting a type the schema had no branch for — the emitter widened,
    the schema lagged, and every such line failed its own validator. So
    an action outside :data:`WORKITEM_ACTIONS` is dropped with a warning
    instead of rendered: the emitter cannot outrun the schema, because
    the same frozenset gates it and is asserted against the schema's
    branches by ``tests/test_workitem_timeline.py``.

    An id-less event is dropped for the same reason — every branch
    requires ``workitem_id``, and a line naming no workitem is not an
    event about a workitem.

    ``session_id`` and ``runtime`` are written only when known. Most
    workitem transitions carry no session (the CRUD surface has none),
    and the schema declares ``session_id`` absent on project-wide
    events; inventing a placeholder would put a session id in the log
    that no session ever had.
    """
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
        # Nested, because the pair only means anything together: a repo
        # with no number names no issue, and a number with no repo names
        # somebody else's.
        line["issue"] = {"repo": ev.repo, "number": ev.number}
    if ev.action == "assigned":
        # Always present, ``null`` included: clearing an assignee is half
        # of what this event exists to record, and an absent key would be
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
    """Append timeline events for this project's events.

    ``root`` is the project's RUNTIME directory since the tier move — the
    timeline is derived history, not part of what a clone would want
    (syncplan §2, R-TIER).

    **There is no read-through to the pre-move file, deliberately** (open
    decision O3, default applied). The other moved documents are single
    JSON files a reader can fall back to; this one is append-only WITH
    rotation, so a read-through would have to reconcile the
    ``timeline.<stamp>.jsonl`` glob across two roots on every read and
    every prune, and get the interleaving right. Existing history stays
    on disk in ``.xo/`` until T21's migration removes it; the runtime
    timeline starts empty.

    Returns the list of rendered lines actually appended (empty when
    no event mapped to a schema-vocab type, or when called with an
    empty input). The watcher main loop passes the returned list to
    the workspace timeline sink so the workspace ``timeline.jsonl``
    stays a multiplexed view without re-rendering.

    Rotation is checked **before** the write so a tick that pushes us
    over 8 MB starts the next tick on a fresh file.
    """
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
    """:func:`apply`, for a caller whose write has already succeeded.

    **The timeline is a log, and a log must never fail the thing it is
    logging.** Every workitem event is emitted after its store write has
    committed, so by the time we get here the record is on disk and the
    caller is going to return success no matter what happens next. A
    disk-full, a permission fault or an unwritable runtime directory
    would otherwise turn a workitem that exists into a 500 — and, worse,
    a 500 for an operation the caller would then legitimately retry
    against a store that already applied it.

    So every failure is swallowed and logged. The cost of the swallow is
    exactly one missing line in an append-only history; the cost of not
    swallowing it is a lie about whether the write happened.

    ``root`` is allowed to be ``None`` — a project whose runtime home
    has not been minted (or has been deleted) has nowhere to put derived
    views, which is a skipped write everywhere else in this system too,
    not an error. Returns the lines appended, ``[]`` when nothing was.
    """
    if root is None:
        return []
    try:
        return apply(root, events)
    except Exception:  # noqa: BLE001 - see the docstring; never fail the write
        logger.warning("timeline append failed for %s", root, exc_info=True)
        return []
