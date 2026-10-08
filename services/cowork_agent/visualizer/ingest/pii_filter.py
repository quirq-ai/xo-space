"""The redactor — the **only** module that turns a raw Claude Code
jsonl line into normalised :mod:`events` objects.

Implements docs/watcher-design.md §5. Everything in §5.1 is dropped;
only fields in §5.2 survive. The function emits zero or more events
per input line so a single ``assistant`` message with one ``tool_use``
and a separate ``usage`` block can fan out to multiple sinks.

Stateless — the source layer is responsible for pairing TaskCreate
with its tool_result and for project-relative path re-anchoring.
The filter delivers `TaskCreateObserved` and `ToolResultObserved`
events; the source's pairing logic converts them into final
`TaskCreated` events.

Every tool's inputs and results are DROPPED (they are PII). What survives
is the tool **name**: every ``tool_use`` (built-ins, ``Agent``, any
``mcp__*``) emits a :class:`events.ToolUseObserved` carrying only that,
capped at :data:`_TOOL_NAME_MAX` characters. The Task family (todos) is
the exception, handled separately below.

For ``Edit`` / ``Write`` / ``NotebookEdit`` specifically we also emit a
:class:`events.FileTouched` event carrying only the input
``file_path`` — re-anchored to project-relative by the SOURCE layer
(this module can't compute it without knowing the project root).
"""

from __future__ import annotations

from typing import Iterator, Optional

from services.cowork_agent.visualizer.ingest.events import (
    Event,
    MessageObserved,
    TaskCreateObserved,
    TaskStatusChanged,
    ToolResultObserved,
    ToolUseObserved,
    UsageObserved,
)


# Every tool gets a ``ToolUseObserved`` event (name only, never its input),
# so usage records and stats count all of them: ``Agent``, MCP tools and
# whatever a runtime adds next. The Task family is handled separately below.
# A name is an identifier, not content; the cap only bounds a malformed one.
_TOOL_NAME_MAX = 128

# Tools whose **input.file_path** the source layer should turn into a
# :class:`events.FileTouched` event. Edit = existing file, Write = new.
_FILE_TOUCH_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})


# ── Internal carriers — only the source uses these ───────────────────────────


# We expose a tiny extra dataclass here so the source can recover the
# file_path the user passed to Edit/Write before we drop the rest of
# the input. The FileTouched Event that actually reaches sinks is
# emitted by the source after re-anchoring the path to project-relative.


from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class FileTouchPending(Event):
    """Internal: an Edit/Write/NotebookEdit observed; the input
    ``file_path`` is here (still absolute). The source layer
    re-anchors to project-relative and emits :class:`events.FileTouched`,
    or drops if the path escapes the project root.
    """

    abs_file_path: str
    tool: str  # "Edit" | "Write" | "NotebookEdit"


# ── The redactor ─────────────────────────────────────────────────────────────


def normalize_event(raw: dict, *, runtime: str) -> Iterator[Event]:
    """Yield zero or more normalised events from one raw jsonl line.

    ``raw`` is the already-``json.loads``'d dict. Lines that aren't
    dicts, lack a known event type, or carry no useful information
    yield nothing.
    """
    if not isinstance(raw, dict):
        return

    sid = raw.get("sessionId")
    ts = raw.get("timestamp")
    if not isinstance(sid, str) or not isinstance(ts, str):
        # Some bookkeeping lines (e.g. ``permission-mode``) carry
        # sessionId but no timestamp; skip — sinks need both.
        return

    rtype = raw.get("type")

    # ── user / assistant messages ─────────────────────────────────────────
    if rtype in ("user", "assistant"):
        msg = raw.get("message")
        if not isinstance(msg, dict):
            return

        role = msg.get("role")
        if role not in ("user", "assistant"):
            return

        # Try to read the assistant's model id (it's the only "model"
        # name we keep from the message — value-type string, never PII).
        model: Optional[str] = None
        if role == "assistant":
            mid = msg.get("model")
            if isinstance(mid, str) and mid:
                model = mid

        yield MessageObserved(
            ts=ts, native_session_id=sid, runtime=runtime,
            role=role, model=model,
        )

        # Usage block — attached to assistant messages by Claude Code.
        usage = msg.get("usage")
        if isinstance(usage, dict):
            yield UsageObserved(
                ts=ts, native_session_id=sid, runtime=runtime,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                model=model,
            )

        # tool_use / tool_result blocks inside the message content
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                yield from _normalize_content_block(c, ts=ts, sid=sid, runtime=runtime)
        return

    # Other top-level event types (``permission-mode``,
    # ``file-history-snapshot``, ``attachment``, ``ai-title``,
    # ``last-prompt``, ``system``) carry no information the watcher
    # needs and may contain prompts / pasted code — drop entirely.
    return


# ── Content-block dispatch (tool_use / tool_result) ─────────────────────────


def _normalize_content_block(
    block: object, *, ts: str, sid: str, runtime: str
) -> Iterator[Event]:
    if not isinstance(block, dict):
        return
    btype = block.get("type")

    if btype == "tool_use":
        name = block.get("name")
        if not isinstance(name, str):
            return
        tool_use_id = str(block.get("id") or "")
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}

        # Task family (todos) — special handling.
        if name == "TaskCreate":
            content = str(inp.get("subject", "") or "")
            yield TaskCreateObserved(
                ts=ts, native_session_id=sid, runtime=runtime,
                tool_use_id=tool_use_id,
                content=content,
                description=_str_or_none(inp.get("description")),
                active_form=_str_or_none(inp.get("activeForm")),
            )
            return
        if name == "TaskUpdate":
            task_id = str(inp.get("taskId", "") or "")
            status = str(inp.get("status", "") or "")
            if task_id and status:
                yield TaskStatusChanged(
                    ts=ts, native_session_id=sid, runtime=runtime,
                    task_id=task_id, status=status,
                )
            return
        if name == "TaskStop":
            # subagent-task id, not user-visible — drop per design §2.3
            return

        # All other tools: a name-only ToolUseObserved.
        if name:
            yield ToolUseObserved(
                ts=ts, native_session_id=sid, runtime=runtime,
                tool=name[:_TOOL_NAME_MAX],
            )

        # Edit/Write/NotebookEdit also surface a pending file-touch
        # carrying the absolute path. The source re-anchors.
        if name in _FILE_TOUCH_TOOLS:
            fp = inp.get("file_path")
            if isinstance(fp, str) and fp:
                yield FileTouchPending(
                    ts=ts, native_session_id=sid, runtime=runtime,
                    abs_file_path=fp, tool=name,
                )
        return

    if btype == "tool_result":
        # Only TaskCreate results carry information we keep. The source
        # layer matches by tool_use_id and parses "Task #N created" out
        # of content_text. For every other tool, the result is dropped
        # — its content can contain command output, file contents,
        # fetched HTML, etc.
        tool_use_id = str(block.get("tool_use_id") or "")
        content = block.get("content")
        # We can't tell from the line alone whether this is a Task result.
        # The source decides — it keeps a `tool_use_id → TaskCreateObserved`
        # map and drops results whose id isn't in the map.
        if isinstance(content, str):
            yield ToolResultObserved(
                ts=ts, native_session_id=sid, runtime=runtime,
                tool_use_id=tool_use_id, content_text=content,
            )
        elif isinstance(content, list):
            # Some tools wrap result content as a list of blocks; we
            # pick out the text-typed blocks only.
            text_parts = [
                str(c.get("text", ""))
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if text_parts:
                yield ToolResultObserved(
                    ts=ts, native_session_id=sid, runtime=runtime,
                    tool_use_id=tool_use_id,
                    content_text="\n".join(text_parts),
                )
        # else: dropped


# ── Small helpers ────────────────────────────────────────────────────────────


def _str_or_none(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    return str(value)


