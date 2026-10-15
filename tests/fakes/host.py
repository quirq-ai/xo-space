"""A ``Host`` for runtime tests: the contract's callbacks with scripted answers.

Its launcher is the real executor (``utils.commands.spawn_stream``), so a
runtime under test starts its fake CLI exactly as it will start the real one
in the engine. Everything the runtime asks is recorded for the test to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from services.cowork_agent.adapters.contract import (
    PermissionDecision,
    PermissionRequest,
    Question,
    ToolResult,
)
from utils.commands import StreamProcess, spawn_stream


class Launcher:
    """``contract.Launcher`` on the one executor; remembers what it started."""

    def __init__(self) -> None:
        self.spawned: list[StreamProcess] = []

    async def spawn(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], label: str) -> StreamProcess:
        handle = await spawn_stream(argv, cwd=cwd, env=env, label=label)
        self.spawned.append(handle)
        return handle


@dataclass
class ScriptedHost:
    """``decide`` answers each permission request (default: deny); ``answers``
    answers native questions; ``tools`` serves host tools by name."""

    decide: Callable[[PermissionRequest], PermissionDecision] = (
        lambda req: PermissionDecision(allow=False, message="denied by the test host"))
    answers: Mapping[str, Any] = field(default_factory=dict)
    tools: Mapping[str, Callable[[Mapping[str, Any]], ToolResult]] = field(default_factory=dict)
    launcher: Launcher = field(default_factory=Launcher)
    permission_requests: list[PermissionRequest] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    tool_calls: list[tuple[str, str, Mapping[str, Any]]] = field(default_factory=list)

    async def decide_permission(self, req: PermissionRequest) -> PermissionDecision:
        self.permission_requests.append(req)
        return self.decide(req)

    async def answer_question(self, q: Question) -> Mapping[str, Any]:
        self.questions.append(q)
        return dict(self.answers)

    async def call_tool(self, call_id: str, name: str, args: Mapping[str, Any]) -> ToolResult:
        self.tool_calls.append((call_id, name, args))
        serve = self.tools.get(name)
        if serve is None:
            return ToolResult(content=f"no host tool named {name}", is_error=True)
        return serve(args)
