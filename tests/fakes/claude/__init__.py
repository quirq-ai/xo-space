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
#: The flag only the runtime's models probe passes (storm's sessions persist).
PROBE_FLAG = "--no-session-persistence"
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


def streamed(session_id: str, message_id: str, blocks: list[tuple], *,
             agent_id: str | None = None) -> list[dict[str, Any]]:
    """One assistant message streamed with partial messages (``--include-
    partial-messages``), then the whole message. ``blocks``: ``("text",
    text)``, ``("thinking", text)`` (paragraphs split on blank lines) or
    ``("tool_use", call_id, name, input)``, whose JSON input streams in
    small fragments as claude's ``input_json_delta`` does."""
    def ev(event: dict[str, Any]) -> dict[str, Any]:
        line: dict[str, Any] = {"type": "stream_event", "session_id": session_id, "event": event}
        if agent_id:
            line["agent_id"] = agent_id
        return {"emit": line}
    steps = [ev({"type": "message_start", "message": {"id": message_id, "role": "assistant", "model": MODEL,
                                                     "content": [], "usage": {"input_tokens": 10}}})]
    content: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        kind = block[0]
        if kind == "tool_use":
            _, call_id, name, tool_input = block
            steps.append(ev({"type": "content_block_start", "index": index,
                             "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}}))
            raw = json.dumps(tool_input)
            steps += [ev({"type": "content_block_delta", "index": index,
                          "delta": {"type": "input_json_delta", "partial_json": raw[at:at + 16]}})
                      for at in range(0, len(raw), 16)]
            content.append({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
        else:
            text = block[1]
            steps.append(ev({"type": "content_block_start", "index": index,
                             "content_block": {"type": kind, kind: ""}}))
            delta_type, key = ("text_delta", "text") if kind == "text" else ("thinking_delta", "thinking")
            steps += [ev({"type": "content_block_delta", "index": index, "delta": {"type": delta_type,
                                                                                   key: text[at:at + 8]}})
                      for at in range(0, len(text), 8)]
            content.append({"type": kind, kind: text})
        steps.append(ev({"type": "content_block_stop", "index": index}))
    steps.append(ev({"type": "message_stop"}))
    whole: dict[str, Any] = {"type": "assistant", "session_id": session_id,
                             "message": {"id": message_id, "role": "assistant", "content": content}}
    if agent_id:
        whole["agent_id"] = agent_id
    steps.append({"emit": whole})
    return steps


def result(session_id: str, text: str, *, subtype: str = "success", terminal_reason: str | None = None,
           origin: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
        "session_id": session_id, "num_turns": 1, "total_cost_usd": 0.0001,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}
    if terminal_reason:
        line["terminal_reason"] = terminal_reason
    if origin:
        line["origin"] = {"kind": origin}
    return {"emit": line}


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


def initialize(*, version: str = VERSION.split()[0], mode: str = "default",
               models: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Expect ``initialize`` and answer it the way 2.1.301 does (the keys the
    runtime reads: version, starting permission mode, models)."""
    reply = {"claude_code_version": version, "current_permission_mode": mode,
             "models": models if models is not None else MODELS, "session_state": "idle"}
    return [
        expect({"type": "control_request", "request": {"subtype": "initialize"}}, rid="request_id"),
        {"emit": {"type": "control_response", "response": {"subtype": "success", "request_id": "$rid",
                                                           "response": reply}}},
    ]


#: The ``initialize`` reply's model rows, as 2.1.301 prints them (trimmed).
MODELS = [
    {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default (recommended)",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "resolvedModel": "claude-haiku-5-5", "displayName": "Haiku 5.5",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "claude-haiku-4-5-20251001", "resolvedModel": "claude-haiku-4-5-20251001",
     "displayName": "Haiku 4.5"},
]

_EVERY_EFFORT = ("low", "medium", "high", "xhigh", "max")
_FEWER_EFFORTS = ("low", "medium", "high", "max")


def _row(value: str, resolved: str, name: str, efforts: tuple[str, ...] = _EVERY_EFFORT) -> dict[str, Any]:
    """A model row; one with no ``efforts`` says nothing of effort (it takes none)."""
    return {"value": value, "resolvedModel": resolved, "displayName": name,
            **({"supportsEffort": True, "supportedEffortLevels": list(efforts)} if efforts else {})}


#: The whole catalog of a live 2.1.306 (B4's live run), every row, with the
#: keys the runtime reads: ``default``, an alias per family (``opus``,
#: ``fable``, ``sonnet``, ``haiku``), and the pinned ids.
LIVE_MODELS = [
    _row("default", "claude-opus-5-5", "Default (recommended)"),
    _row("opus", "claude-opus-5-5", "Opus 5.5"),
    _row("fable", "claude-fable-5-1", "Fable 5.1"),
    _row("sonnet", "claude-sonnet-5-5", "Sonnet 5.5"),
    _row("haiku", "claude-haiku-5-5", "Haiku 5.5"),
    _row("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", "Haiku 4.5", ()),
    _row("claude-sonnet-5", "claude-sonnet-5", "Sonnet 5"),
    _row("claude-opus-5", "claude-opus-5", "Opus 5"),
    _row("claude-fable-5", "claude-fable-5", "Fable 5"),
    _row("claude-opus-4-8", "claude-opus-4-8", "Opus 4.8"),
    _row("claude-opus-4-7", "claude-opus-4-7", "Opus 4.7"),
    _row("claude-opus-4-6", "claude-opus-4-6", "Opus 4.6", _FEWER_EFFORTS),
    _row("claude-sonnet-4-6", "claude-sonnet-4-6", "Sonnet 4.6", _FEWER_EFFORTS),
]


def tool_call(session_id: str, call_id: str, tool: str, tool_input: dict[str, Any], *,
              message_id: str = "msg_t", agent_id: str | None = None) -> dict[str, Any]:
    """The assistant message that starts a tool call."""
    line: dict[str, Any] = {"type": "assistant", "session_id": session_id,
                            "message": {"id": message_id, "role": "assistant",
                                        "content": [{"type": "tool_use", "id": call_id, "name": tool,
                                                     "input": tool_input}]}}
    if agent_id:
        line["agent_id"] = agent_id
    return {"emit": line}


def tool_result(session_id: str, call_id: str, content: str, *, is_error: bool = False,
                agent_id: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {"type": "user", "session_id": session_id,
                            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                                     "content": content, "is_error": is_error}]}}
    if agent_id:
        line["agent_id"] = agent_id
    return {"emit": line}


def system(session_id: str, subtype: str, **fields: Any) -> dict[str, Any]:
    return {"emit": {"type": "system", "subtype": subtype, "session_id": session_id, **fields}}


def task_started(session_id: str, task_id: str, *, tool_use_id: str = "toolu_task", description: str = "reader",
                 task_type: str = "local_agent", background: bool = True,
                 parent_task_id: str | None = None, depth: int = 1) -> dict[str, Any]:
    fields: dict[str, Any] = {"task_id": task_id, "tool_use_id": tool_use_id, "subagent_type": "Explore",
                              "task_type": task_type, "spawn_depth": depth, "is_backgrounded": background,
                              "description": description}
    if parent_task_id:
        fields["parent_task_id"] = parent_task_id
    return system(session_id, "task_started", **fields)


def task_progress(session_id: str, task_id: str, *, tokens: int = 100, tool_uses: int = 1) -> dict[str, Any]:
    return system(session_id, "task_progress", task_id=task_id, last_tool_name="Read",
                  usage={"total_tokens": tokens, "tool_uses": tool_uses, "duration_ms": 1200})


def task_killed(session_id: str, task_id: str) -> list[dict[str, Any]]:
    """What ``stop_task`` (or an interrupt) produces: both lines, as V14a saw."""
    return [system(session_id, "task_updated", task_id=task_id, patch={"status": "killed", "end_time": 1}),
            system(session_id, "task_notification", task_id=task_id, status="stopped", summary="reader")]


def rate_limit(status: str, *, resets_at: float | None = None, window: str = "five_hour",
               overage: bool = False) -> dict[str, Any]:
    info: dict[str, Any] = {"status": status, "rateLimitType": window, "isUsingOverage": overage,
                            "utilization": 0.9}
    if resets_at is not None:
        info["resetsAt"] = resets_at
    return {"emit": {"type": "rate_limit_event", "rate_limit_info": info}}


def ask_user_question(request_id: str, question: str, options: list[str], *,
                      tool_use_id: str = "toolu_q") -> dict[str, Any]:
    return can_use_tool(request_id, "AskUserQuestion", {"questions": [{
        "question": question, "header": "Pick", "multiSelect": False,
        "options": [{"label": o, "description": ""} for o in options]}]}, tool_use_id=tool_use_id)


def oneshot_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """``--print --output-format json``: one JSON object on stdout."""
    return {"emit": {"type": "result", "subtype": "success" if not is_error else "error_during_execution",
                     "is_error": is_error, "result": text, "session_id": "oneshot",
                     "total_cost_usd": 0.0002, "usage": {"input_tokens": 12, "output_tokens": 3}}}


def mcp_call(tool: str, arguments: dict[str, Any] | None = None, *, server: str = "xo_comms",
             bind: dict[str, str] | None = None) -> dict[str, Any]:
    """The CLI calling one of its MCP server's tools over HTTP, as claude
    does with the servers ``--mcp-config`` names. ``bind`` names values of
    the tool's result for later ``"$name"`` use."""
    step: dict[str, Any] = {"mcp": {"server": server, "tool": tool, "arguments": arguments or {}}}
    if bind:
        step["bind"] = bind
    return step
