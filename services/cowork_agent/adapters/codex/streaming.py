from __future__ import annotations

import json
from typing import Any


def _extract_text_from_item(item: dict[str, Any]) -> str:
    """
    Best-effort assistant text from a codex ``item.*`` payload.

    Lifted in behaviour from the Plane-A client
    (``config/models/codex/client.py:45-69``). Hedges three shapes because the
    exact 0.145 wire field for assistant text is UNVERIFIED (blueprint §12.1):
      1. ``item["text"]``                             (flat)
      2. ``item["message"]["text"]``                  (nested message)
      3. join ``item["message"]["content"][].text``   (content-array)
    """
    if not item:
        return ""

    text = item.get("text")
    if isinstance(text, str) and text:
        return text

    message = item.get("message")
    if isinstance(message, dict):
        msg_text = message.get("text")
        if isinstance(msg_text, str) and msg_text:
            return msg_text

        content = message.get("content", [])
        if isinstance(content, list):
            chunks = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            return "".join(chunks)

    return ""


def _error_text(event: dict[str, Any]) -> str:
    """
    Normalise a codex failure event to a plain string, handling BOTH observed
    shapes (blueprint §1.2, Plane-A client.py:216-223):
      * transport   ``{"type":"error","message":"…401 Unauthorized…"}``  (top-level str)
      * turn.failed ``{"type":"turn.failed","error":{"message":"…"}}``   (nested dict)
      * defensive   ``{"error":"…"}``                                     (flat str)
    """
    err = event.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
    elif isinstance(err, str) and err:
        return err

    msg = event.get("message")
    if isinstance(msg, str) and msg:
        return msg

    return "Codex error"


# Non-message ``item.type`` values that carry a tool call, mapped to the tool
# name its chip is labelled with. Kept in step with the names the rollout
# converter produces (``codex/sessions.py``) so one call reads the same while
# it runs and after the history refetch replaces the live chip.
#
# Verified live for ``command_execution`` (item.started → item.completed, with
# ``id`` / ``command`` / ``aggregated_output`` / ``exit_code`` / ``status``).
# The other two are (UNVERIFIED) — their field names come from the blueprint,
# and ``_tool_arguments`` degrades to an empty dict rather than guessing.
_TOOL_ITEM_NAMES = {
    "command_execution": "shell",
    "file_change": "apply_patch",
    "mcp_tool_call": "mcp",
}

# A tool's output goes to the UI so the chip can be opened mid-run. Cap it: a
# single build or test command can emit megabytes on one line, and holding an
# SSE frame open for that stalls every later event. The transcript keeps the
# full text — the history refetch reads it straight from the rollout.
_MAX_LIVE_OUTPUT = 100_000
_TRUNCATION_NOTE = "\n… output truncated — see the completed message"


def _tool_arguments(item: dict[str, Any]) -> dict[str, Any]:
    """The chip's ``arguments``, shaped like the rollout converter's
    ``_tool_input`` (``{"command": …}`` for a shell call) so the live chip and
    the refetched one describe the call identically."""
    command = item.get("command")
    if isinstance(command, str) and command:
        return {"command": command}
    args = item.get("arguments") or item.get("input")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        return {"arguments": args}
    return {}


def _tool_event(etype: str, item: dict[str, Any], item_type: str) -> dict | None:
    """A tool lifecycle event for the UI, or None when there is nothing new.

    ``item.started`` opens the chip, ``item.completed`` fills in its output and
    marks success or failure. ``item.updated`` carries no field the chip shows,
    so it is skipped rather than re-rendering the same row.
    """
    call_id = item.get("id") or ""
    if not call_id:
        return None                      # nothing to correlate the result to
    name = _TOOL_ITEM_NAMES[item_type]

    if etype == "item.started":
        return {
            "type": "tool-call", "tool": name, "call_id": call_id,
            "arguments": _tool_arguments(item),
        }

    if etype == "item.completed":
        output = item.get("aggregated_output")
        output = output if isinstance(output, str) else ""
        if len(output) > _MAX_LIVE_OUTPUT:
            output = output[:_MAX_LIVE_OUTPUT] + _TRUNCATION_NOTE
        exit_code = item.get("exit_code")
        failed = (
            item.get("status") == "failed"
            or (isinstance(exit_code, int) and exit_code != 0)
        )
        return {
            "type": "tool-error" if failed else "tool-result",
            "tool": name, "call_id": call_id, "output": output,
        }

    return None


def parse_stream_line(raw: bytes) -> dict | None:
    """
    Decode one raw JSONL line from ``codex exec --json`` stdout (the WIRE schema)
    into a normalised internal event, or ``None`` to skip.

    Consumed by ``codex/adapter.py``'s ``stream()`` loop exactly as claude_code
    consumes its parser (``claude_code/adapter.py:453-489``). Return contract — a
    dict whose ``type`` is one of:

      * ``{"type":"session_id","session_id":<uuid>}`` — bookkeeping; adapter
        persists nativeSessionId and does NOT forward to SSE.
      * ``{"type":"token","token":<str>}``            — appended to response and
        forwarded to SSE as text-delta.
      * ``{"type":"model-loading","label":<str>}``    — forwarded to SSE as a
        progress ping.
      * ``{"type":"result","usage":<dict>}``          — usage rollup; adapter
        captures ``event["usage"]`` (mirrors claude_code adapter.py:474) and does
        NOT forward.
      * ``{"type":"error","error":<str>}``            — forwarded to SSE as
        agent-error.

    or ``None`` (skip). HARD RULE (blueprint §1.2): the subprocess ``returncode``
    is NOT a failure signal for codex — an unauthenticated run exits 0 while
    emitting ``turn.failed``/``error``. Those events are the authoritative
    failure; the adapter must not raise on rc != 0.
    """
    # ── decode / JSON guard — mirrors claude_code/streaming.py:11-22 ───────────
    # Codex interleaves non-JSON TRACE/ERROR lines on *stderr* (not read here),
    # but a stray non-JSON line on stdout is skipped too.
    try:
        line = raw.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None

    if not line:
        return None

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(event, dict):
        return None

    etype = event.get("type", "")

    # ── thread.started → native session id (first wire event) ──────────────────
    # thread_id == session_meta.session_id == rollout filename uuid (UUIDv7).
    # Mirrors Plane-A client.py:132-134; consumed like claude's "session_id"
    # (adapter.py:461-466): patch nativeSessionId immediately, do not forward.
    if etype == "thread.started":
        sid = event.get("thread_id")
        if sid:
            return {"type": "session_id", "session_id": sid}
        return None

    # ── item.started / item.updated / item.completed ───────────────────────────
    if etype.startswith("item."):
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type", "")

        # agent_message → assistant text token (Plane-A client.py:137-140).
        #
        # TODO(codex): confirm streaming granularity (blueprint §12.2). If codex
        # emits CUMULATIVE text on item.updated AND then repeats the full text on
        # item.completed, emitting on every event double-renders. parse_stream_line
        # is stateless (one line at a time) and cannot dedup across lines. If
        # cumulative, either gate this branch to ``etype == "item.completed"``
        # only, or have adapter.py track the last-emitted length and yield only
        # the new suffix.
        if item_type == "agent_message":
            text = _extract_text_from_item(item)
            if text:
                return {"type": "token", "token": text}
            return None

        # reasoning → opaque/encrypted; NEVER surface as text (blueprint §1.3).
        # Optional progress ping; return None instead if "thinking" is too noisy.
        if item_type == "reasoning":
            return {"type": "model-loading", "label": "thinking"}

        # tool/work items → a real tool chip, opened on item.started and filled
        # on item.completed. This replaces the old progress-label ping: the chip
        # shows the same activity plus what actually ran, and unlike the label it
        # survives on screen instead of being cleared by the next text event.
        if item_type in _TOOL_ITEM_NAMES:
            return _tool_event(etype, item, item_type)

        # item.type == "error" or any unknown type → skip; the authoritative
        # failure text arrives via a top-level turn.failed/error event below.
        return None

    # ── turn.completed → usage rollup (captured, not forwarded) ────────────────
    # Wire usage field names are UNVERIFIED (blueprint §12.3); the on-disk rollout
    # token_count is authoritative for usage.py regardless. Pass the dict through
    # so the adapter finally-block can roll it up (mirrors claude adapter.py:474
    # reading event.get("usage")).
    if etype == "turn.completed":
        return {"type": "result", "usage": event.get("usage") or {}}

    # ── turn.failed / transport error → error (dict AND str shapes) ────────────
    if etype in ("turn.failed", "error"):
        return {"type": "error", "error": _error_text(event)}

    # thread/turn.started and everything else → skip.
    return None
