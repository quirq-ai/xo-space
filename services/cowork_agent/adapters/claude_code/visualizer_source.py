"""Claude Code visualizer source — tails ``~/.claude/projects/*.jsonl``
and reads ``~/.claude/sessions/<pid>.json`` for live presence.

Loaded by :func:`services.cowork_agent.visualizer.source_loader.load_source_module`
when ``AGENT_NAME=claude_code``. The class name ``Source`` is the
loader contract.

Responsibilities:

* Walk every workspace project, enumerate its Claude jsonls, tail
  each via :mod:`ingest.jsonl_tail` (offsets persisted across
  restarts).
* Normalise lines through :mod:`ingest.pii_filter` and yield
  sink-consumable events.
* **Pair** ``TaskCreateObserved`` events with their matching
  ``ToolResultObserved`` to recover the user-visible task id
  (``"Task #N created successfully…"`` in the result text) and emit
  a final :class:`events.TaskCreated`.
* **Re-anchor** ``FileTouchPending`` events: turn the absolute
  ``file_path`` from an Edit/Write/NotebookEdit tool_use into a
  project-relative path, dropping events that escape the project.
* Emit a single :class:`events.SessionFirstSeen` per session id.
* Poll ``~/.claude/sessions/<pid>.json`` files for the live presence
  snapshot (used by the activity sink).

Internal carriers (``TaskCreateObserved``, ``ToolResultObserved``,
``FileTouchPending``) never leave the source — they are converted
into final events here, or dropped. Sinks only see clean events.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail, pii_filter
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    TaskCreated,
    TaskCreateObserved,
    ToolResultObserved,
    UsageObserved,
    compute_latency_ms,
)
from services.cowork_agent.visualizer.project_index import project_id_for_cwd
from services.cowork_agent.visualizer.workspace_index import list_project_ids
from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)


_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

_TASK_RESULT_RE = re.compile(r"Task #(\d+) created", re.IGNORECASE)


class Source:
    """Visualizer source for the Claude Code backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = "claude_code"

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        # Native session id → True. Seeded lazily; used to emit
        # exactly one SessionFirstSeen per session.
        self._sessions_seen: set[str] = set()
        # (native_session_id, tool_use_id) → pending TaskCreate, when the
        # TaskCreate tool_use was observed before its "Task #N created"
        # tool_result.
        self._pending_creates: dict[tuple[str, str], TaskCreateObserved] = {}
        # (native_session_id, tool_use_id) → parsed task id, for the
        # reverse order: Claude occasionally writes the tool_result line
        # BEFORE the tool_use that produced it. We stash ONLY the parsed
        # integer id (never the result text — it may carry PII) until the
        # matching TaskCreate arrives. Symmetric with _pending_creates.
        self._pending_results: dict[tuple[str, str], str] = {}
        # native_session_id → ts of the last MessageObserved(role="user").
        # Used to attach latency_ms on the matching UsageObserved.
        # Cleared on attachment so a single user message contributes
        # at most one latency sample.
        self._last_user_ts: dict[str, str] = {}
        # Claude Code's jsonl emits MULTIPLE records per actual assistant
        # turn, all sharing one ``message.id`` (the Anthropic ``msg_*`` id).
        # The ``usage`` block is repeated IDENTICALLY on every record
        # (summing it over-counts tokens 3-7×), but the CONTENT blocks
        # DIFFER per record — ``thinking`` on one line, ``text`` on the
        # next, a ``tool_use`` on a third. So we dedupe at the EVENT level,
        # not the line level: the first record for a ``(session, msg.id)``
        # contributes its usage + assistant-turn count; later records
        # contribute only their distinct tool events (TaskCreate /
        # TaskUpdate / Edit / Bash …). Skipping whole lines here used to
        # drop those tool events — see
        # docs/claude-code-multiline-message-dedup-investigation-2026-06-03.md.
        self._seen_anthropic_message_ids: set[tuple[str, str]] = set()

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        """One tick. Yields normalised events from every workspace
        project's Claude jsonls. Flushes the offset store at the
        end (atomic; survives crashes mid-tick).
        """
        for project_id, jsonl_path in self._discover_jsonls():
            yield from self._tail_one(project_id, jsonl_path)

        # Persist offsets so restarts don't double-count.
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("Claude source: offset flush failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        """Snapshot of every live Claude process matching a workspace
        project. Each row is the shape the activity sink expects.

        ``<pid>.json`` looks like::

            {"pid": 2578, "sessionId": "ac4029fc-…", "cwd": "/home/coder/xo-projects/blackhole",
             "startedAt": 1778591539234, "status": "busy", "updatedAt": 1778593116251,
             "version": "2.1.138", "entrypoint": "cli", "kind": "interactive"}

        We translate ``cwd`` to ``project_id`` (drop rows outside the
        workspace) and pass the rest through with a stable key
        layout.
        """
        rows: list[dict] = []
        if not _CLAUDE_SESSIONS_DIR.is_dir():
            return rows
        for f in sorted(_CLAUDE_SESSIONS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            cwd = data.get("cwd") or ""
            project_id = project_id_for_cwd(cwd) if isinstance(cwd, str) else None
            if not project_id:
                continue
            pid_int = data.get("pid")
            if isinstance(pid_int, int) and not _pid_alive(pid_int):
                # Stale — the process exited but the file lingered.
                continue
            rows.append({
                "session_id":  str(data.get("sessionId", "")),
                "runtime":     self.name,
                "project_id":  project_id,
                "started_at_ms": int(data.get("startedAt", 0) or 0),
                "updated_at_ms": int(data.get("updatedAt", 0) or 0),
                "status":      str(data.get("status", "")),
                "entrypoint":  str(data.get("entrypoint", "")),
                "version":     str(data.get("version", "")),
            })
        return rows

    # ── Discovery ───────────────────────────────────────────────────────

    def _discover_jsonls(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(project_id, jsonl_path)`` for every Claude jsonl
        the runtime adapters have recorded in any project's
        sessionslist.

        The row enumeration is shared (see
        :mod:`services.cowork_agent.visualizer.discovery`); we only
        do the Claude-specific bit: turn the row's ``directory`` into
        the encoded jsonl path under ``~/.claude/projects/``.

        We iterate adapter rows (not the encoded-cwd directories)
        because the session's actual ``cwd`` doesn't always match the
        project id — e.g. the cowork-api ``default`` project routes
        chats at ``/home/coder/xo-projects/`` (the workspace root)
        rather than ``/home/coder/xo-projects/default/``. The encoded
        directory ``-home-coder-xo-projects`` belongs to ``default``
        in this case, and a project-id-driven glob would miss it.

        Sessions that don't have an adapter row are intentionally
        skipped (e.g. someone running ``claude`` directly in a project
        folder outside the cowork-api chat flow). Watching them would
        also pollute usage totals with sessions the rest of the
        system doesn't know about.
        """
        if not _CLAUDE_PROJECTS_DIR.is_dir():
            return

        yielded: set[Path] = set()

        # 1. Existing adapter-row path (cowork-api chat flow). Preserved
        # unchanged so sessions started via the chat UI keep their full
        # composite-key / directory metadata.
        for project_id, _composite_key, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            directory = row.get("directory")
            if not isinstance(native, str) or not native:
                continue
            if not isinstance(directory, str) or not directory:
                continue
            # Claude's encoding: '/foo/bar' → '-foo-bar' (lossless forward).
            encoded = directory.replace("/", "-")
            jsonl = _CLAUDE_PROJECTS_DIR / encoded / f"{native}.jsonl"
            if jsonl.is_file() and jsonl not in yielded:
                yielded.add(jsonl)
                yield project_id, jsonl

        # 2. Auto-discovery for sessions launched directly inside an
        # xo-project (e.g. ``cd ~/xo-projects/foo && claude``). These
        # don't have a sessionslist row, so step 1 misses them. We
        # walk each project's encoded-cwd directory under
        # ``~/.claude/projects/`` and yield any jsonl we haven't
        # already yielded above. Existing rows take precedence — only
        # genuinely-new jsonls flow through here. The ``default``
        # project, whose chat cwd is the workspace root (not
        # ``xo-projects/default``), is intentionally skipped here:
        # its sessions arrive only via the adapter-row path above.
        projects_root = xo_projects_root()
        project_roots = [projects_root]
        host_projects_root = (
            os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or ""
        ).strip()
        if host_projects_root:
            project_roots.append(Path(host_projects_root).expanduser())
        for project_id in list_project_ids():
            for project_root in project_roots:
                project_path = project_root / project_id
                encoded = str(project_path).replace("/", "-")
                log_dir = _CLAUDE_PROJECTS_DIR / encoded
                if not log_dir.is_dir():
                    continue
                for jsonl in log_dir.glob("*.jsonl"):
                    if jsonl in yielded:
                        continue
                    yielded.add(jsonl)
                    yield project_id, jsonl

    # ── Per-jsonl pipeline ──────────────────────────────────────────────

    def _tail_one(self, project_id: str, jsonl_path: Path) -> Iterator[Event]:
        for line in jsonl_tail.read_new_lines(jsonl_path, self.offsets):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Claude source: dropped malformed line in %s", jsonl_path)
                continue
            if not isinstance(raw, dict):
                continue

            # Note this line's Anthropic message id. A repeat means this is
            # a continuation record of a turn we've already counted — its
            # duplicated usage/turn events must be suppressed, but its
            # (distinct) tool events must still flow. See the constructor
            # note and the 2026-06-03 investigation doc.
            usage_already_counted = self._note_assistant_line(raw)

            cwd = raw.get("cwd") if isinstance(raw, dict) else None
            for ev in pii_filter.normalize_event(raw, runtime=self.name):
                if usage_already_counted and _is_duplicate_turn_event(ev):
                    continue
                # Back-fill project_id on every event (the filter is
                # stateless; the source is the only place that knows).
                ev = dataclasses.replace(ev, project_id=project_id)
                yield from self._post_process(ev, cwd=cwd, fallback_project_id=project_id)

    def _note_assistant_line(self, raw: dict) -> bool:
        """Record the Anthropic ``message.id`` of an assistant line.

        Returns ``True`` when we've already seen a record for this
        ``(sessionId, message.id)`` — i.e. this line is a continuation
        chunk of a turn whose usage/turn-count was already emitted, so
        those events must be suppressed (its tool events still pass).
        Non-assistant lines, or lines without a usable id, return
        ``False`` (nothing to dedupe).
        """
        if raw.get("type") != "assistant":
            return False
        msg = raw.get("message") if isinstance(raw.get("message"), dict) else None
        anth_mid = msg.get("id") if msg else None
        sid = raw.get("sessionId")
        if not (isinstance(anth_mid, str) and anth_mid
                and isinstance(sid, str) and sid):
            return False
        key = (sid, anth_mid)
        if key in self._seen_anthropic_message_ids:
            return True
        self._seen_anthropic_message_ids.add(key)
        return False

    def _post_process(
        self,
        ev: Event,
        *,
        cwd: Optional[str],
        fallback_project_id: str,
    ) -> Iterator[Event]:
        # 1) Emit SessionFirstSeen once per session, before anything else.
        nsid = ev.native_session_id
        if nsid and nsid not in self._sessions_seen:
            self._sessions_seen.add(nsid)
            yield SessionFirstSeen(
                ts=ev.ts,
                native_session_id=nsid,
                runtime=self.name,
                project_id=ev.project_id or fallback_project_id,
                cwd=cwd or "",
            )

        # 2) Latency tracking. User message: stash its ts so the next
        # assistant turn for this session can derive a wall-clock
        # delta. UsageObserved: look up the stashed user ts and
        # attach latency_ms via dataclass.replace. Pop on use so each
        # user message contributes at most one latency sample.
        if isinstance(ev, MessageObserved) and ev.role == "user" and nsid:
            self._last_user_ts[nsid] = ev.ts
        elif isinstance(ev, UsageObserved) and nsid:
            user_ts = self._last_user_ts.pop(nsid, None)
            if user_ts is not None:
                latency = compute_latency_ms(user_ts, ev.ts)
                if latency is not None:
                    ev = dataclasses.replace(ev, latency_ms=latency)

        # 3) Task pairing — symmetric. A TaskCreate tool_use and its
        # "Task #N created" tool_result can appear in EITHER order in the
        # jsonl; we buffer whichever arrives first and emit the final
        # TaskCreated once both are in hand.
        if isinstance(ev, TaskCreateObserved):
            yield from self._pair_task_create(ev)
            return  # internal-only until paired

        if isinstance(ev, ToolResultObserved):
            yield from self._pair_task_result(ev)
            return  # ToolResultObserved is internal-only

        # 4) File-touch re-anchoring.
        if isinstance(ev, pii_filter.FileTouchPending):
            yield from self._reanchor_path(ev, cwd=cwd)
            return  # FileTouchPending is internal-only

        # 5) Everything else passes straight to the sinks.
        yield ev

    def _pair_task_create(self, ev: TaskCreateObserved) -> Iterator[TaskCreated]:
        """Handle a TaskCreate tool_use. If we've already seen its
        ``Task #N created`` result (out-of-order lines), emit the final
        :class:`TaskCreated` now; otherwise buffer the create until the
        result arrives.
        """
        key = (ev.native_session_id, ev.tool_use_id)
        task_id = self._pending_results.pop(key, None)
        if task_id is None:
            self._pending_creates[key] = ev
            return  # result not seen yet — wait for it
        yield self._build_task_created(ev, task_id)

    def _pair_task_result(self, ev: ToolResultObserved) -> Iterator[TaskCreated]:
        """Handle a tool_result. Only ``Task #N created`` results carry
        anything we keep — every other result is dropped (its text may
        contain PII: Bash output, file contents, fetched HTML).

        Pairs with a buffered TaskCreate when one exists; otherwise
        stashes the parsed id for an out-of-order TaskCreate still to
        come.
        """
        key = (ev.native_session_id, ev.tool_use_id)
        m = _TASK_RESULT_RE.match(ev.content_text or "")
        if not m:
            # Not a recoverable Task result. If a TaskCreate was waiting
            # on this id, its result text didn't carry "Task #N" — drop
            # the create (we can't recover its id). Otherwise it's just a
            # non-Task tool_result — drop silently, never buffer the text.
            pending = self._pending_creates.pop(key, None)
            if pending is not None:
                logger.warning(
                    "Claude source: TaskCreate result missing 'Task #N' prefix; "
                    "tool_use_id=%s", ev.tool_use_id,
                )
            return
        task_id = m.group(1)
        pending = self._pending_creates.pop(key, None)
        if pending is None:
            # Out-of-order: result arrived before its TaskCreate. Stash
            # only the parsed integer id — never the result text.
            self._pending_results[key] = task_id
            return
        yield self._build_task_created(pending, task_id)

    def _build_task_created(
        self, pending: TaskCreateObserved, task_id: str
    ) -> TaskCreated:
        return TaskCreated(
            ts=pending.ts,
            native_session_id=pending.native_session_id,
            runtime=pending.runtime,
            project_id=pending.project_id,
            task_id=task_id,
            content=pending.content,
            description=pending.description,
            active_form=pending.active_form,
        )

    def _reanchor_path(
        self, ev: "pii_filter.FileTouchPending", *, cwd: Optional[str]
    ) -> Iterator[FileTouched]:
        """Convert an absolute ``file_path`` into a project-relative
        path. Drops the event if the path escapes the project root.
        """
        # Resolve project from the event's authoritative ``cwd`` — falls
        # back to the encoded-dir discovery if cwd is missing.
        project_id = project_id_for_cwd(cwd) if cwd else None
        if project_id is None:
            return
        project_root = (xo_projects_root() / project_id).resolve()
        try:
            abs_target = Path(ev.abs_file_path).resolve()
            rel = abs_target.relative_to(project_root)
        except (ValueError, OSError):
            return  # outside project root — drop
        yield FileTouched(
            ts=ev.ts,
            native_session_id=ev.native_session_id,
            runtime=ev.runtime,
            project_id=project_id,
            relative_path=str(rel).replace("\\", "/"),
            created=(ev.tool == "Write"),
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _is_duplicate_turn_event(ev: Event) -> bool:
    """True for events that represent the assistant TURN itself — token
    usage and the assistant message count — which Claude repeats on every
    record of a multi-line message. Suppressed on continuation chunks so
    they're counted exactly once per turn.

    Tool events (:class:`ToolUseObserved`, :class:`TaskCreateObserved`,
    :class:`TaskStatusChanged`, :class:`FileTouched`/``FileTouchPending``,
    :class:`ToolResultObserved`) are NOT duplicates — each rides a distinct
    record — so they always pass through.
    """
    if isinstance(ev, UsageObserved):
        return True
    if isinstance(ev, MessageObserved) and ev.role == "assistant":
        return True
    return False


def _pid_alive(pid: int) -> bool:
    """Cheap process-alive probe; same UID-only check the design's
    activity sink would use anyway. POSIX-only.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal — count as alive.
        return True
    except OSError:
        return False
    return True
