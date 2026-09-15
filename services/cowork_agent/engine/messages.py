"""
Convert OpenClaw's JSONL message records into the xo-cowork `MessageResponse`
shape consumed by the frontend.

Entry point: `convert_messages(session_id, records)`. Everything else here is
a helper that emits the text/reasoning/tool-call/tool-result `parts` array the
UI expects.
"""

from services.cowork_agent.helpers import iso_now, short_id, strip_workspace_preamble

_MCP_PREFIX = "mcp__"


def _display_tool_name(raw: str) -> str:
    """``mcp__<server>__<TOOL>`` -> ``<TOOL>``; anything else is returned as-is.

    MCP clients namespace every tool by the server it came from, so the CLI reports
    ``mcp__cowork__COMPOSIO_SEARCH_TOOLS``. That prefix is a routing detail of the
    client — the server itself advertises the bare name — and it only adds noise in
    the transcript, so it is stripped for display.

    Splits on the FIRST ``__`` after the prefix, matching how the MCP-server
    analytics view parses the same names (space_ui/js/views/sessions.js). A name
    that doesn't fit the pattern is passed through untouched rather than guessed at.
    """
    if not isinstance(raw, str) or not raw.startswith(_MCP_PREFIX):
        return raw
    rest = raw[len(_MCP_PREFIX):]
    server, sep, tool = rest.partition("__")
    if not sep or not tool:
        return raw
    return tool


def _normalize_content(msg: dict) -> str:
    """Extract text from a message's content for deduplication comparison."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def content_blocks(msg: dict) -> list[dict]:
    """Normalize a message's ``content`` into a list of typed block dicts.

    OpenClaw stores user messages with ``content`` as a plain string
    (e.g. ``"hi"``) but assistant messages as a list of typed blocks. Any code
    that iterates content blocks should go through this so the string form is
    coerced into a single text block and non-dict entries are dropped
    defensively — otherwise a string ``content`` is iterated
    character-by-character and ``str.get`` raises ``AttributeError`` (the 500 on
    GET /api/messages, and the same crash in the openclaw usage dashboard).

    Public because adapters reuse it (e.g. openclaw usage aggregation), the same
    way they reuse ``convert_messages``.
    """
    content = msg.get("content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def convert_messages(session_id: str, records: list[dict]) -> list[dict]:
    """Convert OpenClaw JSONL message records to xo-cowork MessageResponse format."""
    messages = []
    last_user_content: str | None = None

    for record in records:
        if record.get("type") != "message":
            continue

        msg = record.get("message", {})
        role = msg.get("role", "")
        record_id = record.get("id", short_id())
        timestamp = record.get("timestamp", iso_now())

        if role == "toolResult":
            _attach_tool_result(messages, msg)
            continue

        if role == "user":
            # Deduplicate consecutive user messages with identical content
            # (OpenClaw's bootstrap re-appends the user message after context loading)
            content_text = _normalize_content(msg)
            if content_text and content_text == last_user_content:
                continue
            last_user_content = content_text

            parts = _convert_user_parts(record_id, session_id, timestamp, msg)
            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"role": "user"},
                "parts": parts,
            })

        elif role == "assistant":
            parts = _convert_assistant_parts(record_id, session_id, timestamp, msg)
            usage = msg.get("usage", {})
            cost = usage.get("cost", {})
            cost_total = cost.get("total") if isinstance(cost, dict) else cost

            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "role": "assistant",
                    "model_id": msg.get("model"),
                    "provider_id": msg.get("provider"),
                    "cost": cost_total,
                    "tokens": {
                        "input": usage.get("input", 0),
                        "output": usage.get("output", 0),
                        "reasoning": 0,
                        "cache_read": usage.get("cacheRead", 0),
                        "cache_write": usage.get("cacheWrite", 0),
                    } if usage else None,
                    "finish": _map_stop_reason(msg.get("stopReason")),
                    "error": None,
                },
                "parts": parts,
            })

    return messages


def _convert_user_parts(msg_id, session_id, timestamp, msg):
    parts = []
    for block in content_blocks(msg):
        if block.get("type") == "text":
            # Strip the workspace-context preamble appended by the frontend
            # in project-scoped chats; the model reads it from the JSONL but
            # the UI shouldn't render it as part of the user bubble.
            text = strip_workspace_preamble(block.get("text", ""))
            if not text:
                continue
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": text},
            })
    return parts


def _convert_assistant_parts(msg_id, session_id, timestamp, msg):
    parts = []
    for block in content_blocks(msg):
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "")
            if text.startswith("[["):
                closing = text.find("]]")
                if closing != -1:
                    text = text[closing + 2:].strip()
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": text},
            })

        elif btype == "thinking":
            thinking_text = block.get("thinking", "")
            if thinking_text:
                parts.append({
                    "id": f"{msg_id}_p{len(parts)}",
                    "message_id": msg_id,
                    "session_id": session_id,
                    "time_created": timestamp,
                    "data": {"type": "reasoning", "text": thinking_text},
                })

        elif btype == "toolCall":
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "type": "tool",
                    "tool": _display_tool_name(block.get("name", "unknown")),
                    "call_id": block.get("id", ""),
                    "state": {
                        "status": "completed",
                        "input": block.get("arguments", {}),
                        "output": None,
                        "metadata": None,
                        "title": _display_tool_name(block.get("name", "tool")),
                        "time_start": timestamp,
                        "time_end": timestamp,
                        "time_compacted": None,
                    },
                },
            })

    return parts


def _attach_tool_result(messages, tool_result_msg):
    tool_call_id = tool_result_msg.get("toolCallId", "")
    output_text = ""
    for block in content_blocks(tool_result_msg):
        if block.get("type") == "text":
            output_text += block.get("text", "")

    for msg in reversed(messages):
        if msg["data"].get("role") != "assistant":
            continue
        for part in msg["parts"]:
            if (
                part["data"].get("type") == "tool"
                and part["data"].get("call_id") == tool_call_id
            ):
                part["data"]["state"]["output"] = output_text
                if tool_result_msg.get("isError"):
                    part["data"]["state"]["status"] = "error"
                return


def _map_stop_reason(reason):
    mapping = {
        "stop": "stop",
        "end_turn": "stop",
        "toolUse": "tool_use",
        "tool_use": "tool_use",
        "length": "length",
        "max_tokens": "length",
        "error": "error",
    }
    return mapping.get(reason or "", None)


def convert_native_claude_messages(session_id: str, records: list[dict]) -> list[dict]:
    """Convert native Claude Code JSONL records (type=user/assistant) to MessageResponse."""
    messages = []
    last_user_content: str | None = None
    saw_user_since_last_assistant = False  # reset merge chain on any user record

    for record in records:
        rtype = record.get("type")
        if rtype not in ("user", "assistant"):
            continue

        msg = record.get("message", {})
        if not msg:
            continue

        # Skip runtime-injected meta records (e.g. Skill payloads). The Claude
        # Code runtime tags them with isMeta=true and ties them to a tool call
        # via sourceToolUseID; the model needs them in history, but rendering
        # them as user bubbles surfaces multi-thousand-character internal
        # content into the chat. Skipping before flipping
        # saw_user_since_last_assistant lets the bracketing assistant blocks
        # (thinking → tool_use → post-skill text) merge into one rendered
        # turn, matching the user's mental model of a single assistant reply.
        if record.get("isMeta") is True:
            continue

        role = msg.get("role", rtype)
        record_id = record.get("uuid") or record.get("id") or short_id()
        timestamp = record.get("timestamp") or iso_now()

        if role == "user":
            saw_user_since_last_assistant = True
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
                tool_results = []
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            else:
                text = ""
                tool_results = []

            # Attach tool results to the preceding assistant message's tool calls.
            for tr in tool_results:
                call_id = tr.get("tool_use_id", "")
                result_content = tr.get("content", [])
                if isinstance(result_content, str):
                    output_text = result_content
                elif isinstance(result_content, list):
                    output_text = "".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    output_text = ""
                is_error = tr.get("is_error", False)
                for prev_msg in reversed(messages):
                    if prev_msg["data"].get("role") != "assistant":
                        continue
                    for part in prev_msg["parts"]:
                        if part["data"].get("type") == "tool" and part["data"].get("call_id") == call_id:
                            part["data"]["state"]["output"] = output_text
                            if is_error:
                                part["data"]["state"]["status"] = "error"
                            break

            # Strip the workspace-context preamble appended by the frontend
            # in project-scoped chats. The model still reads the full prompt
            # from the JSONL; this only affects what the UI shows.
            text = strip_workspace_preamble(text)

            if text and text == last_user_content:
                continue
            last_user_content = text or None

            if not text:
                # No visible text — skip; tool results already attached above.
                continue

            parts = [{
                "id": f"{record_id}_p0",
                "message_id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": text},
            }]
            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"role": "user"},
                "parts": parts,
            })

        elif role == "assistant":
            usage = msg.get("usage", {})
            new_parts = []
            for block in msg.get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text.startswith("[["):
                        closing = text.find("]]")
                        if closing != -1:
                            text = text[closing + 2:].strip()
                    if text:
                        new_parts.append({
                            "id": f"{record_id}_p{len(new_parts)}",
                            "message_id": record_id,
                            "session_id": session_id,
                            "time_created": timestamp,
                            "data": {"type": "text", "text": text},
                        })
                elif btype == "thinking":
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        new_parts.append({
                            "id": f"{record_id}_p{len(new_parts)}",
                            "message_id": record_id,
                            "session_id": session_id,
                            "time_created": timestamp,
                            "data": {"type": "reasoning", "text": thinking_text},
                        })
                elif btype == "tool_use":
                    new_parts.append({
                        "id": f"{record_id}_p{len(new_parts)}",
                        "message_id": record_id,
                        "session_id": session_id,
                        "time_created": timestamp,
                        "data": {
                            "type": "tool",
                            "tool": _display_tool_name(block.get("name", "unknown")),
                            "call_id": block.get("id", ""),
                            "state": {
                                "status": "completed",
                                "input": block.get("input", {}),
                                "output": None,
                                "metadata": None,
                                "title": _display_tool_name(block.get("name", "tool")),
                                "time_start": timestamp,
                                "time_end": timestamp,
                                "time_compacted": None,
                            },
                        },
                    })

            # Merge into the previous assistant message when consecutive records
            # belong to the same turn (thinking block followed by text/tool block).
            # Only merge when no user record (even tool-result-only) has appeared
            # since the last assistant message was created.
            prev = messages[-1] if messages else None
            if prev and prev["data"].get("role") == "assistant" and new_parts and not saw_user_since_last_assistant:
                prev["parts"].extend(new_parts)
                # Update usage/finish to the latest (more complete) record.
                if usage:
                    prev["data"]["tokens"] = {
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "reasoning": 0,
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    }
                if msg.get("stop_reason"):
                    prev["data"]["finish"] = _map_stop_reason(msg.get("stop_reason"))
                continue

            if not new_parts:
                # Empty assistant record (e.g. thinking block with no content) — skip.
                continue

            saw_user_since_last_assistant = False
            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "role": "assistant",
                    "model_id": msg.get("model"),
                    "provider_id": None,
                    "cost": None,
                    "tokens": {
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "reasoning": 0,
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    } if usage else None,
                    "finish": _map_stop_reason(msg.get("stop_reason")),
                    "error": None,
                },
                "parts": new_parts,
            })

    return messages
