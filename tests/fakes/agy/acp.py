"""``agy acp``: the Agent Client Protocol over stdio, as the fake speaks it.

Shapes from agy 1.5.15, recorded live (2026-11-23, an isolated HOME): the
client sends ``initialize``, ``session/new`` or ``session/load``,
``session/set_mode`` / ``set_model`` / ``set_config_option`` and one
``session/prompt`` per message, each answered by its ``id``, and the
``session/cancel`` notification. agy streams ``session/update``
notifications and sends its own ``session/request_permission`` requests,
which the client answers by ``id``. A prompt's answer is its turn's end:
``{stopReason, usage}``.

XO's hook runs under ``agy acp`` as under print mode (``hook`` steps, from
``tests.fakes.agy``); its ``toolCall`` carries agy's call id, the same
``toolCallId`` the updates use.
"""

from __future__ import annotations

from typing import Any

from tests.fakes import answer, expect
from tests.fakes import agy

VERSION = "1.5.15"
SID = agy.CONV
MODES = ["default", "accept-edits", "auto", "plan"]


def probes(version: str = VERSION) -> list[dict[str, Any]]:
    return [answer(["--version"], version), agy.plugin_list(), agy.models(), agy.usage()]


def call(method: str, result: Any, **match: Any) -> list[dict[str, Any]]:
    """Expect a client request (its params matching ``match``) and answer it."""
    request: dict[str, Any] = {"method": method}
    if match:
        request["params"] = match
    return [expect(request, id="id"), {"emit": {"jsonrpc": "2.0", "id": "$id", "result": result}}]


def refuse(method: str, message: str, code: int = -32002) -> list[dict[str, Any]]:
    return [expect({"method": method}, id="id"),
            {"emit": {"jsonrpc": "2.0", "id": "$id", "error": {"code": code, "message": message}}}]


def opened(sid: str = SID, *, mode: str = "default", model: str = "gemini-3.8-flash-high",
           level: str | None = "high") -> dict[str, Any]:
    """``session/new``'s answer (``session/load``'s, without the id)."""
    options: list[dict[str, Any]] = [{"id": "model", "category": "model", "type": "select",
                                      "currentValue": model.rsplit("-", 1)[0], "options": []}]
    if level is not None:
        options.append({"id": "thought_level", "category": "thought_level", "type": "select", "currentValue": level,
                        "options": [{"value": v, "name": v.title()} for v in ("low", "medium", "high")]})
    return {"sessionId": sid,
            "modes": {"currentModeId": mode, "availableModes": [{"id": m, "name": m} for m in MODES]},
            "models": {"currentModelId": model,
                       "availableModels": [{"modelId": i, "name": n} for i, n in agy.MODELS]},
            "configOptions": options,
            "_meta": {"antigravity": {"tier": {"name": "Google AI Pro", "id": "g1-pro-tier"}}}}


def initialize() -> list[dict[str, Any]]:
    return call("initialize", {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True, "embeddedContext": True},
                              "sessionCapabilities": {"resume": {}, "close": {}, "load": {}, "list": {},
                                                      "additionalDirectories": {}}},
        "authMethods": [{"id": "oauth-personal", "name": "Sign in with Google"}]},
        protocolVersion=1, clientInfo={"name": "xo_space", "title": "XO Space"})


def started(sid: str = SID, **state: Any) -> list[dict[str, Any]]:
    """A new session: the handshake and ``session/new``."""
    return [*initialize(), *call("session/new", opened(sid, **state)),
            update("available_commands_update", sid=sid, availableCommands=[{"name": "compact"}]),
            update("usage_update", sid=sid, used=0, size=1048576)]


def loaded(sid: str = SID, **state: Any) -> list[dict[str, Any]]:
    """A resumed session: the handshake, then ``session/load``, which replays
    the conversation before it answers."""
    body = {k: v for k, v in opened(sid, **state).items() if k != "sessionId"}
    return [*initialize(), expect({"method": "session/load", "params": {"sessionId": sid}}, id="id"),
            update("user_message_chunk", sid=sid, content={"type": "text", "text": "an old message"}),
            update("agent_message_chunk", sid=sid, content={"type": "text", "text": "an old answer"}),
            {"emit": {"jsonrpc": "2.0", "id": "$id", "result": body}}]


def update(kind: str, /, *, sid: str = SID, **fields: Any) -> dict[str, Any]:
    return {"emit": {"jsonrpc": "2.0", "method": "session/update",
                     "params": {"sessionId": sid, "update": {"sessionUpdate": kind, **fields}}}}


def prompt(text: str | None = None, *, bind: str = "pid") -> dict[str, Any]:
    """Expect the prompt for one message; its id is bound as ``bind``."""
    params: dict[str, Any] = {"sessionId": SID}
    if text is not None:
        params["prompt"] = [{"type": "text", "text": text}]
    return expect({"method": "session/prompt", "params": params}, **{bind: "id"})


def done(stop: str = "end_turn", *, bind: str = "pid", input_tokens: int = 1200, output_tokens: int = 40,
         cached: int = 300) -> dict[str, Any]:
    """The prompt's answer: its turn ends."""
    return {"emit": {"jsonrpc": "2.0", "id": f"${bind}", "result": {"stopReason": stop, "usage": {
        "inputTokens": input_tokens, "outputTokens": output_tokens, "cachedReadTokens": cached,
        "cachedWriteTokens": 0, "thoughtTokens": 7, "totalTokens": input_tokens + output_tokens + cached}}}}


def say(*chunks: str) -> list[dict[str, Any]]:
    return [update("agent_message_chunk", content={"type": "text", "text": c}) for c in chunks]


def think(*chunks: str) -> list[dict[str, Any]]:
    return [update("agent_thought_chunk", content={"type": "text", "text": c}) for c in chunks]


def tool_call(call_id: str, name: str, args: dict[str, Any], *, kind: str = "other",
              title: str | None = None) -> dict[str, Any]:
    return update("tool_call", toolCallId=call_id, name=name, args=args, rawInput=args, kind=kind,
                  title=title or name, status="in_progress")


def tool_done(call_id: str, name: str, args: dict[str, Any], *, status: str = "completed",
              output: str | None = None, diff: dict[str, Any] | None = None, kind: str = "other") -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if diff is not None:
        content.append({"type": "diff", **diff})
    if output is not None:
        content.append({"type": "content", "content": {"type": "text", "text": output}})
    fields: dict[str, Any] = {"toolCallId": call_id, "name": name, "args": args, "rawInput": args, "kind": kind,
                              "title": name, "status": status, "content": content}
    if output is not None:
        fields["rawOutput"] = {"output": output} if status == "completed" else output
    return update("tool_call_update", **fields)


def pre_tool(call_id: str, name: str, args: dict[str, Any], *, decision: str = "auto_approve", step: int = 2,
             conv: str = SID) -> dict[str, Any]:
    """XO's hook for one call, as ``agy acp`` runs it: ``toolCall.id`` is
    the ACP call id."""
    payload = {"conversationId": conv, "stepIdx": step, "toolCall": {"id": call_id, "name": name, "args": args},
               "workspacePaths": ["/work"], "modelName": "gemini-3.8-flash-high",
               "artifactDirectoryPath": f"/nowhere/brain/{conv}"}
    return agy.hook("PreToolUse", payload, expect_decision=decision, bind={"reason": "reason"})


def tool(call_id: str, name: str, args: dict[str, Any], *, decision: str = "auto_approve", output: str = "",
         diff: dict[str, Any] | None = None, kind: str = "other", step: int = 2) -> list[dict[str, Any]]:
    """One call: its start, XO's hook, then its end as the answer makes it."""
    end = tool_done(call_id, name, args, output=output, diff=diff, kind=kind) \
        if decision in ("auto_approve", "allow", "ask") else \
        tool_done(call_id, name, args, status="failed", output="tool call denied by pre-tool hook: $reason", kind=kind)
    return [tool_call(call_id, name, args, kind=kind), pre_tool(call_id, name, args, decision=decision, step=step),
            end]


def ask(call_id: str, name: str, args: dict[str, Any], *, rid: str = "perm-1", chosen: str | None = "allow",
        outcome: str = "selected") -> list[dict[str, Any]]:
    """agy asks before a call (its Auto mode wouldn't decide it); XO's
    answer must pick ``chosen`` (or, ``outcome="cancelled"``, cancel)."""
    asked = {"emit": {"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": SID,
        "toolCall": {"sessionUpdate": "tool_call", "toolCallId": call_id, "name": name, "args": args,
                     "rawInput": args, "status": "pending", "kind": "execute", "title": name},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}}}
    wanted: dict[str, Any] = {"outcome": outcome}
    if chosen is not None:
        wanted["optionId"] = chosen
    return [asked, update("tool_call_update", toolCallId=call_id, status="pending"),
            expect({"id": rid, "result": {"outcome": wanted}})]


def stop(*, fully_idle: bool = True, conv: str = SID) -> dict[str, Any]:
    return agy.stop(fully_idle=fully_idle, conv=conv)


def text_turn(words: str, *, text: str | None = None) -> list[dict[str, Any]]:
    """A whole prompt: its request, the words, the loop's stop, the answer."""
    return [prompt(text), *say(words), stop(), done()]
