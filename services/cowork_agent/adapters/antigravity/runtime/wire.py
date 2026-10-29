"""agy's stream-json output as contract events (storm ch 04 §7, §8).

``Wire`` keeps the parse state of one session (the root conversation, the
turn in progress, the text being streamed, the open tool calls, the
sub-agent runs) and turns each line agy prints into ``Out`` records: an
event class and its fields. The session stamps the envelope and delivers
them. It survives the session's restarts (an interrupt ends agy's process,
an update starts a new one), so the turns, runs and usage carry on.

Shapes (agy 1.4.10, live): every line is ``{"event": …, …}``:

* ``init {conversation_id, init: {model, cwd, tools, permission_mode}}``;
* ``step_update {step_update: {conversation_id, step_index, state
  (ACTIVE|DONE|ERROR), step_type, tool_name?, tool_info?, text_delta?,
  usage?, subagent_info?}}``;
* ``result {result: {conversation_id, status, response, error?, usage,
  num_turns, denied_actions?, structured_output?}}``.

Rules:

* agy has no turn ids: each ``send`` names one, and agy runs sends in order
  (a send while busy is queued as the next turn), so each ``user_input``
  step takes the oldest. A turn agy starts with no send (a sub-agent's
  report after the turn ended) is ``TurnStarted(origin="injected")``;
* a call's id is ``<conversation>:<step index>``, the same key XO's hook
  sees (``conversationId``, ``stepIdx``), so an ask joins its call;
* each ``result`` ends its turn; a tool call still open is ``cancelled``;
* ``result.usage`` counts the whole conversation, across agy's restarts:
  a turn's usage is the difference from the last result seen, or (the
  first turn of a resumed process) the sum of its own steps;
* a turn that ends with no answer after agy refused an action by its
  default policy is reported as stopped on a denied action;
* unknown wire data is a ``Notice`` (once per kind per session).
"""

from __future__ import annotations

import itertools
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from services.cowork_agent.adapters.antigravity.runtime.tools import describe, one_line
from services.cowork_agent.adapters.contract import (
    Event,
    Notice,
    RunFinished,
    RunInfo,
    RunProgress,
    RunStarted,
    TextDelta,
    TextDone,
    TokenUsage,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)

#: agy's own refusals, as a tool's error message: XO's hook, or agy's
#: default policy in headless mode.
DENIED = re.compile(r"^(tool call denied by pre-tool hook|permission check failed|"
                    r"unsupported hook decision|JSON hook \S+ failed)")
#: Step types that carry nothing to show: the root's own bookkeeping.
QUIET_STEPS = frozenset({"checkpoint", "system_message", "unknown", "ephemeral_message"})
SUMMARY_MAX = 200
TITLE_MAX = 80


@dataclass(frozen=True)
class Out:
    """One event to deliver: its class and fields (``turn_id`` and ``run``
    included where they apply); the session adds the envelope."""

    cls: type[Event]
    fields: dict[str, Any]


@dataclass
class _Tool:
    turn_id: str | None
    run: str | None
    tool: str


@dataclass
class _Run:
    info: RunInfo
    started: float = 0.0
    tool_calls: int = 0
    tokens: int = 0
    background: bool = False
    stopping: bool = False


@dataclass
class _Turn:
    id: str
    origin: str
    text: dict[int, list[str]] = field(default_factory=dict)
    steps_usage: TokenUsage = field(default_factory=TokenUsage)


def usage_of(raw: object) -> TokenUsage | None:
    if not isinstance(raw, Mapping):
        return None

    def n(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and value >= 0 else 0
    return TokenUsage(input=n("input_tokens"), output=n("output_tokens"), cache_read=n("cache_read_tokens"),
                      reasoning=n("thinking_tokens"), context_tokens=n("input_tokens") or None)


def _minus(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(input=max(a.input - b.input, 0), output=max(a.output - b.output, 0),
                      cache_read=max(a.cache_read - b.cache_read, 0), reasoning=max(a.reasoning - b.reasoning, 0))


def _plus(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(input=a.input + b.input, output=a.output + b.output, cache_read=a.cache_read + b.cache_read,
                      reasoning=a.reasoning + b.reasoning, context_tokens=b.context_tokens or a.context_tokens)


class Wire:
    def __init__(self, *, cwd: str, runs_detail: str = "steps") -> None:
        self.cwd = cwd
        self.runs_detail = runs_detail
        self.conversation: str | None = None
        self.current: _Turn | None = None
        self.denied: set[str] = set()            # call ids XO's hook refused
        self.baseline: TokenUsage | None = None  # the conversation's total at the last result seen
        self._expected: deque[tuple[str, str]] = deque()   # (turn id, origin) per send, in order
        self._tools: dict[str, _Tool] = {}
        self._runs: dict[str, _Run] = {}
        self._runs_done: set[str] = set()
        self._unknown: set[str] = set()
        self._finished: deque[str] = deque(maxlen=512)   # calls already finished (a late line is no new call)
        self._ids = itertools.count(1)
        self._prefix = secrets.token_hex(3)

    # ── sends ────────────────────────────────────────────────────────────

    @property
    def current_turn(self) -> str | None:
        return self.current.id if self.current else None

    def new_turn_id(self) -> str:
        return f"agy-{self._prefix}-{next(self._ids)}"

    def expect_turn(self, origin: str) -> str:
        """A send: the turn agy will start for it, in order."""
        turn_id = self.new_turn_id()
        self._expected.append((turn_id, origin))
        return turn_id

    def never_started(self, why: str) -> list[Out]:
        """Sends agy will never run (its process was stopped first): each is
        a turn that ends at once, interrupted."""
        outs = []
        while self._expected:
            turn_id, origin = self._expected.popleft()
            outs += [Out(TurnStarted, {"origin": origin, "turn_id": turn_id}),
                     Out(TurnFinished, {"status": "interrupted", "text": None, "structured": None, "error": why,
                                        "usage": None, "turn_id": turn_id})]
        return outs

    def drop_expected(self, turn_id: str) -> None:
        self._expected = deque(e for e in self._expected if e[0] != turn_id)

    @property
    def pending(self) -> int:
        """Sends agy hasn't started yet."""
        return len(self._expected)

    def pending_turns(self) -> list[str]:
        return [t for t, _ in self._expected]

    # ── lines ────────────────────────────────────────────────────────────

    def line(self, msg: Mapping[str, Any]) -> list[Out]:
        event = msg.get("event")
        if event == "step_update" and isinstance(msg.get("step_update"), Mapping):
            return self.step(msg["step_update"])
        if event == "result" and isinstance(msg.get("result"), Mapping):
            return self.result(msg["result"])
        if event == "init":
            return []                           # the session's: the native id and model
        return self.unknown(f"event {event!r}")

    def unknown(self, what: str) -> list[Out]:
        if what in self._unknown:
            return []
        self._unknown.add(what)
        return [Out(Notice, {"code": "unknown_wire", "message": f"agy sent {what}, which XO doesn't read",
                             "turn_id": self.current_turn})]

    def _start(self, origin: str | None = None) -> list[Out]:
        if self._expected and origin is None:
            turn_id, origin = self._expected.popleft()
        else:
            turn_id, origin = self.new_turn_id(), origin or "injected"
        self.current = _Turn(id=turn_id, origin=origin)
        return [Out(TurnStarted, {"origin": origin, "turn_id": turn_id})]

    def step(self, s: Mapping[str, Any]) -> list[Out]:
        conv = s.get("conversation_id") if isinstance(s.get("conversation_id"), str) else self.conversation
        kind = s.get("step_type")
        index = s.get("step_index") if isinstance(s.get("step_index"), int) else -1
        if conv and self.conversation and conv != self.conversation:
            return self.child_step(conv, s)
        outs: list[Out] = []
        if kind == "user_input":
            if self.current is not None:
                outs += self._finish("completed", None, None, None)
            return outs + self._start()
        if self.current is None:
            if kind in QUIET_STEPS and kind != "system_message":
                return []
            outs += self._start("injected")
        turn = self.current
        if kind == "agent_response":
            return outs + self._text(turn, index, s)
        if kind in ("tool", "subagent"):
            return outs + self._tool(turn, conv, index, s)
        if kind in QUIET_STEPS:
            return outs
        return outs + self.unknown(f"step type {kind!r}")

    def _text(self, turn: _Turn, index: int, s: Mapping[str, Any]) -> list[Out]:
        outs: list[Out] = []
        delta = s.get("text_delta")
        block = f"{turn.id}:{index}"
        if isinstance(delta, str) and delta:
            turn.text.setdefault(index, []).append(delta)
            outs.append(Out(TextDelta, {"block_id": block, "delta": delta, "turn_id": turn.id}))
        if s.get("state") in ("DONE", "ERROR"):
            used = usage_of(s.get("usage"))
            if used is not None:
                turn.steps_usage = _plus(turn.steps_usage, used)
                outs.append(Out(Usage, {"usage": turn.steps_usage, "turn_id": turn.id}))
            parts = turn.text.pop(index, None)
            text = "".join(parts or ()).strip("\n")
            if text:
                outs.append(Out(TextDone, {"block_id": block, "text": text, "turn_id": turn.id}))
        return outs

    def _tool(self, turn: _Turn, conv: str | None, index: int, s: Mapping[str, Any]) -> list[Out]:
        info = s.get("tool_info") if isinstance(s.get("tool_info"), Mapping) else {}
        name = s.get("tool_name") or info.get("name") or s.get("step_type") or "tool"
        call_id = f"{conv}:{index}"
        outs: list[Out] = []
        if call_id not in self._tools and call_id not in self._finished:
            # its ACTIVE line, or a finished call whose ACTIVE line agy never printed
            outs += self.tool_started(call_id, str(name), info.get("parameters"), turn.id, None)
        if s.get("state") in ("DONE", "ERROR"):
            error = info.get("error") if isinstance(info.get("error"), Mapping) else None
            subagents = (s.get("subagent_info") or {}).get("subagents") if isinstance(s.get("subagent_info"),
                                                                                    Mapping) else None
            if error is not None or s.get("state") == "ERROR":
                message = str((error or {}).get("message") or "")
                status = "denied" if call_id in self.denied or DENIED.match(message) else "error"
                outs += self.tool_finished(call_id, status, one_line(message) if message else None)
            else:
                output = info.get("output")
                outs += self.tool_finished(call_id, "ok", one_line(output) if isinstance(output, str) and output
                                           else None)
            if isinstance(subagents, list):
                outs += self.runs_started(call_id, subagents)
        return outs

    def tool_started(self, call_id: str, tool: str, params: object, turn_id: str | None, run: str | None) -> list[Out]:
        call = describe(tool, params if isinstance(params, Mapping) else {}, self.cwd)
        self._tools[call_id] = _Tool(turn_id=turn_id, run=run, tool=tool)
        if run is not None and run in self._runs:
            self._runs[run].tool_calls += 1
        return [Out(ToolStarted, {"call_id": call_id, "tool": tool, "kind": call.kind, "title": call.title,
                                  "target": call.target, "turn_id": turn_id, "run": run})]

    def tool_finished(self, call_id: str, status: str, summary: str | None) -> list[Out]:
        tool = self._tools.pop(call_id, None)
        if tool is None:
            return []
        self._finished.append(call_id)
        self.denied.discard(call_id)
        return [Out(ToolFinished, {"call_id": call_id, "status": status, "summary": summary,
                                   "turn_id": tool.turn_id, "run": tool.run})]

    def open_call(self, call_id: str) -> bool:
        return call_id in self._tools

    # ── the end of a turn ────────────────────────────────────────────────

    def result(self, r: Mapping[str, Any]) -> list[Out]:
        if self.current is None:
            outs = self._start("injected") if not self._expected else self._start()
        else:
            outs = []
        status_raw = str(r.get("status") or "").upper()
        error = r.get("error") if isinstance(r.get("error"), str) and r.get("error") else None
        if status_raw == "SUCCESS":
            status = "completed"
        elif status_raw in ("CANCELED", "CANCELLED", "INTERRUPTED") or error == "interrupted":
            status, error = "interrupted", None
        else:
            status = "failed"
        text = r.get("response") if isinstance(r.get("response"), str) else None
        text = text.strip("\n") if text else None
        denied = r.get("denied_actions") if isinstance(r.get("denied_actions"), list) else []
        if status == "completed" and not text and denied:
            names = ", ".join(sorted({str(d.get("display_name") or d.get("action")) for d in denied
                                      if isinstance(d, Mapping)}))
            error = f"agy stopped on an action it refused by its own policy ({names})"
        total = usage_of(r.get("usage"))
        turn = self.current
        if total is not None and self.baseline is not None:
            used = _minus(total, self.baseline)
        else:
            used = turn.steps_usage if turn else None
        if total is not None:
            self.baseline = total
        return outs + self._finish(status, text, r.get("structured_output"), error, used)

    def _finish(self, status: str, text: str | None, structured: Any, error: str | None,
                usage: TokenUsage | None = None) -> list[Out]:
        turn = self.current
        if turn is None:
            return []
        outs: list[Out] = []
        for index, parts in sorted(turn.text.items()):
            cut = "".join(parts).strip("\n")
            if cut:
                outs.append(Out(TextDone, {"block_id": f"{turn.id}:{index}", "text": cut, "turn_id": turn.id}))
        turn.text.clear()
        outs += self.cancel_tools(root_only=True)
        outs += self.background_runs()
        outs.append(Out(TurnFinished, {"status": status, "text": text, "structured": structured, "error": error,
                                       "usage": usage, "turn_id": turn.id}))
        self.current = None
        return outs

    def close_turn(self, status: str, error: str | None) -> list[Out]:
        """The process ended mid-turn: the turn ends as it stood."""
        return self._finish(status, None, None, error, self.current.steps_usage if self.current else None)

    def cancel_tools(self, *, root_only: bool = False) -> list[Out]:
        outs = []
        for call_id, tool in list(self._tools.items()):
            if root_only and tool.run is not None:
                continue
            outs += self.tool_finished(call_id, "denied" if call_id in self.denied else "cancelled", None)
        return outs

    # ── sub-agents ───────────────────────────────────────────────────────

    def runs_started(self, call_id: str, subagents: list[Any]) -> list[Out]:
        outs = []
        for sub in subagents:
            if not isinstance(sub, Mapping) or not isinstance(sub.get("conversation_id"), str):
                continue
            run_id = sub["conversation_id"]
            if run_id in self._runs or run_id in self._runs_done:
                continue
            role = sub.get("role") or sub.get("type_name") or "sub-agent"
            info = RunInfo(run_id=run_id, parent_run_id=None, spawn_call_id=call_id, kind="subagent",
                           agent_type=sub.get("type_name") if isinstance(sub.get("type_name"), str) else None,
                           title=one_line(str(role), TITLE_MAX), background=False, depth=1)
            self._runs[run_id] = _Run(info=info, started=time.monotonic())
            outs.append(Out(RunStarted, {"info": info, "run": run_id, "turn_id": self.current_turn}))
        return outs

    def live_runs(self) -> list[str]:
        return list(self._runs)

    def run_title(self, run_id: str | None) -> str | None:
        found = self._runs.get(run_id) if run_id else None
        return found.info.title if found else None

    def is_run(self, conversation: str | None) -> bool:
        return bool(conversation) and conversation in self._runs

    def child_step(self, conv: str, s: Mapping[str, Any]) -> list[Out]:
        """A step of a sub-agent's conversation on stdout (agy streams none
        today; its steps come from its transcript)."""
        if conv not in self._runs:
            return self.unknown("a step of a conversation this session didn't start")
        if s.get("step_type") == "tool":
            info = s.get("tool_info") if isinstance(s.get("tool_info"), Mapping) else {}
            call_id = f"{conv}:{s.get('step_index')}"
            outs: list[Out] = []
            if call_id not in self._tools and call_id not in self._finished:
                outs += self.tool_started(call_id, str(s.get("tool_name") or info.get("name") or "tool"),
                                          info.get("parameters"), self.current_turn, conv)
            if s.get("state") in ("DONE", "ERROR"):
                outs += self.tool_finished(call_id, "error" if s.get("state") == "ERROR" else "ok", None)
            return outs
        return []

    def background_runs(self) -> list[Out]:
        """The root's turn ends while sub-agents still run: they go on in the
        background (said once)."""
        outs = []
        for run_id, run in self._runs.items():
            if not run.background:
                run.background = True
                outs.append(Out(RunProgress, {"tokens": run.tokens, "tool_calls": run.tool_calls,
                                              "duration_ms": int((time.monotonic() - run.started) * 1000),
                                              "background": True, "run": run_id, "turn_id": self.current_turn}))
        return outs

    def run_progress(self, run_id: str, last_tool: str | None) -> list[Out]:
        """A run's numbers so far, after its transcript showed new steps."""
        run = self._runs.get(run_id)
        if run is None:
            return []
        return [Out(RunProgress, {"tokens": run.tokens, "tool_calls": run.tool_calls,
                                  "duration_ms": int((time.monotonic() - run.started) * 1000),
                                  "last_tool": last_tool, "run": run_id, "turn_id": self.current_turn})]

    def run_tokens(self, run_id: str, tokens: int) -> None:
        if run_id in self._runs:
            self._runs[run_id].tokens += max(tokens, 0)

    def mark_stopping(self, run_id: str) -> None:
        if run_id in self._runs:
            self._runs[run_id].stopping = True

    def finish_run(self, run_id: str, status: str, summary: str | None) -> list[Out]:
        run = self._runs.pop(run_id, None)
        if run is None:
            return []
        self._runs_done.add(run_id)
        outs = [*self._cancel_run_tools(run_id)]
        outs.append(Out(RunFinished, {"status": "stopped" if run.stopping and status != "completed" else status,
                                      "summary": one_line(summary) if summary else None, "run": run_id,
                                      "turn_id": self.current_turn}))
        return outs

    def _cancel_run_tools(self, run_id: str) -> list[Out]:
        outs = []
        for call_id, tool in list(self._tools.items()):
            if tool.run == run_id:
                outs += self.tool_finished(call_id, "cancelled", None)
        return outs

    def end_live_runs(self, summary: str) -> list[Out]:
        outs = []
        for run_id in list(self._runs):
            outs += self.finish_run(run_id, "stopped", summary)
        return outs
