"""agy's stream-json (``--input-format stream-json --output-format
stream-json``), as the fake speaks it.

Shapes from storm ch 04 §1, §7 and the live harness
(docs/sessions-router/notes/harnesses/agy/). One turn per stdin line
(``{"event": "user", ...}``); out come ``init``, ``step_update`` and one
``result`` per turn. There is no control channel: Stop is SIGINT, which ends
the turn as an error and exits the process (V8).
"""

from __future__ import annotations

from typing import Any

from tests.fakes import answer, expect

VERSION = "1.3.5"


def version() -> dict[str, Any]:
    return answer(["--version"], VERSION)


def user_message() -> dict[str, Any]:
    return expect({"event": "user"})


def init(conversation_id: str, *, cwd: str = "/work") -> dict[str, Any]:
    return {"emit": {"type": "init", "conversation_id": conversation_id, "cwd": cwd, "tools": [],
                     "permission_mode": "default"}}


def step(step_type: str, *, status: str = "DONE", **fields: Any) -> dict[str, Any]:
    return {"emit": {"type": "step_update", "step": {"step_type": step_type, "status": status, **fields}}}


def result(response: str, *, status: str = "success", denied_actions: list[str] | None = None) -> dict[str, Any]:
    return {"emit": {"type": "result", "status": status, "response": response, "num_turns": 1,
                     "denied_actions": denied_actions or [],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}}


def text_turn(text: str) -> list[dict[str, Any]]:
    return [step("user_input"), step("agent_response", text=text), result(text)]


def stops_on_sigint() -> dict[str, Any]:
    """From now on SIGINT ends the turn as ``ERROR "interrupted"``, with its
    usage, and the process exits (V8; its exit code wasn't recorded, so the
    fake uses 0)."""
    interrupted = {"type": "result", "status": "error", "error": "interrupted", "response": "",
                   "usage": {"input_tokens": 10, "output_tokens": 1}}
    return {"on_signal": "SIGINT", "steps": [{"emit": interrupted}], "exit": 0}
