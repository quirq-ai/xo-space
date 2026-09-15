"""OpenClaw visualizer source — reads the transcripts of the OpenClaw
sessions recorded in the projects' session indexes.

Loaded by :func:`services.cowork_agent.visualizer.source_loader.load_source_module`
when ``AGENT_NAME=openclaw``. The class name ``Source`` is the loader
contract.

Responsibilities (mirror of the claude_code source, minus the parts
that don't apply):

* Walk every workspace project's ``sessionslist`` rows whose
  ``backend == "openclaw"``; the OpenClaw agent is the session key's
  second segment (``agent:<agent>:web:<8hex>``).
* Current OpenClaw keeps transcripts in each agent's SQLite database
  (see ``agent_db``): poll new ``transcript_events`` rows for every
  generation of the session key, with per-generation ``seq`` offsets
  persisted to ``watcher_state_dir() / "openclaw-offsets.json"``. An
  agent with no database (an older gateway) is tailed from
  ``~/.openclaw/agents/<agent>/sessions/<sid>.jsonl`` via
  :mod:`ingest.jsonl_tail`. Both carry the same record shape.
* Normalise message records into watcher events.
* Emit a single :class:`events.SessionFirstSeen` per session id.

Not implemented (intentional):

* ``poll_presence`` returns ``[]`` — OpenClaw has no per-session pid
  file the way Claude does.
* ``FileTouched`` — OpenClaw's edit inputs aren't carried in the
  transcript in the shape the PII filter wants. Defer.
* Task pairing — OpenClaw has no ``TaskCreate`` tool. Todos for
  OpenClaw flow through the ``POST /api/xo-projects/{id}/todos``
  HTTP endpoint, unaffected by the watcher.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Generator, Iterator, Optional

from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
    compute_latency_ms,
)
from services.cowork_agent.visualizer.state import watcher_state_dir

logger = logging.getLogger(__name__)


_OFFSETS_FILE = watcher_state_dir() / "openclaw-offsets.json"
_BATCH = 500  # transcript rows per generation per tick


class Source:
    """Visualizer source for the OpenClaw backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = "openclaw"

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        self._sessions_seen: set[str] = set()
        # Per-file session id (header row holds it once; lines that
        # follow don't carry it). Keyed by absolute jsonl path so a
        # second pass on a different file doesn't clobber the cache.
        self._sid_by_path: dict[str, str] = {}
        # native_session_id → ts of the last MessageObserved(role="user").
        # Used to attach latency_ms on the matching UsageObserved
        # Mirrors the claude_code source.
        self._last_user_ts: dict[str, str] = {}
        # "<agent>/<generation>" → last transcript_events seq emitted.
        self._db_offsets: dict[str, int] = _load_db_offsets()

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        db_offsets_changed = False
        for project_id, agent, session_key, native in self._discover_sessions():
            if agent_db.has_database(agent):
                changed = yield from self._poll_database(project_id, agent, session_key, native)
                db_offsets_changed = db_offsets_changed or changed
                continue
            jsonl = agent_db.legacy_sessions_dir(agent) / f"{native}.jsonl"
            if jsonl.is_file():
                yield from self._tail_one(project_id, jsonl)
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("OpenClaw source: offset flush failed: %s", exc)
        if db_offsets_changed:
            try:
                _save_db_offsets(self._db_offsets)
            except OSError as exc:
                logger.warning("OpenClaw source: offset save failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # MVP: no per-session pid file. Honest signal that we don't
        # know who's active under OpenClaw. Revisit if/when we wire
        # the gateway's open-sessions list in.
        return []

    # ── Discovery ───────────────────────────────────────────────────────

    def _discover_sessions(self) -> Iterator[tuple[str, str, str, str]]:
        """Yield ``(project_id, agent, session_key, native_session_id)``
        for every OpenClaw session the adapter has recorded in any
        project's sessionslist.

        Row enumeration is shared (see
        :mod:`services.cowork_agent.visualizer.discovery`); the
        OpenClaw-specific part is the session-key parse.
        """
        if not agent_db.AGENTS_DIR.is_dir():
            return
        for project_id, composite, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            if not isinstance(native, str) or not native or "/" in native:
                continue
            agent = agent_db.agent_from_session_key(composite) or "main"
            yield project_id, agent, composite, native

    # ── Per-session pipelines ───────────────────────────────────────────

    def _poll_database(
        self, project_id: str, agent: str, session_key: str, native: str
    ) -> Generator[Event, None, bool]:
        """Emit new transcript rows for every generation of the session
        key. Events carry the row's ``nativeSessionId`` so a reset
        doesn't split one XO session in two. Returns whether any offset
        moved."""
        changed = False
        for generation in agent_db.list_generations(agent, session_key) or [native]:
            offset_key = f"{agent}/{generation}"
            rows = agent_db.read_generation_after(
                agent, generation, self._db_offsets.get(offset_key, -1), _BATCH
            )
            for _seq, raw in rows:
                if raw is None or raw.get("type") == "session":
                    continue
                for ev in self._normalize_event(raw, current_sid=native):
                    yield from self._emit(project_id, ev)
            if rows:
                self._db_offsets[offset_key] = rows[-1][0]
                changed = True
        return changed

    def _tail_one(self, project_id: str, jsonl_path: Path) -> Iterator[Event]:
        for line in jsonl_tail.read_new_lines(jsonl_path, self.offsets):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("OpenClaw source: dropped malformed line in %s", jsonl_path)
                continue
            if not isinstance(raw, dict):
                continue

            # Header row — record the session id and move on. The
            # next line's events carry the ts we want
            # SessionFirstSeen anchored to.
            if raw.get("type") == "session":
                sid = raw.get("id")
                if isinstance(sid, str) and sid:
                    self._sid_by_path[str(jsonl_path)] = sid
                continue

            sid = self._sid_by_path.get(str(jsonl_path))
            if not sid:
                # Tail started mid-file (server restart, offset>0) and
                # we never saw the header. Fall back to the filename
                # stem — OpenClaw's jsonl name == sessionId by
                # convention.
                sid = jsonl_path.stem
                self._sid_by_path[str(jsonl_path)] = sid

            for ev in self._normalize_event(raw, current_sid=sid):
                yield from self._emit(project_id, ev)

    def _emit(self, project_id: str, ev: Event) -> Iterator[Event]:
        ev = dataclasses.replace(ev, project_id=project_id)
        nsid = ev.native_session_id
        if nsid and nsid not in self._sessions_seen:
            self._sessions_seen.add(nsid)
            yield SessionFirstSeen(
                ts=ev.ts,
                native_session_id=nsid,
                runtime=self.name,
                project_id=project_id,
                cwd="",  # OpenClaw doesn't surface a cwd per record
            )
        # Latency tracking: stash user-message ts, attach
        # latency_ms to the next UsageObserved for this
        # session, pop on use so each user message
        # contributes at most one sample.
        if isinstance(ev, MessageObserved) and ev.role == "user" and nsid:
            self._last_user_ts[nsid] = ev.ts
        elif isinstance(ev, UsageObserved) and nsid:
            user_ts = self._last_user_ts.pop(nsid, None)
            if user_ts is not None:
                latency = compute_latency_ms(user_ts, ev.ts)
                if latency is not None:
                    ev = dataclasses.replace(ev, latency_ms=latency)
        yield ev

    # ── Normaliser ──────────────────────────────────────────────────────

    def _normalize_event(self, raw: dict, *, current_sid: str) -> Iterator[Event]:
        """Yield zero or more normalised events from one raw OpenClaw
        transcript record.

        Unlike Claude, OpenClaw doesn't repeat the session id on every
        message — the caller passes it in via ``current_sid``. Header
        rows (``type:"session"``) are skipped by the callers; this
        method is only called for message rows.
        """
        if not isinstance(raw, dict):
            return
        if raw.get("type") != "message":
            return

        ts = raw.get("timestamp")
        if not isinstance(ts, str) or not ts:
            return
        if not current_sid:
            return

        msg = raw.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return

        model: Optional[str] = None
        if role == "assistant":
            m = msg.get("model")
            if isinstance(m, str) and m:
                model = m

        yield MessageObserved(
            ts=ts, native_session_id=current_sid, runtime=self.name,
            role=role, model=model,
        )

        usage = msg.get("usage")
        if isinstance(usage, dict):
            # OpenClaw token field names → Claude-shaped fields.
            yield UsageObserved(
                ts=ts, native_session_id=current_sid, runtime=self.name,
                input_tokens=int(usage.get("input", 0) or 0),
                output_tokens=int(usage.get("output", 0) or 0),
                cache_read_input_tokens=int(usage.get("cacheRead", 0) or 0),
                cache_creation_input_tokens=int(usage.get("cacheWrite", 0) or 0),
                model=model,
            )

        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "toolCall":
                    continue
                name = block.get("name")
                if isinstance(name, str) and name:
                    yield ToolUseObserved(
                        ts=ts, native_session_id=current_sid, runtime=self.name,
                        tool=name,
                    )


def _load_db_offsets() -> dict[str, int]:
    """Load the persisted per-generation offsets. Missing/corrupt file →
    empty dict (everything is re-emitted once; the sinks key events by
    session and ts)."""
    if not _OFFSETS_FILE.is_file():
        return {}
    try:
        data = json.loads(_OFFSETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


def _save_db_offsets(offsets: dict[str, int]) -> None:
    """Atomic write so a mid-write crash doesn't corrupt the file."""
    _OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OFFSETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets, separators=(",", ":")), encoding="utf-8")
    tmp.replace(_OFFSETS_FILE)
