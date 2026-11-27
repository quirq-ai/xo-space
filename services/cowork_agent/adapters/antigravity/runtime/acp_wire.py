"""agy's ACP ``session/update`` notifications as contract events.

``AcpWire`` keeps the stream-json ``Wire``'s bookkeeping (turns, open tool
calls, sub-agent runs, denied calls) and reads the Agent Client Protocol
instead (agy 1.5.15, live):

* ``agent_message_chunk`` / ``agent_thought_chunk``: text and thinking, one
  block until anything else (a tool call, a plan) comes between;
* ``tool_call`` then ``tool_call_update`` (``in_progress`` → ``pending``
  while agy waits for a permission → ``completed`` | ``failed``), with
  agy's own tool name, ``rawInput``, ``rawOutput`` and ``content`` (an
  edit's diff as data: path, old text, new text);
* ``plan``: the agent's plan entries;
* ``usage_update``: the context window after a model call;
* ``available_commands_update``, ``current_mode_update``,
  ``config_option_update``, ``session_info_update``,
  ``user_message_chunk`` (a load's replay): nothing to show.

A turn is one ``session/prompt``: the session starts it when it sends the
prompt (``begin_turn``) and ends it with the prompt's answer
(``prompt_done``: its ``stopReason`` and the turn's usage, sub-agents and
background work included). A call's id is agy's ``toolCallId``, the same id
XO's hook sees (``toolCall.id``), so an ask joins its call. Unknown update
kinds are one ``Notice`` each per session.
"""

from __future__ import annotations

import difflib
import re
import time
from collections import deque
from typing import Any, Mapping

from services.cowork_agent.adapters.antigravity.runtime.tools import one_line
from services.cowork_agent.adapters.antigravity.runtime.transcript import strip_header
from services.cowork_agent.adapters.antigravity.runtime.wire import DENIED, Out, Wire
from services.cowork_agent.adapters.contract import (
    PlanItem,
    PlanUpdated,
    TextDelta,
    TextDone,
    ThinkingDelta,
    ThinkingDone,
    TokenUsage,
    ToolFinished,
    Usage,
)

#: Updates that carry nothing to show.
QUIET_UPDATES = frozenset({"available_commands_update", "current_mode_update", "config_option_update",
                           "session_info_update", "user_message_chunk"})
#: agy's terminal sandbox couldn't start for a command (its log: ``sbox: mount / private: permission denied``).
SANDBOX_FAILED = "connecting to sandbox server"
_RAN = "Command failed to run:"
_EXIT = re.compile(r"^Command exited with code (-?\d+)\.?", re.M)
_COMMAND_OUTPUT = "Command output:"
THINKING_LINES = 20
_PLAN_STATES = {"pending": "todo", "in_progress": "doing", "completed": "done"}


def acp_usage(raw: object, context: tuple[int, int] | None) -> TokenUsage | None:
    """A prompt's ``usage`` (``inputTokens``, ``outputTokens``,
    ``cachedReadTokens``, ``cachedWriteTokens``, ``thoughtTokens``), with the
    context window last reported."""
    if not isinstance(raw, Mapping):
        return None

    def n(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and value >= 0 else 0
    used, size = context if context else (None, None)
    return TokenUsage(input=n("inputTokens"), output=n("outputTokens"), cache_read=n("cachedReadTokens"),
                      cache_write=n("cachedWriteTokens"), reasoning=n("thoughtTokens"), context_tokens=used,
                      context_window=size)


def _text_of(content: object) -> str:
    """The text of an ACP content block (``{type: text, text}``)."""
    if isinstance(content, Mapping) and content.get("type") == "text" and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def diff_stat(diffs: list[Mapping[str, Any]]) -> tuple[tuple[int, int] | None, int | None]:
    """Lines added and removed across a call's diffs, and the size of the
    last file written."""
    if not diffs:
        return None, None
    added = removed = 0
    size = None
    for d in diffs:
        old = d.get("oldText") if isinstance(d.get("oldText"), str) else ""
        new = d.get("newText") if isinstance(d.get("newText"), str) else ""
        for line in difflib.ndiff(old.splitlines(), new.splitlines()):
            if line.startswith("+ "):
                added += 1
            elif line.startswith("- "):
                removed += 1
        size = len(new.encode("utf-8"))
    return (added, removed), size


class AcpWire(Wire):
    def __init__(self, *, cwd: str, runs_detail: str = "steps", servers: tuple[str, ...] = (),
                 thinking: bool = True) -> None:
        super().__init__(cwd=cwd, runs_detail=runs_detail, servers=servers)
        self.thinking = thinking
        self.context: tuple[int, int] | None = None     # (used, size) after agy's last model call
        self.sandbox_failed = False                      # a command couldn't start in agy's terminal sandbox
        self._block: tuple[str, str, float] | None = None   # (kind, block id, started) of the open block
        self._parts: list[str] = []
        self._blocks = 0
        self._last_text: str | None = None
        # invoke_subagent calls whose run hasn't shown up yet: (call id, role, type)
        self._spawns: deque[tuple[str, str | None, str | None]] = deque(maxlen=16)

    # ── turns ────────────────────────────────────────────────────────────

    def begin_turn(self, origin: str | None = None) -> list[Out]:
        """The prompt for the oldest send goes out now (``origin``: a turn
        agy started on its own)."""
        self._last_text = None
        return self._start(origin)

    def prompt_done(self, result: Mapping[str, Any] | None, error: str | None) -> list[Out]:
        """The prompt's answer (or its JSON-RPC error): the turn ends."""
        outs = self.close_block()
        if self.current is None:
            return outs
        result = result or {}
        stop = result.get("stopReason")
        if error is not None:
            status = "failed"
        elif stop == "cancelled":
            status = "interrupted"
        elif stop == "refusal":
            status, error = "refused", "the model refused"
        elif stop in (None, "end_turn"):
            status = "completed"
        else:
            status, error = "failed", f"agy stopped the turn: {stop}"
        used = acp_usage(result.get("usage"), self.context)
        if used is not None:
            outs.append(Out(Usage, {"usage": used, "turn_id": self.current.id}))
        return outs + self._finish(status, self._last_text, None, error, used)

    def end_injected(self) -> list[Out]:
        """A turn agy started on its own (work after a prompt's answer) ends
        when its loop stops."""
        outs = self.close_block()
        if self.current is None:
            return outs
        return outs + self._finish("completed", self._last_text, None, None, None)

    # ── updates ──────────────────────────────────────────────────────────

    def update(self, u: Mapping[str, Any]) -> list[Out]:
        kind = u.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            text = _text_of(u.get("content"))
            if not text or (kind == "agent_thought_chunk" and not self.thinking):
                return []
            outs = self.begin_turn("injected") if self.current is None else []
            return outs + self._chunk("text" if kind == "agent_message_chunk" else "thought", text)
        if kind in QUIET_UPDATES:
            return []
        if kind == "usage_update":
            used, size = u.get("used"), u.get("size")
            if isinstance(used, int) and isinstance(size, int):
                self.context = (used, size)
            return []
        if kind in ("tool_call", "tool_call_update"):
            return self.close_block() + self._tool_update(u)
        if kind == "plan":
            entries = u.get("entries") if isinstance(u.get("entries"), list) else []
            items = tuple(PlanItem(text=one_line(str(e.get("content") or "")),
                                   state=_PLAN_STATES.get(str(e.get("status")), "todo"))
                          for e in entries if isinstance(e, Mapping))
            outs = self.close_block()
            if self.current is None:
                outs += self.begin_turn("injected")
            return outs + [Out(PlanUpdated, {"items": items, "turn_id": self.current_turn})]
        return self.unknown(f"session update {kind!r}")

    def _chunk(self, kind: str, text: str) -> list[Out]:
        outs: list[Out] = []
        if self._block is None or self._block[0] != kind:
            outs += self.close_block()
            self._blocks += 1
            block = f"{self.current_turn}:{self._blocks}" + (":t" if kind == "thought" else "")
            self._block = (kind, block, time.monotonic())
        self._parts.append(text)
        cls = TextDelta if kind == "text" else ThinkingDelta
        outs.append(Out(cls, {"block_id": self._block[1], "delta": text, "turn_id": self.current_turn}))
        return outs

    def close_block(self) -> list[Out]:
        """The open text or thinking block is done."""
        if self._block is None:
            return []
        (kind, block, started), parts = self._block, self._parts
        self._block, self._parts = None, []
        whole = "".join(parts).strip("\n")
        if not whole.strip():
            return []
        if kind == "text":
            self._last_text = whole
            return [Out(TextDone, {"block_id": block, "text": whole, "turn_id": self.current_turn})]
        lines = tuple(line.strip() for line in whole.splitlines() if line.strip())[:THINKING_LINES]
        return [Out(ThinkingDone, {"block_id": block, "duration_ms": int((time.monotonic() - started) * 1000),
                                   "steps": lines, "turn_id": self.current_turn})]

    def _tool_update(self, u: Mapping[str, Any]) -> list[Out]:
        call_id = u.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return self.unknown("a tool call without an id")
        outs: list[Out] = []
        if call_id not in self._tools and call_id not in self._finished:
            if self.current is None:
                outs += self.begin_turn("injected")
            name = str(u.get("name") or "tool")
            args = u.get("rawInput") if isinstance(u.get("rawInput"), Mapping) else u.get("args")
            outs += self.tool_started(call_id, name, args, self.current_turn, None)
            if name == "invoke_subagent":
                given = args if isinstance(args, Mapping) else {}
                role, type_name = given.get("Role"), given.get("TypeName")
                self._spawns.append((call_id, role if isinstance(role, str) else None,
                                     type_name if isinstance(type_name, str) else None))
        status = u.get("status")
        if status in ("completed", "failed"):
            outs += self._tool_end(call_id, u)
        return outs

    def _tool_end(self, call_id: str, u: Mapping[str, Any]) -> list[Out]:
        content = u.get("content") if isinstance(u.get("content"), list) else []
        diffs = [c for c in content if isinstance(c, Mapping) and c.get("type") == "diff"]
        texts = [_text_of(c.get("content")) for c in content if isinstance(c, Mapping) and c.get("type") == "content"]
        raw = u.get("rawOutput")
        output = raw.get("output") if isinstance(raw, Mapping) and isinstance(raw.get("output"), str) else \
            raw if isinstance(raw, str) else "\n".join(t for t in texts if t)
        if SANDBOX_FAILED in (output or ""):
            self.sandbox_failed = True
        if u.get("status") == "failed":
            message = (output or "").strip()
            status = "denied" if call_id in self.denied or DENIED.match(message) else "error"
            return self.tool_finished(call_id, status, one_line(message) if message else None)
        failed, summary = command_result(output or "", raw)
        stat, size = diff_stat(diffs)
        outs = self.tool_finished(call_id, "error" if failed else "ok", summary)
        if outs and (stat is not None or size is not None):
            fields = {**outs[0].fields, "diff_stat": stat, "size": size}
            outs[0] = Out(ToolFinished, fields)
        return outs

    # ── sub-agents ───────────────────────────────────────────────────────

    def subagent_started(self, conversation: str) -> list[Out]:
        """A conversation of agy's own sub-agents showed up (XO's hook saw it
        start): a run, spawned by the oldest ``invoke_subagent`` call not yet
        given one. ACP shows the spawn as one tool call; the run's own steps
        come from its transcript, as on stream-json."""
        if not conversation or conversation == self.conversation or self.is_run(conversation) \
                or conversation in self._runs_done:
            return []
        call_id, role, type_name = self._spawns.popleft() if self._spawns else (None, None, None)
        return self.runs_started(call_id, [{"conversation_id": conversation, "role": role, "type_name": type_name}])


def command_result(output: str, raw: object) -> tuple[bool, str | None]:
    """Whether a finished call failed, and the line that sums it up.

    agy reports a command that never ran (its sandbox wouldn't start) or that
    exited nonzero as *completed*; XO shows both as errors. The summary skips
    agy's ``Created At`` header: the command's first line of output, else what
    agy said about it."""
    body = strip_header(output).strip()
    code = raw.get("exit_code") if isinstance(raw, Mapping) else None
    if not isinstance(code, int) or isinstance(code, bool):
        found = _EXIT.search(body)
        code = int(found.group(1)) if found else None
    failed = body.startswith(_RAN) or (code is not None and code != 0)
    printed = body.split(_COMMAND_OUTPUT, 1)[1].strip() if _COMMAND_OUTPUT in body else ""
    if body.startswith(_RAN):
        return True, one_line(body)
    if printed:
        line = one_line(printed)
        return failed, f"exit {code}: {line}" if failed else line
    return failed, one_line(body) if body else None


__all__ = ["AcpWire", "command_result", "QUIET_UPDATES", "SANDBOX_FAILED", "acp_usage", "diff_stat"]
