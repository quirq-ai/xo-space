"""Claude Code's stream-json control protocol, as the fake speaks it.

Shapes from storm ch 02 §1–§4 and the live harness
(docs/sessions-router/notes/harnesses/claude/). Host → CLI: ``user`` messages
and ``control_request``s (``initialize``, ``interrupt``, …); CLI → host:
``system``/``stream_event``/``assistant``/``result`` lines and its own
``control_request``s (``can_use_tool``), which the host answers with a
``control_response``.
"""

from __future__ import annotations

import json
from typing import Any

from tests.fakes import answer, expect

VERSION = "2.1.300 (Claude Code)"
MODEL = "claude-haiku-4-5"


def version() -> dict[str, Any]:
    return answer(["--version"], VERSION)


def auth_status(*, logged_in: bool = True, auth_method: str = "claude.ai") -> dict[str, Any]:
    return answer(["auth", "status", "--json"],
                  json.dumps({"loggedIn": logged_in, "authMethod": auth_method if logged_in else "none"}))


def init(session_id: str, *, cwd: str = "/work", model: str = MODEL) -> dict[str, Any]:
    return {"emit": {"type": "system", "subtype": "init", "session_id": session_id, "cwd": cwd, "model": model,
                     "tools": ["Read", "Edit", "Bash"], "permissionMode": "default",
                     "claude_code_version": VERSION.split()[0]}}


def user_message() -> dict[str, Any]:
    """Expect the host's next user message, whatever its content blocks."""
    return expect({"type": "user", "message": {"role": "user"}})


def text_turn(session_id: str, text: str, *, message_id: str = "msg_1") -> list[dict[str, Any]]:
    """One answer streamed as deltas, its assistant message, then the result."""
    def ev(event: dict[str, Any]) -> dict[str, Any]:
        return {"emit": {"type": "stream_event", "session_id": session_id, "event": event}}
    return [
        ev({"type": "message_start", "message": {"id": message_id, "role": "assistant", "model": MODEL,
                                                 "content": [], "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ev({"type": "content_block_stop", "index": 0}),
        ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        ev({"type": "message_stop"}),
        {"emit": {"type": "assistant", "session_id": session_id,
                  "message": {"id": message_id, "role": "assistant", "content": [{"type": "text", "text": text}]}}},
        result(session_id, text),
    ]


def result(session_id: str, text: str, *, subtype: str = "success") -> dict[str, Any]:
    return {"emit": {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
                     "session_id": session_id, "num_turns": 1, "total_cost_usd": 0.0001,
                     "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}}


def idle(session_id: str) -> dict[str, Any]:
    """``SessionIdle`` comes only from this line (ch 02 §4.3)."""
    return {"emit": {"type": "system", "subtype": "session_state_changed", "session_id": session_id, "state": "idle"}}


def can_use_tool(request_id: str, tool: str, tool_input: dict[str, Any], *,
                 tool_use_id: str = "toolu_1", agent_id: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"subtype": "can_use_tool", "tool_name": tool, "input": tool_input,
                               "tool_use_id": tool_use_id, "permission_suggestions": []}
    if agent_id:
        request["agent_id"] = agent_id
    return {"emit": {"type": "control_request", "request_id": request_id, "request": request}}


def permission_answer(request_id: str, behavior: str) -> dict[str, Any]:
    """Expect the host's answer to a ``can_use_tool`` request."""
    return expect({"type": "control_response",
                   "response": {"subtype": "success", "request_id": request_id, "response": {"behavior": behavior}}})


def host_control(subtype: str) -> list[dict[str, Any]]:
    """Expect a host control request (``initialize``, ``interrupt``, …) and
    acknowledge it."""
    return [
        expect({"type": "control_request", "request": {"subtype": subtype}}, rid="request_id"),
        {"emit": {"type": "control_response", "response": {"subtype": "success", "request_id": "$rid", "response": {}}}},
    ]
