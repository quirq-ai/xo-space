from __future__ import annotations

import json
from typing import Any

from services.cowork_agent.engine import stream_events as se


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


# Non-message ``item.type`` values that mean "work in progress". Surfaced as a
# ``model-loading`` progress ping (forwarded to SSE, see chat.py:166-172), never
# as assistant tokens, and never carrying inputs/paths (PII boundary).
# TODO(codex): confirm the exact live non-message item.type enum on an authed
# --json run (blueprint §12.10) — only ``agent_message`` and ``error`` were seen
# on the local (401'd) run. Unknown item types fall through to None below, safe.
_TOOL_ITEM_LABELS = {
    "command_execution": se.RUNNING_COMMAND,
    "file_change": se.EDITING_FILES,
    "mcp_tool_call": se.CALLING_TOOL,
}


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
            return se.session_id(sid)
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
                return se.token(text)
            return None

        # reasoning → opaque/encrypted; NEVER surface as text (blueprint §1.3).
        # Optional progress ping; return None instead if "thinking" is too noisy.
        if item_type == "reasoning":
            return se.activity(se.THINKING)

        # tool/work items → progress ping only (label, never inputs = PII).
        if item_type in _TOOL_ITEM_LABELS:
            return se.activity(_TOOL_ITEM_LABELS[item_type])

        # item.type == "error" or any unknown type → skip; the authoritative
        # failure text arrives via a top-level turn.failed/error event below.
        return None

    # ── turn.completed → usage rollup (captured, not forwarded) ────────────────
    # Wire usage field names are UNVERIFIED (blueprint §12.3); the on-disk rollout
    # token_count is authoritative for usage.py regardless. Pass the dict through
    # so the adapter finally-block can roll it up (mirrors claude adapter.py:474
    # reading event.get("usage")).
    if etype == "turn.completed":
        return se.result(usage=event.get("usage") or {})

    # ── turn.failed / transport error → error (dict AND str shapes) ────────────
    if etype in ("turn.failed", "error"):
        return se.error(_error_text(event))

    # thread/turn.started and everything else → skip.
    return None
