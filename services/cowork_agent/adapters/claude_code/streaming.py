from __future__ import annotations

import json


def parse_stream_line(raw: bytes) -> dict | None:
    """
    Decode one raw line from Claude Code's stream-json output.
    Returns a normalised event dict or None to skip.
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
            return {"type": "session_id", "session_id": sid}
        return None

    if etype == "stream_event":
        # `--include-partial-messages`: API streaming events wrapped in a
        # stream_event line. Only text deltas become tokens; thinking and
        # tool-input deltas have other delta types and are dropped here.
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return {"type": "token", "token": delta["text"], "partial": True}
        return None

    if etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                return {"type": "token", "token": text, "partial": True}
        return None

    if etype == "assistant":
        message = event.get("message", {})
        parts = []
        for block in message.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        if parts:
            return {"type": "token", "token": "".join(parts)}
        return None

    if etype == "result":
        if event.get("is_error"):
            return {"type": "error", "error": event.get("result", "Claude Code error")}
        return {
            "type": "result",
            "result": event.get("result", ""),
            "session_id": event.get("session_id"),
            "usage": event.get("usage") or {},
            "model": event.get("model", ""),
        }

    if etype == "text":
        text = event.get("text", "") or event.get("content", "")
        if text:
            return {"type": "token", "token": text}
        return None

    if etype == "error":
        return {"type": "error", "error": event.get("error", event.get("message", "unknown error"))}

    return None
