"""codex's app-server JSON-RPC (``codex app-server --listen stdio://``), as the
fake speaks it.

Shapes from storm ch 03 and the live harness
(docs/sessions-router/notes/harnesses/codex/). The client sends requests with
an ``id`` (``initialize``, ``thread/start``, ``turn/start``, …) and the
``initialized`` notification; the server answers by ``id``, streams
notifications (``turn/started``, ``item/*``, ``turn/completed``) and sends its
own requests (``item/*/requestApproval``), which the client answers by ``id``.
"""

from __future__ import annotations

from typing import Any

from tests.fakes import answer, expect

VERSION = "codex-cli 0.162.1"


def version() -> dict[str, Any]:
    return answer(["--version"], VERSION)


def call(method: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Expect a client request and answer it with ``result``."""
    return [expect({"method": method}, id="id"), {"emit": {"id": "$id", "result": result}}]


def handshake() -> list[dict[str, Any]]:
    return [*call("initialize", {"userAgent": "codex_cli_rs/0.162.1"}), expect({"method": "initialized"})]


def thread_start(thread_id: str) -> list[dict[str, Any]]:
    return call("thread/start", {"thread": {"id": thread_id}})


def turn_start(thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    return [*call("turn/start", {"turn": {"id": turn_id, "status": "inProgress"}}),
            notify("turn/started", {"threadId": thread_id, "turn": {"id": turn_id}})]


def notify(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"emit": {"method": method, "params": params}}


def agent_message(thread_id: str, turn_id: str, text: str, *, item_id: str = "item_1") -> list[dict[str, Any]]:
    item = {"type": "agentMessage", "id": item_id}
    return [
        notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": {**item, "text": ""}}),
        notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": text}),
        notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": {**item, "text": text}}),
    ]


def turn_completed(thread_id: str, turn_id: str, *, status: str = "completed") -> dict[str, Any]:
    return notify("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id, "status": status}})


def command_approval(request_id: int, thread_id: str, turn_id: str, command: str, *,
                     item_id: str = "item_cmd") -> dict[str, Any]:
    return {"emit": {"id": request_id, "method": "item/commandExecution/requestApproval",
                     "params": {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "command": command}}}


def approval_answer(request_id: int, decision: str) -> dict[str, Any]:
    """Expect the client's answer: ``accept``, ``acceptForSession``,
    ``decline`` or ``cancel`` (never ``acceptWithExecpolicyAmendment``)."""
    return expect({"id": request_id, "result": {"decision": decision}})
