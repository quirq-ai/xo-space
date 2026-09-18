"""Hermes visualizer source — polls every hermes profile's SQLite
state.db and emits message-level events into the watcher.

Polling (not JSONL tail) because hermes writes to a WAL SQLite DB,
not a transcript file. We open each profile's state.db read-only
per tick, query messages we haven't seen, emit events.

A session is enriched only if some project's ``sessionslist.json``
row references it (written by ``adapters/hermes/sessionslist.py``
at chat-done time). Sessions hermes owns but no project references
are skipped — same rule the other sources follow.

Emitted: :class:`events.SessionFirstSeen`, :class:`events.MessageObserved`,
:class:`events.ToolUseObserved` and :class:`events.UsageObserved`.

Hermes keeps no usage per message (``messages.token_count`` stays empty):
the ``sessions`` row carries the main loop's running totals (input without
cache, output with reasoning inside it, cache read, cache write), the same
row ``/api/usage`` and Space's session telemetry read. Each tick compares a
mapped session's totals with the ones last recorded and emits one
``UsageObserved`` for the increase, stamped with the session's last activity.
A total that went down (a rewound or reset session) becomes the new baseline
and emits nothing, so a count is never subtracted or repeated.

Per-``(profile, session_id)`` offsets persisted to
``watcher_state_dir() / "hermes-offsets.json"`` so restarts don't
replay: the last message id under ``<profile>:<session_id>``, and the last
recorded usage totals under ``<profile>:<session_id>#<column>``. The shared
``OffsetStore`` is byte-offset shaped so we own our own state file.

``poll_presence`` returns ``[]``; hermes has no per-process state
file analogous to claude's ``~/.claude/sessions/<pid>.json``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.adapters.hermes.paths import HERMES_DIR, HERMES_PROFILES_DIR
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.state import (
    legacy_watcher_state_dir,
    watcher_state_dir,
)

logger = logging.getLogger(__name__)


_OFFSETS_FILE = watcher_state_dir() / "hermes-offsets.json"
_LEGACY_OFFSETS_FILE = legacy_watcher_state_dir() / "hermes-offsets.json"
_DEFAULT_PROFILE = "default"
_BATCH = 500  # messages per session per tick — safety cap on a single query

# ``sessions`` usage column → the UsageObserved field it feeds.
_USAGE_COLUMNS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


class Source:
    """Visualizer source for the Hermes backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = "hermes"

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        # The shared OffsetStore is byte-offset shaped and not used by
        # this source. Accept it for interface symmetry only.
        del offsets
        self._sessions_seen: set[str] = set()
        self._offsets: dict[str, int] = _load_offsets()

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        """One tick. Yields events from every project-mapped hermes
        session across every profile DB. Persists offsets at the end.
        """
        # 1. Build the project map: hermes session_id → project_id.
        session_to_project = _build_session_to_project_map(self.name)
        if not session_to_project:
            return  # no projects own any hermes session — nothing to do

        # 2. Walk every profile DB and emit events for new rows.
        any_offset_changed = False
        for profile, db_path in _profile_state_dbs():
            try:
                yielded = yield from self._poll_profile(
                    profile, db_path, session_to_project
                )
            except sqlite3.Error as exc:
                logger.warning("hermes source: db error on %s: %s", db_path, exc)
                continue
            any_offset_changed = any_offset_changed or yielded

        # 3. Persist offsets so restarts don't replay.
        if any_offset_changed:
            try:
                _save_offsets(self._offsets)
            except OSError as exc:
                logger.warning("hermes source: offset save failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # Hermes has no per-process state file; no live-presence signal.
        return []

    # ── Per-profile pipeline ────────────────────────────────────────────

    def _poll_profile(
        self,
        profile: str,
        db_path: Path,
        session_to_project: dict[str, str],
    ) -> Iterator[Event]:
        """Emit events for every new message in every project-mapped
        session this profile owns. Yields events; returns True if any
        offset advanced (so the caller knows whether to flush)."""
        if not db_path.is_file():
            return False

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        any_change = False
        try:
            # Pre-fetch model and usage totals for every session we care
            # about so we don't issue one SELECT per row.
            session_rows = _fetch_sessions(conn, list(session_to_project))

            for hermes_sid, project_id in session_to_project.items():
                offset_key = f"{profile}:{hermes_sid}"
                last_id = self._offsets.get(offset_key, 0)

                try:
                    rows = conn.execute(
                        """
                        SELECT id, role, content, tool_calls, timestamp
                        FROM messages
                        WHERE session_id = ? AND id > ?
                        ORDER BY id
                        LIMIT ?
                        """,
                        (hermes_sid, last_id, _BATCH),
                    ).fetchall()
                except sqlite3.Error as exc:
                    logger.warning(
                        "hermes source: query failed for session %s in profile %s: %s",
                        hermes_sid, profile, exc,
                    )
                    continue

                session = session_rows.get(hermes_sid) or {}
                session_model = session.get("model")
                if rows:
                    for row in rows:
                        yield from self._events_from_row(
                            row, hermes_sid=hermes_sid, project_id=project_id,
                            session_model=session_model,
                        )
                    self._offsets[offset_key] = int(rows[-1]["id"])
                    any_change = True

                # Usage is committed with the session row, not per message,
                # so it is checked whether or not new messages arrived.
                usage = self._usage_event(
                    session, offset_key=offset_key, hermes_sid=hermes_sid,
                    project_id=project_id,
                )
                if usage is not None:
                    yield usage
                    any_change = True
                elif session.get("usage_moved"):
                    any_change = True
        finally:
            conn.close()
        return any_change

    def _usage_event(
        self,
        session: dict,
        *,
        offset_key: str,
        hermes_sid: str,
        project_id: str,
    ) -> Optional[UsageObserved]:
        """The increase in a session's usage totals since the last tick, as
        one event; ``None`` when nothing grew. Records the new totals."""
        totals = session.get("usage")
        if not totals:
            return None
        delta: dict[str, int] = {}
        for column, value in totals.items():
            key = f"{offset_key}#{column}"
            last = self._offsets.get(key, 0)
            if value != last:
                session["usage_moved"] = True
                self._offsets[key] = value
            # A lower total is a reset, not negative usage: it is only the
            # new baseline.
            if value > last:
                delta[_USAGE_COLUMNS[column]] = value - last
        if not delta:
            return None
        return UsageObserved(
            ts=_epoch_to_iso(session.get("last_activity_at")),
            native_session_id=hermes_sid,
            runtime=self.name,
            project_id=project_id,
            model=session.get("model"),
            **delta,
        )

    def _events_from_row(
        self,
        row: sqlite3.Row,
        *,
        hermes_sid: str,
        project_id: str,
        session_model: Optional[str],
    ) -> Iterator[Event]:
        """Convert one ``messages`` row into 0+ events."""
        role = row["role"]
        if role == "session_meta":
            return

        ts_iso = _epoch_to_iso(row["timestamp"])

        # SessionFirstSeen — exactly once per hermes session id.
        if hermes_sid not in self._sessions_seen:
            self._sessions_seen.add(hermes_sid)
            yield SessionFirstSeen(
                ts=ts_iso,
                native_session_id=hermes_sid,
                runtime=self.name,
                project_id=project_id,
                cwd="",  # hermes doesn't surface a cwd per message
            )

        if role in ("user", "assistant"):
            yield MessageObserved(
                ts=ts_iso,
                native_session_id=hermes_sid,
                runtime=self.name,
                project_id=project_id,
                role=role,
                model=session_model if role == "assistant" else None,
            )
        elif role == "tool":
            # tool result rows: count as a message so messageCount reflects
            # actual turn-take volume, matching openclaw/claude_code semantics.
            yield MessageObserved(
                ts=ts_iso,
                native_session_id=hermes_sid,
                runtime=self.name,
                project_id=project_id,
                role="assistant",  # tool results sit on the assistant side of the conversation
                model=session_model,
            )

        # Tool calls live as a JSON-encoded list in the assistant row.
        if role == "assistant" and row["tool_calls"]:
            for name in _tool_names_from_json(row["tool_calls"]):
                yield ToolUseObserved(
                    ts=ts_iso,
                    native_session_id=hermes_sid,
                    runtime=self.name,
                    project_id=project_id,
                    tool=name,
                )


# ── Helpers ──────────────────────────────────────────────────────────────


def _profile_state_dbs() -> list[tuple[str, Path]]:
    """Mirror of :func:`state_db._profile_state_dbs` — kept local
    to avoid making the visualizer source depend on the chat-side
    module's surface."""
    out: list[tuple[str, Path]] = []
    default_db = HERMES_DIR / "state.db"
    if default_db.is_file():
        out.append((_DEFAULT_PROFILE, default_db))
    if HERMES_PROFILES_DIR.is_dir():
        for entry in sorted(p for p in HERMES_PROFILES_DIR.iterdir() if p.is_dir()):
            db = entry / "state.db"
            if db.is_file():
                out.append((entry.name, db))
    return out


def _fetch_sessions(
    conn: sqlite3.Connection, session_ids: list[str]
) -> dict[str, dict]:
    """One query per chunk for every relevant session's model, last
    activity and usage totals. SQLite caps the IN list around 1000 — we
    batch in chunks of 500 to be safe. A hermes build whose ``sessions``
    table lacks a usage column yields no ``usage`` (and so no usage
    events) instead of failing the tick."""
    if not session_ids:
        return {}
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
    except sqlite3.Error:
        return {}
    usage_columns = [c for c in _USAGE_COLUMNS if c in columns]
    if len(usage_columns) != len(_USAGE_COLUMNS):
        usage_columns = []
    selected = ["id", "model", *usage_columns]
    if "last_activity_at" in columns:
        selected.append("last_activity_at")
    out: dict[str, dict] = {}
    for i in range(0, len(session_ids), 500):
        chunk = session_ids[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT {', '.join(selected)} FROM sessions WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            out[row["id"]] = {
                "model": str(row["model"]) if row["model"] else None,
                "last_activity_at": row["last_activity_at"] if "last_activity_at" in selected else None,
                "usage": {
                    column: max(0, int(row[column] or 0)) for column in usage_columns
                },
            }
    return out


def _build_session_to_project_map(backend: str) -> dict[str, str]:
    """Reverse-index sessionslist rows: ``{native_session_id:
    project_id}`` for every row whose backend matches. Empty dict if
    no project has any matching rows yet.

    Row enumeration is shared (see
    :mod:`services.cowork_agent.visualizer.discovery`); this function
    is the Hermes-specific shape — the JSONL-tail sources project the
    same iterator to ``(project_id, jsonl_path)`` pairs, the SQLite
    source projects to a reverse lookup."""
    out: dict[str, str] = {}
    for project_id, _composite_key, row in iter_sessionslist_rows(backend):
        native = row.get("nativeSessionId")
        if isinstance(native, str) and native:
            out[native] = project_id
    return out


def _tool_names_from_json(tool_calls_raw: object) -> Iterator[str]:
    """Decode the hermes ``tool_calls`` JSON column into a stream of
    tool names. Mirrors the parse in
    ``state_db._row_to_openclaw_record`` — same defensive
    shape handling so a malformed entry never raises."""
    if not isinstance(tool_calls_raw, str):
        return
    try:
        decoded = json.loads(tool_calls_raw)
    except (TypeError, ValueError):
        return
    if not isinstance(decoded, list):
        return
    for tc in decoded:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name") if fn else tc.get("name")
        if isinstance(name, str) and name:
            yield name


def _epoch_to_iso(ts: object) -> str:
    """Float epoch → ISO-8601 UTC, matching the format the events expect.
    Falls back to *now* on garbage input so the watcher never crashes
    on a single bad row."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _load_offsets() -> dict[str, int]:
    """Load the persisted per-session offsets. Missing/corrupt file →
    empty dict (we'll re-emit everything on next tick — events are
    idempotent at the sink level by ``(session_id, ts)`` keys)."""
    source_path = _OFFSETS_FILE if _OFFSETS_FILE.is_file() else _LEGACY_OFFSETS_FILE
    if not source_path.is_file():
        return {}
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, int):
            out[k] = v
    if source_path == _LEGACY_OFFSETS_FILE:
        try:
            _save_offsets(out)
        except OSError:
            pass
    return out


def _save_offsets(offsets: dict[str, int]) -> None:
    """Atomic write so a mid-write crash doesn't corrupt the file."""
    _OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OFFSETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets, separators=(",", ":")), encoding="utf-8")
    tmp.replace(_OFFSETS_FILE)
