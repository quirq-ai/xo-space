"""codex's app-server JSON-RPC (``codex app-server --listen stdio://``), as the
fake speaks it.

Shapes from codex-cli 0.162.1, recorded live (storm C1). The client sends
requests with an ``id`` (``initialize``, ``thread/start``, ``turn/start``, …)
and the ``initialized`` notification; the server answers by ``id``, streams
notifications (``turn/started``, ``item/*``, ``thread/status/changed``,
``turn/completed``) and sends its own requests (``item/*/requestApproval``),
numbered from 0, which the client answers by ``id``. codex reports the
thread ``idle`` just *before* its turn's ``turn/completed``.
"""

from __future__ import annotations

from typing import Any

from tests.fakes import answer, expect

VERSION = "codex-cli 0.162.1"
USER_AGENT = "xo_space/0.162.1 (Ubuntu 26.4.0; x86_64) unknown (xo_space; 1)"
MODEL = "gpt-test-luna"
#: The models probe's catalog: what ``model/list`` answered live, trimmed.
LIVE_MODELS = [
    {"id": "gpt-6.1-sol", "model": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol", "hidden": False,
     "description": "Latest workhorse model for coding and everyday work.", "isDefault": True,
     "defaultReasoningEffort": "medium",
     "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e}
                                   for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"id": "gpt-6-luna", "model": "gpt-6-luna", "displayName": "GPT-6-Luna", "hidden": False,
     "description": "Small and fast.", "isDefault": False, "defaultReasoningEffort": "low",
     "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e}
                                   for e in ("low", "medium", "high", "xhigh", "max")]},
    {"id": "codex-internal", "model": "codex-internal", "displayName": "Internal", "hidden": True,
     "supportedReasoningEfforts": []},
]


def version() -> dict[str, Any]:
    return answer(["--version"], VERSION)


def call(method: str, result: dict[str, Any], **match: Any) -> list[dict[str, Any]]:
    """Expect a client request (its params matching ``match``) and answer it
    with ``result``."""
    request: dict[str, Any] = {"method": method}
    if match:
        request["params"] = match
    return [expect(request, id="id"), {"emit": {"id": "$id", "result": result}}]


def refuse(method: str, message: str, code: int = -32600) -> list[dict[str, Any]]:
    return [expect({"method": method}, id="id"), {"emit": {"id": "$id", "error": {"code": code, "message": message}}}]


def notify(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"emit": {"method": method, "params": params}}


def handshake() -> list[dict[str, Any]]:
    return [*call("initialize", {"userAgent": USER_AGENT, "codexHome": "/x/.codex", "platformFamily": "unix",
                                 "platformOs": "linux"}),
            expect({"method": "initialized"})]


def thread(thread_id: str, *, parent: str | None = None, status: str = "idle", ephemeral: bool = False,
           path: str | None = None) -> dict[str, Any]:
    return {"id": thread_id, "sessionId": thread_id, "parentThreadId": parent, "ephemeral": ephemeral,
            "model": MODEL, "status": {"type": status}, "path": path, "cliVersion": "0.162.1", "turns": []}


def thread_start(thread_id: str, *, method: str = "thread/start", model: str = MODEL,
                 **match: Any) -> list[dict[str, Any]]:
    """The thread's start (``thread/resume`` / ``thread/fork`` by ``method``)."""
    return [*call(method, {"thread": thread(thread_id), "model": model, "approvalPolicy": "untrusted",
                           "sandbox": {"type": "dangerFullAccess"}}, **match),
            notify("thread/started", {"thread": thread(thread_id)})]


def started(thread_id: str, **match: Any) -> list[dict[str, Any]]:
    """A session's start: the handshake and its thread."""
    return [*handshake(), *thread_start(thread_id, **match)]


def status(thread_id: str, kind: str, *flags: str) -> dict[str, Any]:
    body: dict[str, Any] = {"type": kind}
    if kind == "active":
        body["activeFlags"] = list(flags)
    return notify("thread/status/changed", {"threadId": thread_id, "status": body})


def turn_obj(turn_id: str, *, status: str = "inProgress", root: str | None = None,
             error: dict | None = None, items: list | None = None) -> dict[str, Any]:
    return {"id": turn_id, "rootTurnId": root or turn_id, "items": items or [], "itemsView": "notLoaded",
            "status": status, "error": error}


def turn_start(thread_id: str, turn_id: str, **match: Any) -> list[dict[str, Any]]:
    """Expect ``turn/start`` and begin the turn."""
    return [*call("turn/start", {"turn": turn_obj(turn_id)}, threadId=thread_id, **match),
            status(thread_id, "active"),
            notify("turn/started", {"threadId": thread_id, "turn": turn_obj(turn_id)})]


def child_turn(child: str, turn_id: str, root_turn: str) -> list[dict[str, Any]]:
    return [status(child, "active"),
            notify("turn/started", {"threadId": child, "turn": turn_obj(turn_id, root=root_turn)})]


def item(method: str, thread_id: str, turn_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return notify(method, {"threadId": thread_id, "turnId": turn_id, "item": body})


def agent_message(thread_id: str, turn_id: str, text: str, *, item_id: str = "msg_1",
                  phase: str = "final_answer") -> list[dict[str, Any]]:
    body = {"type": "agentMessage", "id": item_id, "phase": phase, "memoryCitation": None, "delivery": None,
            "questions": None}
    return [item("item/started", thread_id, turn_id, {**body, "text": ""}),
            notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                                               "delta": text}),
            item("item/completed", thread_id, turn_id, {**body, "text": text})]


def reasoning(thread_id: str, turn_id: str, parts: list[str], *, item_id: str = "rs_1") -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [item("item/started", thread_id, turn_id,
                                        {"type": "reasoning", "id": item_id, "summary": [], "content": []})]
    for index, part in enumerate(parts):
        if index:
            steps.append(notify("item/reasoning/summaryPartAdded", {"threadId": thread_id, "turnId": turn_id,
                                                                     "itemId": item_id, "summaryIndex": index}))
        steps.append(notify("item/reasoning/summaryTextDelta", {"threadId": thread_id, "turnId": turn_id,
                                                                 "itemId": item_id, "summaryIndex": index,
                                                                 "delta": part}))
    steps.append(item("item/completed", thread_id, turn_id,
                      {"type": "reasoning", "id": item_id, "summary": parts, "content": []}))
    return steps


def command_item(item_id: str, command: str, *, cwd: str = "/work", status: str = "inProgress",
                 output: str | None = None, exit_code: int | None = None) -> dict[str, Any]:
    return {"type": "commandExecution", "id": item_id, "command": f"/bin/bash -lc '{command}'", "cwd": cwd,
            "processId": None, "source": "agent", "status": status,
            "commandActions": [{"type": "unknown", "command": command}], "aggregatedOutput": output,
            "exitCode": exit_code, "durationMs": None}


def command_started(thread_id: str, turn_id: str, item_id: str, command: str) -> dict[str, Any]:
    return item("item/started", thread_id, turn_id, command_item(item_id, command))


def command_completed(thread_id: str, turn_id: str, item_id: str, command: str, output: str = "", *,
                      status: str = "completed", exit_code: int | None = 0) -> dict[str, Any]:
    return item("item/completed", thread_id, turn_id,
                command_item(item_id, command, status=status, output=output, exit_code=exit_code))


def output_delta(thread_id: str, turn_id: str, item_id: str, delta: str) -> dict[str, Any]:
    return notify("item/commandExecution/outputDelta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                                                         "delta": delta})


def command_approval(request_id: int, thread_id: str, turn_id: str, item_id: str, command: str, *,
                     cwd: str = "/work") -> dict[str, Any]:
    return {"emit": {"id": request_id, "method": "item/commandExecution/requestApproval", "params": {
        "kind": "command", "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "environmentId": "local",
        "command": f"/bin/bash -lc '{command}'", "cwd": cwd,
        "commandActions": [{"type": "unknown", "command": command}],
        "proposedExecpolicyAmendment": command.split(),
        "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": command.split()}},
                               "cancel"]}}}


def change_item(item_id: str, changes: list[dict[str, Any]], *, status: str = "inProgress") -> dict[str, Any]:
    return {"type": "fileChange", "id": item_id, "changes": changes, "status": status}


def add(path: str, text: str) -> dict[str, Any]:
    return {"path": path, "kind": {"type": "add"}, "diff": text}


def update(path: str, diff: str) -> dict[str, Any]:
    return {"path": path, "kind": {"type": "update", "move_path": None}, "diff": diff}


def change_started(thread_id: str, turn_id: str, item_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    return item("item/started", thread_id, turn_id, change_item(item_id, changes))


def change_completed(thread_id: str, turn_id: str, item_id: str, changes: list[dict[str, Any]], *,
                     status: str = "completed") -> dict[str, Any]:
    return item("item/completed", thread_id, turn_id, change_item(item_id, changes, status=status))


def change_approval(request_id: int, thread_id: str, turn_id: str, item_id: str) -> dict[str, Any]:
    return {"emit": {"id": request_id, "method": "item/fileChange/requestApproval", "params": {
        "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "reason": None, "grantRoot": None}}}


def approval_answer(request_id: int, decision: str) -> dict[str, Any]:
    """Expect the client's answer: ``accept``, ``acceptForSession``,
    ``decline`` or ``cancel`` (never ``acceptWithExecpolicyAmendment``)."""
    return expect({"id": request_id, "result": {"decision": decision}})


def resolved(thread_id: str, request_id: int) -> dict[str, Any]:
    return notify("serverRequest/resolved", {"threadId": thread_id, "requestId": request_id})


def steer(thread_id: str, turn_id: str, **match: Any) -> list[dict[str, Any]]:
    """Expect ``turn/steer`` into ``turn_id`` and accept it."""
    return call("turn/steer", {"turnId": turn_id}, threadId=thread_id, expectedTurnId=turn_id, **match)


def interrupted(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    """Expect ``turn/interrupt``; the turn ends interrupted."""
    return [*call("turn/interrupt", {}, threadId=thread_id, turnId=turn_id),
            status(thread_id, "idle"),
            notify("turn/completed", {"threadId": thread_id, "turn": turn_obj(turn_id, status="interrupted")})]


def token_usage(thread_id: str, turn_id: str, *, total: dict[str, int], last: dict[str, int] | None = None,
                window: int = 258_400) -> dict[str, Any]:
    def breakdown(counts: dict[str, int]) -> dict[str, int]:
        full = {"inputTokens": 0, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 0,
                "reasoningOutputTokens": 0}
        full.update(counts)
        full["totalTokens"] = full["inputTokens"] + full["outputTokens"]
        return full
    return notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {
        "total": breakdown(total), "last": breakdown(last or total), "modelContextWindow": window}})


def rate_limits(used: float, *, resets_at: int = 1_793_312_017, minutes: int = 10_080,
                has_credits: bool = False) -> dict[str, Any]:
    return notify("account/rateLimits/updated", {"rateLimits": limits(used, resets_at=resets_at, minutes=minutes,
                                                                      has_credits=has_credits)})


def limits(used: float, *, resets_at: int = 1_793_312_017, minutes: int = 10_080,
           has_credits: bool = False) -> dict[str, Any]:
    return {"limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets_at},
            "secondary": None, "credits": {"hasCredits": has_credits, "unlimited": False, "balance": "0"},
            "planType": "pro", "rateLimitReachedType": None}


def turn_completed(thread_id: str, turn_id: str, *, status: str = "completed",
                   error: dict | None = None) -> dict[str, Any]:
    return notify("turn/completed", {"threadId": thread_id, "turn": turn_obj(turn_id, status=status, error=error)})


def turn_end(thread_id: str, turn_id: str, *, status_: str = "completed") -> list[dict[str, Any]]:
    """The end of a turn as codex sends it: idle first, then the turn."""
    return [status(thread_id, "idle"), turn_completed(thread_id, turn_id, status=status_)]


def text_turn(thread_id: str, turn_id: str, text: str, *, item_id: str = "msg_1",
              **match: Any) -> list[dict[str, Any]]:
    """A whole turn that only answers ``text``."""
    return [*turn_start(thread_id, turn_id, **match), *agent_message(thread_id, turn_id, text, item_id=item_id),
            *turn_end(thread_id, turn_id)]


def spawned(parent: str, turn_id: str, child: str, path: str, *, call_id: str = "call_spawn") -> list[dict[str, Any]]:
    body = {"type": "subAgentActivity", "model": MODEL, "reasoningEffort": "low", "id": call_id, "kind": "started",
            "agentThreadId": child, "agentPath": path}
    return [status(child, "idle"), item("item/started", parent, turn_id, body),
            item("item/completed", parent, turn_id, body)]


def child_done(parent: str, turn_id: str, child: str, path: str, child_turn_id: str) -> list[dict[str, Any]]:
    body = {"type": "subAgentActivity", "model": None, "reasoningEffort": None,
            "id": f"subagent-completed-{child_turn_id}", "kind": "completed", "agentThreadId": child,
            "agentPath": path}
    return [status(child, "idle"), item("item/started", parent, turn_id, body),
            notify("turn/completed", {"threadId": child, "turn": turn_obj(child_turn_id, status="completed",
                                                                          root=turn_id)}),
            item("item/completed", parent, turn_id, body)]


def models_probe(models: list[dict[str, Any]] | None = None, *, used: float = 10.0) -> list[dict[str, Any]]:
    """What the runtime's models probe plays: the handshake, every model,
    the account's rate limits."""
    return [*handshake(), *call("model/list", {"data": models if models is not None else LIVE_MODELS,
                                               "nextCursor": None}),
            *call("account/rateLimits/read", {"rateLimits": limits(used), "rateLimitsByLimitId": {}})]
