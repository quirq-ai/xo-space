"""``sessions/sessions-augment.json`` sink — watcher-owned per-session counters."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
    ToolUseObserved,
)
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.todo_status import (
    TODO_STATUSES,
    VALID_TODO_STATUSES,
)


_AUGMENT_FILE = Path("sessions/sessions-augment.json")


def _iso_to_ms(ts: str) -> Optional[int]:
    """Best-effort ISO-8601 → epoch ms. Returns ``None`` on parse
    failure (the field then stays absent on the row)."""
    try:
        return int(
            datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        )
    except (ValueError, AttributeError):
        return None


def _now_iso() -> str:
    # Same string as before; ``utcnow()`` is deprecated and this sink is now
    # called from a request thread too, where the warning is noise.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_native_to_composite_map(sessionslist: Optional[dict]) -> dict[str, str]:
    """Map every adapter row's ``nativeSessionId`` to its composite
    outer key. Used so augment rows can use the same key shape the
    BFF merge expects.
    """
    out: dict[str, str] = {}
    if not isinstance(sessionslist, dict):
        return out
    for composite, row in sessionslist.items():
        if not isinstance(row, dict):
            continue
        native = row.get("nativeSessionId")
        if isinstance(native, str) and native:
            out[native] = composite
    return out


def _empty_row() -> dict:
    return {
        "messageCount":   0,
        "messageCountByRole": {"user": 0, "assistant": 0,
                               "toolResults": 0, "errors": 0},
        "toolCallCount":  0,
        # One counter per status, built from the shared vocabulary so a new
        # status cannot arrive without a bucket to land in.
        "taskCount":      {"total": 0, **{st: 0 for st in TODO_STATUSES}},
        "firstActivity":  None,
        "lastActivity":   None,
        "ended_at":       None,
        "episode_refs":   [],
    }


def _stamp_activity(row: dict, ts: str) -> None:
    ms = _iso_to_ms(ts)
    if ms is None:
        return
    if row.get("firstActivity") is None or ms < row["firstActivity"]:
        row["firstActivity"] = ms
    if row.get("lastActivity") is None or ms > row["lastActivity"]:
        row["lastActivity"] = ms


# Status transitions we track.
_VALID_STATUSES = VALID_TODO_STATUSES


def apply(
    root: Path, events: Iterable[Event], *, legacy_root: Optional[Path] = None
) -> bool:
    """
    Apply ``events`` to this project's augment file. Returns ``True`` if the
    file changed (so the workspace tier knows to re-aggregate).
    """
    events = list(events)
    if not events:
        return False

    augment_path = root / _AUGMENT_FILE
    with locked(augment_path):
        return _apply_locked(root, augment_path, events, legacy_root=legacy_root)


def _apply_locked(
    root: Path,
    augment_path: Path,
    events: list,
    *,
    legacy_root: Optional[Path] = None,
) -> bool:
    """The read-modify-write itself. Caller holds the lock."""
    native_to_composite = _build_native_to_composite_map(
        session_index.read_session_index_at(root, legacy_root=legacy_root)
    )

    current = read_json(augment_path)
    if current is None and legacy_root is not None:
        current = read_json(legacy_root / _AUGMENT_FILE)
    current = current or {}
    sessions: dict = dict(current.get("sessions") or {})

    # Per-task last-known status, so a TaskCreated followed by
    # TaskStatusChanged correctly transitions counts.
    # Loaded lazily per (key, task_id) from the row.
    def _task_states(row: dict) -> dict[str, str]:
        st = row.setdefault("_task_states", {})
        if not isinstance(st, dict):
            st = {}
            row["_task_states"] = st
        return st

    changed = False

    for ev in events:
        nsid = ev.native_session_id
        if not nsid:
            continue
        key = native_to_composite.get(nsid, nsid)
        row = sessions.get(key)
        if row is None or not isinstance(row, dict):
            row = _empty_row()
            sessions[key] = row
            changed = True

        # Preserve adapter-written timing if it ever bleeds in (defensive).
        _stamp_activity(row, ev.ts)

        if isinstance(ev, MessageObserved):
            row["messageCount"] = int(row.get("messageCount", 0)) + 1
            # Per-role split. Legacy schema-1 rows on disk won't have
            # the sub-dict — create it lazily so an upgrade doesn't
            # lose existing messageCount.
            by_role = row.get("messageCountByRole")
            if not isinstance(by_role, dict):
                by_role = {"user": 0, "assistant": 0, "toolResults": 0, "errors": 0}
                row["messageCountByRole"] = by_role
            role_key = ev.role if ev.role in ("user", "assistant") else "assistant"
            by_role[role_key] = int(by_role.get(role_key, 0)) + 1
            changed = True
        elif isinstance(ev, ToolUseObserved):
            row["toolCallCount"] = int(row.get("toolCallCount", 0)) + 1
            changed = True
        elif isinstance(ev, TaskCreated):
            tc = row["taskCount"]
            tc["total"] += 1
            tc["pending"] = tc.get("pending", 0) + 1
            _task_states(row)[ev.task_id] = "pending"
            changed = True
        elif isinstance(ev, TaskStatusChanged):
            if ev.status not in _VALID_STATUSES:
                continue
            states = _task_states(row)
            prev = states.get(ev.task_id)
            tc = row["taskCount"]
            if prev and prev in tc:
                tc[prev] = max(0, tc[prev] - 1)
            tc[ev.status] = tc.get(ev.status, 0) + 1
            states[ev.task_id] = ev.status
            changed = True
        elif isinstance(ev, (FileTouched, SessionFirstSeen)):
            # Updates the timestamps but no counter — already stamped above.
            pass

    if not changed:
        return False

    # ``_task_states`` is private state — the prev-status map needed
    # to decrement ``taskCount`` correctly across restarts. It IS
    # persisted (we'd lose count integrity otherwise). The BFF
    # routes never serialise it: ``reader.merge_session_record``
    # treats it as a watcher-only field, and the Pydantic models on
    # the wire pick named fields rather than spreading the dict —
    # see ``routers/cowork_agent/bff/visualizer.py::_row_to_list_item``.
    write_json_atomic(augment_path, {
        "schema": 2,
        "updated_at": _now_iso(),
        "sessions": sessions,
    })
    return True


def forget_task(
    root: Path,
    *,
    native_session_id: str,
    task_id: str,
    ts: Optional[str] = None,
    legacy_root: Optional[Path] = None,
) -> bool:
    """Drop one task from the counters — its todo was deleted."""
    augment_path = root / _AUGMENT_FILE
    with locked(augment_path):
        current = read_json(augment_path)
        if current is None and legacy_root is not None:
            current = read_json(legacy_root / _AUGMENT_FILE)
        current = current or {}
        sessions: dict = dict(current.get("sessions") or {})
        if not sessions:
            return False

        native_to_composite = _build_native_to_composite_map(
            session_index.read_session_index_at(root, legacy_root=legacy_root)
        )
        key = native_to_composite.get(native_session_id, native_session_id)
        row = sessions.get(key)
        if not isinstance(row, dict):
            return False

        states = row.get("_task_states")
        if not isinstance(states, dict) or task_id not in states:
            return False
        previous = states.pop(task_id)

        tc = row.get("taskCount")
        if isinstance(tc, dict):
            if previous in tc:
                tc[previous] = max(0, int(tc.get(previous, 0)) - 1)
            tc["total"] = max(0, int(tc.get("total", 0)) - 1)

        if ts:
            _stamp_activity(row, ts)

        write_json_atomic(augment_path, {
            "schema": 2,
            "updated_at": _now_iso(),
            "sessions": sessions,
        })
        return True
