"""Normalised event types — the API the sinks consume.

Frozen, ``__slots__``-backed dataclasses. Sinks dispatch on
``isinstance`` and never see raw jsonl shapes (P5-style boundary
inside the watcher itself).

Shared helpers (small enough to live alongside the types they
operate on): :func:`compute_latency_ms` derives the user→assistant
latency that :class:`UsageObserved` carries — used by every file-tail
source.

The shape is intentionally **sink-oriented**, not jsonl-oriented:

* :class:`MessageObserved` collapses both user and assistant messages
  to a single counter event.
* :class:`UsageObserved` carries token counts; emitted alongside (not
  inside) :class:`MessageObserved` because OpenClaw and Claude Code
  attach usage to different surfaces.
* :class:`ToolUseObserved` carries only the tool **name** — never
  inputs (Bash commands, Edit diffs, etc.). The PII filter strips
  inputs before construction.
* :class:`TaskCreateObserved` and :class:`TaskUpdateObserved` are
  pre-pairing observations. The source layer correlates a
  ``TaskCreate`` tool_use with its ``Task #N created`` tool_result
  to assign the user-visible task id; once paired, the source emits
  :class:`TaskCreated`/:class:`TaskStatusChanged` to the sinks.

Path fields (:class:`FileTouched`) are always **project-relative**.
The PII filter drops events whose path resolves outside the project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


_LATENCY_MAX_S = 600  # 10 min — matches the canonical /api/usage clamp


def compute_latency_ms(user_ts: str, assistant_ts: str) -> Optional[int]:
    """User→assistant wall-clock delta in ms, or None if out-of-range.

    Both args are ISO-8601 timestamps. Returns ``None`` when either
    ts is malformed, when the delta is negative (clock skew / out-
    of-order events), or when it exceeds :data:`_LATENCY_MAX_S`
    (treated as a pause/resume rather than a real response time —
    same semantics the canonical aggregator uses; see
    ``services/cowork_agent/adapters/claude_code/usage.py``).
    """
    try:
        u = datetime.fromisoformat(user_ts.replace("Z", "+00:00"))
        a = datetime.fromisoformat(assistant_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    delta = (a - u).total_seconds()
    if 0 <= delta <= _LATENCY_MAX_S:
        return int(delta * 1000)
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base class for every normalised event.

    Every event has a timestamp (ISO-8601 UTC), the native session id
    (Claude's ``sessionId`` field — the per-jsonl UUID), and the
    runtime name. Sinks key state by ``(runtime, native_session_id)``.

    ``project_id`` is filled in by the source layer (the PII filter is
    stateless and doesn't know it; the source knows which project the
    jsonl belongs to and back-fills via ``dataclasses.replace`` before
    handing events to the watcher loop). Defaults to ``""`` so the
    filter can construct events without it; sinks reject empty.
    """

    ts: str
    native_session_id: str
    runtime: str  # the active agent's name (adapter directory name)
    project_id: str = ""


# ── Session lifecycle ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionFirstSeen(Event):
    """First event the watcher has ever observed for this session.

    Emitted exactly once per session by the source layer (deduped via
    session-id seen-set). The sinks treat this as ``session.started``
    for timeline purposes.

    ``cwd`` is the authoritative working-directory string the runtime
    reported on its first event. The source layer cross-checks
    against the encoded directory before emitting this event.
    """

    cwd: str


# ── Per-event observations ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageObserved(Event):
    """One message in the session log. ``role`` ∈ {``user``,
    ``assistant``}. ``model`` is set on assistant messages only.
    """

    role: str
    model: Optional[str] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageObserved(Event):
    """Token usage attached to an assistant turn (or a synthetic
    cumulative measure for runtimes that emit usage out-of-band).

    The watcher's stats sink aggregates these per session-day-model.

    ``latency_ms`` is the wall-clock gap between the preceding user
    message and this assistant turn, in milliseconds. ``None`` when
    the source can't derive it (user message preceded the watcher's
    offset, or the backend doesn't surface per-turn timing — hermes
    today). The stats sink only accumulates non-None values.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model: Optional[str] = None
    latency_ms: Optional[int] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolUseObserved(Event):
    """A tool_use event (the tool **name** only — never the inputs).

    Specifically excludes ``TaskCreate``/``TaskUpdate`` — those are
    routed to :class:`TaskCreateObserved`/:class:`TaskUpdateObserved`
    instead so the source layer can pair them with their tool_result.
    """

    tool: str


# ── File touches (project-relative paths only) ──────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class FileTouched(Event):
    """An ``Edit``/``Write``/``NotebookEdit`` tool_use rewritten to a
    project-relative path.

    Built by the PII filter only after re-anchoring the absolute path
    in the tool_use input against the session's project root. If the
    path escapes the project, the filter drops the event entirely.

    ``created`` is True iff the tool was ``Write`` (i.e. a new file).
    """

    relative_path: str
    created: bool = False


# ── Tasks (todos) — pre-pairing observations ─────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskCreateObserved(Event):
    """A ``TaskCreate`` tool_use, BEFORE the source pairs it with its
    tool_result to recover the assigned task id.

    The source layer keeps a per-session ``{tool_use_id:
    TaskCreateObserved}`` map; when the matching tool_result arrives,
    it parses ``"Task #N created successfully"`` from the result text
    and emits the final :class:`TaskCreated` event downstream.
    """

    tool_use_id: str
    content: str
    description: Optional[str] = None
    active_form: Optional[str] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskCreated(Event):
    """User-visible task assignment — emitted by the source layer
    once :class:`TaskCreateObserved` has been paired with its result.
    """

    task_id: str
    content: str
    description: Optional[str] = None
    active_form: Optional[str] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskStatusChanged(Event):
    """A ``TaskUpdate`` tool_use.

    ``status`` is the raw tool input, copied verbatim and NOT
    validated here — a runtime may emit anything. Consumers filter
    against
    :data:`~services.cowork_agent.visualizer.todo_status.VALID_TODO_STATUSES`
    (see ``sinks/sessions_augment.py``), which is the one definition
    of the vocabulary.
    """

    task_id: str
    status: str


# ── Source-emitted helper, not from the jsonl ────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultObserved(Event):
    """A ``tool_result`` event keyed by the matching ``tool_use_id``.

    The PII filter emits this **only** for tool_use_ids the source
    later cares about (today: ``TaskCreate``). The ``content_text``
    field carries the result's text payload — for Task results, the
    source parses ``"Task #N created successfully"`` out of it. For
    other tools, the watcher drops the result before it reaches this
    event type (see ``pii_filter`` allowlist).
    """

    tool_use_id: str
    content_text: str
