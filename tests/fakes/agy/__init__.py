"""agy's stream-json (``--input-format stream-json --output-format
stream-json``), as the fake speaks it: agy 1.4.10's shapes, read live
(``/home/coder/scratch/c2v2/live/FINDINGS.md``).

One turn per stdin line (``{"event": "user", "message": {"content": …}}``);
every output line is ``{"event": …}``: ``init``, ``step_update`` and one
``result`` per turn. There is no control channel: Stop is SIGINT, which ends
the turn as an error and exits the process with code 1.

agy reaches XO through XO's plugin (``hook``: the plugin's hook command, run
as agy would, with the event's JSON on stdin; its answer decides the call),
so a scenario plays the hook where agy would call it, and expects the answer
XO should give (``expect_decision``).
"""

from __future__ import annotations

import json
from typing import Any

from tests.fakes import answer, expect

VERSION = "1.4.10"
CONV = "6a36a4da-4871-4ab5-94e4-2a62440bef88"
MODELS = [("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"), ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
          ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"), ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
          ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"), ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)")]
USAGE = ("Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-05T13:43:17Z\n"
         "Gemini Models\tFive Hour Limit Remaining\t99%\t2026-10-29T18:43:17Z\n"
         "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-05T14:14:47Z\n"
         "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-10-29T19:14:47Z")


def version() -> dict[str, Any]:
    return answer(["--version"], VERSION)


def plugin_list(*, enabled: bool = True, present: bool = True) -> dict[str, Any]:
    rows = [{"name": "xo", "enabled": enabled, "path": "~/.gemini/config/plugins/xo", "mcpServers": [],
             "hooks": ["xo"]}] if present else []
    return answer(["plugin", "list", "--json"], json.dumps(rows))


def models() -> dict[str, Any]:
    return answer(["models"], "Fetching available models...\n" + "\n".join(f"{i}\t{n}" for i, n in MODELS))


def usage(text: str = USAGE) -> dict[str, Any]:
    return answer(["-p", "/usage"], text)


def probes() -> list[dict[str, Any]]:
    """The model-free calls a runtime makes before a session."""
    return [version(), plugin_list(), models(), usage()]


def user_message() -> dict[str, Any]:
    return expect({"event": "user"})


def init(conversation_id: str = CONV, *, cwd: str = "/work", model: str = "gemini-3.8-flash-low") -> dict[str, Any]:
    return {"emit": {"event": "init", "conversation_id": conversation_id,
                     "init": {"model": model, "cwd": cwd, "tools": ["run_command", "write_to_file", "view_file"],
                              "permission_mode": "request-review"}}}


def step(index: int, step_type: str, *, state: str = "DONE", conv: str = CONV, **fields: Any) -> dict[str, Any]:
    return {"emit": {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": index,
                                                              "state": state, "step_type": step_type, **fields}}}


def text(index: int, words: str, *, conv: str = CONV) -> list[dict[str, Any]]:
    return [step(index, "agent_response", state="ACTIVE", conv=conv, text_delta=words),
            step(index, "agent_response", conv=conv, text_delta="\n",
                 usage={"input_tokens": 100, "output_tokens": 5, "thinking_tokens": 0, "cache_read_tokens": 0,
                        "total_tokens": 105})]


def hook(event: str, payload: dict[str, Any], *, expect_decision: str | None = None,
         bind: dict[str, str] | None = None) -> dict[str, Any]:
    """Run XO's hook for ``event`` as agy would; ``expect_decision`` fails the
    scenario on any other answer; ``bind`` names answer values for later."""
    out: dict[str, Any] = {"hook": {"event": event, "payload": payload}}
    if expect_decision is not None:
        out["expect_decision"] = expect_decision
    if bind:
        out["bind"] = bind
    return out


def pre_tool(index: int, name: str, args: dict[str, Any], *, conv: str = CONV, decision: str = "auto_approve",
             artifacts: str | None = None) -> dict[str, Any]:
    payload = {"conversationId": conv, "stepIdx": index, "toolCall": {"name": name, "args": args},
               "workspacePaths": ["/work"], "modelName": "gemini-3.8-flash-low",
               "artifactDirectoryPath": artifacts or f"/nowhere/brain/{conv}"}
    return hook("PreToolUse", payload, expect_decision=decision, bind={"reason": "reason"})


def tool(index: int, name: str, params: dict[str, Any], *, output: str = "", conv: str = CONV,
         decision: str = "auto_approve", args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One tool call: its ACTIVE line, XO's hook, then its end as the hook's
    answer makes it."""
    started = step(index, "tool", state="ACTIVE", conv=conv, tool_name=name,
                   tool_info={"name": name, "parameters": params})
    asked = pre_tool(index, name, args if args is not None else params, conv=conv, decision=decision)
    if decision == "auto_approve":
        ended = step(index, "tool", conv=conv, tool_name=name, tool_info={"name": name, "parameters": params,
                                                                         "output": output})
    else:
        ended = step(index, "tool", state="ERROR", conv=conv, tool_name=name, tool_info={
            "name": name, "parameters": params,
            "error": {"type": "TOOL_ERROR", "message": "tool call denied by pre-tool hook: $reason"}})
    return [started, asked, ended]


def result(response: str, *, status: str = "SUCCESS", conv: str = CONV, error: str | None = None,
           input_tokens: int = 200, output_tokens: int = 10, denied: list[dict] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"conversation_id": conv, "status": status, "response": response, "duration_seconds": 1.0,
                            "num_turns": 1, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                                      "thinking_tokens": 0, "cache_read_tokens": 0,
                                                      "total_tokens": input_tokens + output_tokens}}
    if error:
        body["error"] = error
    if denied:
        body["denied_actions"] = denied
    return {"emit": {"event": "result", "result": body}}


def stop(*, fully_idle: bool = True, conv: str = CONV) -> dict[str, Any]:
    """agy's Stop hook, just before a turn's result: its loop ended, and
    (``fullyIdle``) nothing it started still runs."""
    return hook("Stop", {"conversationId": conv, "executionNum": 0, "terminationReason": "NO_TOOL_CALL",
                         "fullyIdle": fully_idle})


def end(words: str, *, conv: str = CONV, **kw: Any) -> list[dict[str, Any]]:
    """A turn's end: the Stop hook, then the result."""
    return [stop(conv=conv), result(words, conv=conv, **kw)]


def text_turn(words: str, *, first: int = 0, conv: str = CONV) -> list[dict[str, Any]]:
    """A whole turn after its message was read: the input, the words, the end."""
    return [step(first, "user_input", conv=conv), *text(first + 1, words, conv=conv), *end(words, conv=conv)]


def stops_on_sigint(conv: str = CONV) -> dict[str, Any]:
    """From now on SIGINT ends the turn as ``ERROR "interrupted"``, with its
    usage, and the process exits 1 (V8, agy 1.4.10)."""
    interrupted = {"event": "result", "result": {"conversation_id": conv, "status": "ERROR", "response": "",
                                                 "error": "interrupted", "num_turns": 1,
                                                 "usage": {"input_tokens": 50, "output_tokens": 1}}}
    return {"on_signal": "SIGINT", "steps": [{"emit": interrupted}, {"stderr": "error: interrupted"}], "exit": 1}


def session_start(*, bind: str = "prompt") -> dict[str, Any]:
    """agy's SessionStart hook at a new conversation's first message: XO
    answers with the role's prompt as a system message."""
    return hook("SessionStart", {"conversationId": CONV}, bind={bind: "injectSteps.0.systemMessage.systemMessage"})


def pre_invocation(text_sent: str, *, bind: str = "context") -> dict[str, Any]:
    return hook("PreInvocation", {"conversationId": CONV, "invocationNum": 0, "lastUserInput": text_sent},
                bind={bind: "injectSteps.0.ephemeralMessage"})
