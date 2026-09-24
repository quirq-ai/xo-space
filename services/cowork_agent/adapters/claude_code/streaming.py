from __future__ import annotations

import json

from services.cowork_agent.engine import stream_events as se


def _block_activity(block: dict, *, partial: bool) -> dict | None:
    """The activity a non-text content block stands for, as a label only.

    ``thinking`` becomes the thinking label; ``tool_use`` names the tool. The
    thinking text and the tool's input never leave here: they are in the
    record, not the live stream.
    """
    btype = block.get("type")
    if btype == "thinking":
        return se.activity(se.THINKING, partial=partial)
    if btype == "tool_use":
        return se.running(block.get("name") or "", partial=partial)
    return None


def parse_stream_line(raw: bytes) -> dict | None:
    """
    Decode one raw line from Claude Code's stream-json output.
    Returns a normalised event dict (see ``engine.stream_events``) or None
    to skip.
    """
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

    etype = event.get("type", "")

    if etype == "system" and event.get("subtype") == "init":
        # First event Claude CLI emits in stream-json mode — carries the
        # native session_id BEFORE any tokens. Surface it as an internal
        # event so the adapter can persist the nativeSessionId mapping
        # before any chance of SSE cancellation.
        sid = event.get("session_id") or event.get("sessionId")
        if sid:
            return se.session_id(sid)
        return None

    if etype == "stream_event":
        # `--include-partial-messages`: API streaming events wrapped in a
        # stream_event line. Text deltas become tokens; a block start for
        # thinking or a tool becomes an activity label. Thinking and
        # tool-input deltas carry content, and are dropped.
        inner = event.get("event") or {}
        itype = inner.get("type")
        if itype == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return se.token(delta["text"], partial=True)
            return None
        if itype == "content_block_start":
            block = inner.get("content_block") or {}
            # ``partial``: the CLI repeats the finished block as a complete
            # ``assistant`` message, which the adapter then skips, exactly as
            # it does for text.
            return _block_activity(block, partial=True)
        return None

    if etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                return se.token(text, partial=True)
        return None

    if etype == "assistant":
        # One complete content block. Text is the reply; a thinking or
        # tool_use block names what the agent is doing.
        message = event.get("message", {})
        parts = []
        blocks = message.get("content", [])
        for block in blocks:
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        if parts:
            return se.token("".join(parts))
        for block in blocks:
            act = _block_activity(block, partial=False)
            if act is not None:
                return act
        return None

    if etype == "result":
        if event.get("is_error"):
            return se.error(event.get("result", "Claude Code error"))
        return se.result(
            result=event.get("result", ""),
            session_id=event.get("session_id"),
            usage=event.get("usage") or {},
            model=event.get("model", ""),
        )

    if etype == "text":
        text = event.get("text", "") or event.get("content", "")
        if text:
            return se.token(text)
        return None

    if etype == "error":
        return se.error(event.get("error", event.get("message", "unknown error")))

    # ``user`` (a tool result going back to the agent), rate limits, and
    # everything else: not part of the reply.
    return None
