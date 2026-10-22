"""claude's stream-json output as contract events (storm ch 02 §4.3–§5).

``Wire`` keeps the parse state of one session (the turn in progress, the
blocks being streamed, the open tool calls, the sub-agent runs) and turns each
line claude prints into ``Out`` records: an event class and its fields. The
session stamps the envelope (``seq``, ``ts``, ids) and delivers them. Control
requests and responses are the session's; everything else is read here.

Rules this follows:

* one ``TurnFinished`` per ``result``: one input can give two results (a
  background worker finishing wakes a turn), so a turn claude starts on its
  own is ``TurnStarted(origin="injected")``;
* ``SessionIdle`` only from ``session_state_changed: idle``;
* a tool call still open when its turn ends is finished as ``cancelled``, so
  every ``ToolStarted`` is finished;
* a text or thinking block cut off by an interrupt is recorded as it stood
  (``TextDone`` / ``ThinkingDone``) before the turn's ``TurnFinished``:
  claude stops streaming it without a ``content_block_stop``, and the part
  the person watched must survive in history;
* unknown wire data is reported as a ``Notice`` (once per kind per session),
  never dropped silently and never a crash;
* ``ToolInputDelta`` comes *before* its ``ToolStarted``: claude streams a
  call's input (``input_json_delta``) before the whole ``tool_use`` block, and
  only the whole block has the path or command that ``ToolStarted`` must
  carry. A presenter keys the growing "Writing … N KB" row by ``call_id`` and
  fills in its title when ``ToolStarted`` arrives; no delta follows the
  ``ToolFinished``;
* every event carries the root turn in progress when its line arrived
  (``turn_id``, None between turns) and, when a sub-agent produced it, that
  run (``run`` = claude's ``agent_id``/``task_id``).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.cowork_agent.adapters.claude_code.runtime.tools import (
    CONTENT_KEYS,
    describe_call,
    one_line,
)
from services.cowork_agent.adapters.contract import (
    Error,
    Event,
    Notice,
    PlanItem,
    PlanUpdated,
    RateLimit,
    RunFinished,
    RunInfo,
    RunKind,
    RunProgress,
    RunSnapshot,
    RunStarted,
    RunState,
    SessionIdle,
    TextDelta,
    TextDone,
    ThinkingDelta,
    ThinkingDone,
    TokenUsage,
    ToolFinished,
    ToolInputDelta,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)

#: A run's title is one line of at most this many characters (contract RunInfo).
RUN_TITLE_MAX = 80

#: claude's task types as the contract's run kinds (ch 02 §4.5).
RUN_KINDS: dict[str, RunKind] = {
    "local_agent": "subagent",
    "in_process_teammate": "teammate",
    "local_workflow": "workflow_agent",
    "local_bash": "background_command",
    "remote_agent": "remote_agent",
    "monitor": "monitor",
    "dream": "internal",
    "ambient": "internal",
}
HIDDEN_TASK_TYPES = frozenset({"dream", "ambient"})

#: How each terminal run status reads in the contract.
RUN_END = {"completed": "completed", "failed": "failed", "stopped": "stopped", "killed": "stopped"}
SNAPSHOT_STATES = {"running": "running", "pending": "running", "waiting": "waiting", "completed": "finished",
                   "failed": "failed", "killed": "stopped", "stopped": "stopped"}

RATE_STATUS = {"allowed": "ok", "allowed_warning": "warning", "rejected": "limited"}
PLAN_STATES = {"pending": "todo", "in_progress": "doing", "completed": "done"}
ABORTED = frozenset({"aborted_tools", "aborted_streaming"})

#: The diagnostic ``system`` lines claude prints, as Notice codes.
#: HTTP statuses of an API failure worth trying again later (ch 07 §5.1: overload
#: and 5xx re-queue with backoff). 529 is the API's overload. 429 is safe here
#: because a usage window's 429 comes with a ``rejected`` rate_limit_event,
#: which closes the engine's pool until the reset first: the re-queued input
#: waits for it instead of looping.
RETRYABLE_STATUS = frozenset({429, 529}) | frozenset(range(500, 600))
OVERLOADED_STATUS = 529
SYSTEM_NOTICES = {"api_retry": "api_retry", "compact_boundary": "compacted", "model_fallback": "model_fallback",
                  "permission_denied": "permission_denied"}
#: ``system`` lines that carry nothing the contract needs (seen live: on 2.1.301
#: a request-status ping and a running count of thinking tokens; on 2.1.303 and
#: 2.1.304 ``commands_changed`` and ``vcs_state_changed``, which no event reports).
SYSTEM_IGNORED = frozenset({"status", "thinking_tokens", "commands_changed", "vcs_state_changed"})
#: ``stream_event`` types that carry nothing the contract needs.
STREAM_IGNORED = frozenset({"message_delta", "message_stop", "ping"})
#: Delta types inside a content block that carry nothing the contract needs.
DELTA_IGNORED = frozenset({"signature_delta", "citations_delta"})


@dataclass(frozen=True)
class Out:
    """One event to deliver: its class and fields (``turn_id`` and ``run``
    included where they apply); the session adds the envelope."""

    cls: type[Event]
    fields: dict[str, Any]


def iso_from_epoch(seconds: object) -> str | None:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContentCounter:
    """Counts the decoded UTF-8 bytes of one string field of a JSON object
    that arrives in fragments (``input_json_delta``), without keeping the
    content: the deltas are JSON-escaped, so their raw length overstates it."""

    def __init__(self, key: str) -> None:
        self._key = f'"{key}"'
        self._buf = ""
        self._state = "seek"            # seek → value → done
        self._escape = ""
        self.count = 0

    def feed(self, fragment: str) -> int:
        if self._state == "seek":
            fragment = self._seek(fragment)
        if self._state == "value":
            self._scan(fragment)
        return self.count

    def _seek(self, fragment: str) -> str:
        self._buf += fragment
        start = 0
        while True:
            at = self._buf.find(self._key, start)
            if at < 0:
                self._buf = self._buf[-len(self._key):]
                return ""
            rest = self._buf[at + len(self._key):]
            stripped = rest.lstrip()
            if not stripped or (stripped[0] == ":" and not stripped[1:].lstrip()):
                self._buf = self._buf[at:]          # the rest of the key's syntax hasn't arrived yet
                return ""
            if stripped[0] != ":":
                start = at + 1                      # a value equal to the key's name, not the key
                continue
            value = stripped[1:].lstrip()
            if value[0] != '"':
                self._state = "done"                # not a string: nothing to count
                return ""
            self._state, self._buf = "value", ""
            return value[1:]

    def _scan(self, text: str) -> None:
        for ch in text:
            if self._escape:
                self._escape += ch
                if self._escape == "\\u" or (self._escape.startswith("\\u") and len(self._escape) < 6):
                    continue
                if self._escape.startswith("\\u"):
                    code = int(self._escape[2:], 16) if re.fullmatch(r"[0-9a-fA-F]{4}", self._escape[2:]) else 0
                    # A surrogate pair is one 4-byte character: count it on the high half.
                    self.count += 0 if 0xDC00 <= code <= 0xDFFF else 4 if 0xD800 <= code <= 0xDBFF \
                        else len(chr(code).encode("utf-8"))
                else:
                    self.count += 1
                self._escape = ""
            elif ch == "\\":
                self._escape = "\\"
            elif ch == '"':
                self._state = "done"
                return
            else:
                self.count += len(ch.encode("utf-8", "surrogatepass"))


@dataclass
class _Block:
    kind: str                           # text | thinking | tool_use | other
    block_id: str
    text: str = ""
    started: float = field(default_factory=time.monotonic)
    call_id: str | None = None
    counter: ContentCounter | None = None


@dataclass
class _OpenTool:
    turn_id: str | None
    run: str | None
    tool: str
    size: int | None


class Wire:
    """The parse state of one session's output."""

    def __init__(self, *, cwd: str, runs_detail: str, coordinator: bool) -> None:
        self.cwd = cwd
        self.runs_detail = runs_detail
        self.coordinator = coordinator
        self.current_turn: str | None = None
        self.model: str | None = None
        self.denied: set[str] = set()              # tool calls the host denied
        self._inits = 0
        self._message: dict[str | None, str] = {}  # run -> the message being streamed
        self._blocks: dict[tuple[str | None, str, int], _Block] = {}
        self._streamed: set[str] = set()           # messages already delivered from the stream
        self._tools: dict[str, _OpenTool] = {}
        self._runs: dict[str, RunInfo] = {}
        self._runs_by_call: dict[str, str] = {}
        self._runs_done: set[str] = set()
        self._unknown: set[str] = set()
        self._retry_status: int | None = None      # an api_retry's HTTP status the API hasn't answered since

    # ── turns ────────────────────────────────────────────────────────────

    def run_title(self, run: str | None) -> str | None:
        """The title of a sub-agent this session has started (``task_started``)."""
        info = self._runs.get(run) if run else None
        return info.title if info else None

    def start_turn(self, origin: str) -> tuple[str, list[Out]]:
        """A turn this side starts (``send``) or claude starts on its own."""
        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        self.current_turn = turn_id
        return turn_id, [Out(TurnStarted, {"turn_id": turn_id, "origin": origin})]

    def _activity(self, run: str | None) -> list[Out]:
        """Root activity with no turn open means claude started one itself."""
        if run is None and self.current_turn is None:
            return self.start_turn("injected")[1]
        return []

    def close_turn(self, *, status: str, error: str | None) -> list[Out]:
        """End the open turn when the process is gone (closed, crashed)."""
        if self.current_turn is None:
            return []
        outs = self._cancel_open_tools(self.current_turn)
        if status == "interrupted":
            outs += self._cut_off_blocks(self.current_turn)
        outs.append(Out(TurnFinished, {"turn_id": self.current_turn, "status": status, "error": error}))
        self.current_turn = None
        return outs

    def _cut_off_blocks(self, turn_id: str | None) -> list[Out]:
        """The root's text and thinking blocks an interrupt left open, as the
        person saw them stream; an empty one was never shown and is dropped."""
        outs = []
        for key, block in list(self._blocks.items()):
            if key[0] is not None or block.kind not in ("text", "thinking"):
                continue
            del self._blocks[key]
            if not block.text:
                continue
            if block.kind == "text":
                outs.append(Out(TextDone, {"block_id": block.block_id, "text": block.text, "turn_id": turn_id}))
            else:
                outs.append(Out(ThinkingDone, {
                    "block_id": block.block_id, "steps": thinking_steps(block.text), "turn_id": turn_id,
                    "duration_ms": int((time.monotonic() - block.started) * 1000)}))
        return outs

    def cancel_all_tools(self) -> list[Out]:
        """Every call still open when the process ends will never finish."""
        outs = [Out(ToolFinished, {"call_id": cid, "status": "cancelled", "turn_id": t.turn_id, "run": t.run})
                for cid, t in self._tools.items()]
        self._tools.clear()
        return outs

    def end_live_runs(self, summary: str) -> list[Out]:
        """Runs still going when their process ends are stopped with it."""
        live = [run for run in self._runs if run not in self._runs_done]
        self._runs_done.update(live)
        return [Out(RunFinished, {"run": run, "status": "stopped", "summary": summary, "turn_id": None})
                for run in live]

    def _cancel_open_tools(self, turn_id: str) -> list[Out]:
        ended = [cid for cid, t in self._tools.items() if t.turn_id == turn_id and t.run is None]
        return [Out(ToolFinished, {"call_id": cid, "status": "cancelled", "turn_id": turn_id,
                                   "run": self._tools.pop(cid).run}) for cid in ended]

    # ── lines ────────────────────────────────────────────────────────────

    def line(self, msg: dict[str, Any]) -> list[Out]:
        kind = msg.get("type")
        if kind == "system":
            return self._system(msg)
        if kind == "stream_event":
            return self._stream(msg)
        if kind == "assistant":
            return self._assistant(msg)
        if kind == "user":
            return self._user(msg)
        if kind == "result":
            return self._result(msg)
        if kind == "rate_limit_event":
            return self._rate_limit(msg)
        return self.unknown(f"{kind}")

    def unknown(self, what: str) -> list[Out]:
        if what in self._unknown:
            return []
        self._unknown.add(what)
        return [Out(Notice, {"code": "unknown_wire", "turn_id": self.current_turn,
                             "message": f"claude sent a line this runtime doesn't know ({what}); "
                                        "further lines of this kind are not reported again"})]

    def _run_of(self, msg: dict[str, Any]) -> str | None:
        agent = msg.get("agent_id")
        if isinstance(agent, str) and agent:
            return agent
        parent = msg.get("parent_tool_use_id")
        return self._runs_by_call.get(parent) if isinstance(parent, str) else None

    def _shown(self, run: str | None, kind: str) -> bool:
        """Whether a run's ``kind`` (``tool`` or ``text``) events stream, per
        ``runs_detail``. The root's always do."""
        if run is None:
            return True
        return self.runs_detail == "text" or (self.runs_detail == "steps" and kind == "tool")

    def _system(self, msg: dict[str, Any]) -> list[Out]:
        sub = msg.get("subtype")
        if sub == "init":
            outs = self._activity(None)
            model = msg.get("model") if isinstance(msg.get("model"), str) else None
            if self._inits and model and self.model and model != self.model:
                outs.append(Out(Notice, {"code": "model_changed", "turn_id": self.current_turn,
                                         "message": f"claude now runs {model} (was {self.model})"}))
            self._inits += 1
            self.model = model or self.model
            return outs
        if sub == "session_state_changed":
            return [Out(SessionIdle, {})] if msg.get("state") == "idle" else []
        if sub in ("task_started", "task_progress", "task_notification", "task_updated", "background_tasks_changed"):
            return self._task(sub, msg)
        if sub in SYSTEM_IGNORED:
            return []
        if sub == "api_retry" and isinstance(msg.get("error_status"), int):
            self._retry_status = msg["error_status"]
        if sub in SYSTEM_NOTICES:
            detail = {k: msg[k] for k in ("attempt", "error", "error_status", "tool_name", "reason", "trigger")
                      if k in msg}
            return [Out(Notice, {"code": SYSTEM_NOTICES[sub], "turn_id": self.current_turn,
                                 "run": self._run_of(msg), "message": f"claude: {sub} {json.dumps(detail)[:300]}"})]
        return self.unknown(f"system/{sub}")

    # ── streaming ────────────────────────────────────────────────────────

    def _stream(self, msg: dict[str, Any]) -> list[Out]:
        run = self._run_of(msg)
        ev = msg.get("event") if isinstance(msg.get("event"), dict) else {}
        kind = ev.get("type")
        outs = self._activity(run)
        turn = self.current_turn
        if kind == "message_start":
            if run is None:
                self._retry_status = None   # the API answered: an earlier api_retry recovered
            message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            message_id = str(message.get("id") or uuid.uuid4().hex)
            self._message[run] = message_id
            self._streamed.add(message_id)
            return outs
        message_id = self._message.get(run, "")
        index = ev.get("index") if isinstance(ev.get("index"), int) else -1
        key = (run, message_id, index)
        if kind == "content_block_start":
            block = ev.get("content_block") if isinstance(ev.get("content_block"), dict) else {}
            btype = block.get("type")
            state = _Block(kind=btype if btype in ("text", "thinking", "tool_use") else "other",
                           block_id=f"{message_id}:{index}")
            if btype == "tool_use":
                state.call_id = block.get("id") if isinstance(block.get("id"), str) else None
                content_key = CONTENT_KEYS.get(block.get("name") or "")
                state.counter = ContentCounter(content_key) if content_key else None
            self._blocks[key] = state
            return outs
        state = self._blocks.get(key)
        if kind == "content_block_delta":
            delta = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
            dtype = delta.get("type")
            if state is None or dtype in DELTA_IGNORED:
                return outs
            if dtype == "text_delta" and isinstance(delta.get("text"), str):
                state.text += delta["text"]
                if self._shown(run, "text"):
                    outs.append(Out(TextDelta, {"block_id": state.block_id, "delta": delta["text"],
                                                "turn_id": turn, "run": run}))
            elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
                state.text += delta["thinking"]
                if self._shown(run, "text"):
                    outs.append(Out(ThinkingDelta, {"block_id": state.block_id, "delta": delta["thinking"],
                                                    "step_title": _step_title(state.text),
                                                    "turn_id": turn, "run": run}))
            elif dtype == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                if state.counter is not None and state.call_id and self._shown(run, "tool"):
                    before = state.counter.count
                    after = state.counter.feed(delta["partial_json"])
                    if after != before:
                        outs.append(Out(ToolInputDelta, {"call_id": state.call_id, "bytes_so_far": after,
                                                         "turn_id": turn, "run": run}))
            else:
                outs += self.unknown(f"stream_event/content_block_delta/{dtype}")
            return outs
        if kind == "content_block_stop":
            self._blocks.pop(key, None)
            if state is not None and self._shown(run, "text"):
                if state.kind == "text":
                    outs.append(Out(TextDone, {"block_id": state.block_id, "text": state.text,
                                               "turn_id": turn, "run": run}))
                elif state.kind == "thinking":
                    outs.append(Out(ThinkingDone, {
                        "block_id": state.block_id, "steps": thinking_steps(state.text), "turn_id": turn, "run": run,
                        "duration_ms": int((time.monotonic() - state.started) * 1000)}))
            return outs
        if kind in STREAM_IGNORED:
            return outs
        return outs + self.unknown(f"stream_event/{kind}")

    # ── whole messages ───────────────────────────────────────────────────

    def _assistant(self, msg: dict[str, Any]) -> list[Out]:
        run = self._run_of(msg)
        outs = self._activity(run)
        turn = self.current_turn
        message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        message_id = str(message.get("id") or "")
        streamed = message_id in self._streamed
        content = message.get("content") if isinstance(message.get("content"), list) else []
        for n, block in enumerate(b for b in content if isinstance(b, dict)):
            btype = block.get("type")
            if btype == "tool_use":
                outs += self._tool_started(block, turn, run)
            elif btype == "text" and not streamed and self._shown(run, "text"):
                outs.append(Out(TextDone, {"block_id": f"{message_id}:a{n}", "text": str(block.get("text") or ""),
                                           "turn_id": turn, "run": run}))
            elif btype == "thinking" and not streamed and self._shown(run, "text"):
                outs.append(Out(ThinkingDone, {"block_id": f"{message_id}:a{n}", "duration_ms": None,
                                               "steps": thinking_steps(str(block.get("thinking") or "")),
                                               "turn_id": turn, "run": run}))
        return outs

    def _tool_started(self, block: dict[str, Any], turn: str | None, run: str | None) -> list[Out]:
        call_id = block.get("id")
        tool = block.get("name") if isinstance(block.get("name"), str) else ""
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        if not isinstance(call_id, str) or not call_id or call_id in self._tools:
            return []
        content_key = CONTENT_KEYS.get(tool)
        size = len(args[content_key].encode("utf-8")) if tool == "Write" and content_key \
            and isinstance(args.get(content_key), str) else None
        if not self._shown(run, "tool"):
            return []
        self._tools[call_id] = _OpenTool(turn_id=turn, run=run, tool=tool, size=size)
        call = describe_call(tool, args, cwd=self.cwd)
        outs = [Out(ToolStarted, {"call_id": call_id, "tool": tool, "kind": call.kind, "title": call.title,
                                  "target": call.target, "input": args, "turn_id": turn, "run": run})]
        items = plan_items(tool, args)
        if items is not None:
            outs.append(Out(PlanUpdated, {"items": items, "turn_id": turn, "run": run}))
        return outs

    def _user(self, msg: dict[str, Any]) -> list[Out]:
        message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            return []                       # a plain text message: an echo or claude's own note
        outs: list[Out] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            open_tool = self._tools.pop(call_id, None) if isinstance(call_id, str) else None
            if open_tool is None:
                continue                    # a call this side never reported (a hidden run's)
            status = "denied" if call_id in self.denied else "error" if block.get("is_error") else "ok"
            self.denied.discard(call_id)
            outs.append(Out(ToolFinished, {"call_id": call_id, "status": status,
                                           "summary": _result_summary(block.get("content")),
                                           "size": open_tool.size if status == "ok" else None,
                                           "turn_id": open_tool.turn_id, "run": open_tool.run}))
        return outs

    def _result(self, msg: dict[str, Any]) -> list[Out]:
        outs = self._activity(None)
        turn = self.current_turn
        subtype = msg.get("subtype")
        if msg.get("terminal_reason") in ABORTED:
            status = "interrupted"
        elif msg.get("is_error") or subtype != "success":
            status = "failed"
        else:
            status = "completed"
        outs += self._cancel_open_tools(turn)
        if status == "interrupted":
            outs += self._cut_off_blocks(turn)
        usage: TokenUsage | None = token_usage(msg)
        if status == "interrupted" and _reports_nothing(usage):
            usage = None            # claude reports zeros for an interrupted turn: unknown, never 0
        else:
            outs.append(Out(Usage, {"usage": usage, "turn_id": turn}))
        text = msg.get("result") if isinstance(msg.get("result"), str) else None
        error = None
        # Only a failed turn has an error: an interrupted one's `errors` is
        # claude's own diagnostic ("[ede_diagnostic] …"), not the person's to read.
        if status == "failed":
            errors = msg.get("errors")
            parts = [str(e) for e in errors] if isinstance(errors, list) else []
            if msg.get("api_error_status"):
                parts.insert(0, f"API error {msg['api_error_status']}")
            error = "; ".join(parts) or text or subtype or status
            outs += _api_failure(msg, self._retry_status, error, turn)
        self._retry_status = None
        outs.append(Out(TurnFinished, {"turn_id": turn, "status": status, "text": text,
                                       "structured": msg.get("structured_output"), "error": error, "usage": usage}))
        self.current_turn = None
        self._streamed.clear()
        self._message.pop(None, None)
        self._blocks = {k: v for k, v in self._blocks.items() if k[0] is not None}
        return outs

    def _rate_limit(self, msg: dict[str, Any]) -> list[Out]:
        info = msg.get("rate_limit_info") if isinstance(msg.get("rate_limit_info"), dict) else msg
        status = RATE_STATUS.get(info.get("status"))
        if status is None:
            return self.unknown(f"rate_limit_event/{info.get('status')}")
        utilization = info.get("utilization")
        return [Out(RateLimit, {"status": status, "window": info.get("rateLimitType"),
                                "resets_at": iso_from_epoch(info.get("resetsAt")),
                                "utilization": float(utilization) if isinstance(utilization, (int, float)) else None,
                                "overage": bool(info.get("isUsingOverage")), "turn_id": self.current_turn})]

    # ── sub-agent runs ───────────────────────────────────────────────────

    def _task(self, sub: str, msg: dict[str, Any]) -> list[Out]:
        if sub == "background_tasks_changed":
            tasks = msg.get("tasks") if isinstance(msg.get("tasks"), list) else []
            live = tuple(RunState(run_id=str(t.get("task_id") or t.get("id")),
                                  state=SNAPSHOT_STATES.get(t.get("status"), "running"))
                         for t in tasks if isinstance(t, dict) and (t.get("task_id") or t.get("id")))
            return [Out(RunSnapshot, {"live": live})]
        task_id = msg.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return self.unknown(f"system/{sub} without task_id")
        if sub == "task_started":
            if task_id in self._runs:
                return []
            info = self._run_info(task_id, msg)
            self._runs[task_id] = info
            if info.spawn_call_id:
                self._runs_by_call[info.spawn_call_id] = task_id
            return [Out(RunStarted, {"info": info, "run": task_id})]
        if task_id in self._runs_done:
            return []
        if sub == "task_progress":
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            return [Out(RunProgress, {"run": task_id, "tokens": _int(usage.get("total_tokens")),
                                      "tool_calls": _int(usage.get("tool_uses")),
                                      "duration_ms": _int(usage.get("duration_ms")),
                                      "last_tool": msg.get("last_tool_name"),
                                      "summary": msg.get("description")})]
        if sub == "task_notification":
            status = RUN_END.get(msg.get("status"))
        else:                               # task_updated: only a terminal patch ends the run
            patch = msg.get("patch") if isinstance(msg.get("patch"), dict) else {}
            status = RUN_END.get(patch.get("status"))
        if status is None:
            return []
        self._runs_done.add(task_id)
        summary = msg.get("summary") if isinstance(msg.get("summary"), str) else None
        return [Out(RunFinished, {"run": task_id, "status": status, "summary": summary})]

    def _run_info(self, task_id: str, msg: dict[str, Any]) -> RunInfo:
        task_type = msg.get("task_type") if isinstance(msg.get("task_type"), str) else ""
        kind = RUN_KINDS.get(task_type, "other")
        if kind == "subagent" and self.coordinator:
            kind = "worker"
        parent = msg.get("parent_task_id")
        depth = msg.get("spawn_depth")
        if not (isinstance(depth, int) and depth > 0) and isinstance(parent, str) and parent in self._runs:
            depth = self._runs[parent].depth + 1   # claude sends no depth for a sub-agent's shell (live run 4)
        call = msg.get("tool_use_id")
        return RunInfo(run_id=task_id, parent_run_id=parent if isinstance(parent, str) and parent else None,
                       spawn_call_id=call if isinstance(call, str) and call else None, kind=kind,
                       agent_type=msg.get("subagent_type") if isinstance(msg.get("subagent_type"), str) else None,
                       title=one_line(str(msg.get("description") or task_type or "a sub-agent"), RUN_TITLE_MAX),
                       background=bool(msg.get("is_backgrounded")),
                       depth=depth if isinstance(depth, int) and depth > 0 else 1,
                       hidden=task_type in HIDDEN_TASK_TYPES)


def _api_failure(msg: dict[str, Any], retry_status: int | None, error: str, turn: str | None) -> list[Out]:
    """A turn whose result is itself an API failure (overloaded, a 5xx, the
    API's own rate limit), reported in the result or by claude's retries
    running out with no answer since: one retryable ``Error`` before the
    ``TurnFinished``, so the engine re-queues the input with backoff instead
    of consuming it (ch 07 §5.1). Any other failure (max turns, a tool's
    error) is not retried, whatever happened earlier in the turn."""
    errors = msg.get("errors") if isinstance(msg.get("errors"), list) else []
    execution_error = msg.get("subtype") == "error_during_execution"   # not max turns, not a budget
    if isinstance(msg.get("api_error_status"), int):
        status = msg["api_error_status"]
    elif execution_error and retry_status is not None:
        status = retry_status           # retries ran out and the API never answered again
    else:
        status = None
    said_overloaded = (status is not None or execution_error) and any("overloaded" in str(e).lower()
                                                                       for e in errors)
    overloaded = status == OVERLOADED_STATUS or said_overloaded
    if not overloaded and status not in RETRYABLE_STATUS:
        return []
    return [Out(Error, {"code": "overloaded" if overloaded else "api_error", "message": error,
                        "retryable": True, "fatal": False, "turn_id": turn})]


def _reports_nothing(usage: TokenUsage) -> bool:
    return not (usage.input or usage.output or usage.cache_read or usage.cache_write or usage.reasoning
                or usage.cost_usd)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def token_usage(msg: dict[str, Any]) -> TokenUsage:
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    cost = msg.get("total_cost_usd")
    window = None
    model_usage = msg.get("modelUsage")
    if isinstance(model_usage, dict):
        windows = [m.get("contextWindow") for m in model_usage.values() if isinstance(m, dict)]
        window = max((w for w in windows if isinstance(w, int)), default=None)
    return TokenUsage(input=_int(usage.get("input_tokens")), output=_int(usage.get("output_tokens")),
                      cache_read=_int(usage.get("cache_read_input_tokens")),
                      cache_write=_int(usage.get("cache_creation_input_tokens")),
                      cost_usd=float(cost) if isinstance(cost, (int, float)) else None, context_window=window)


def _result_summary(content: object) -> str | None:
    if isinstance(content, list):
        content = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
    return one_line(content) if isinstance(content, str) and content.strip() else None


_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _first_sentence(paragraph: str) -> str:
    return one_line(_SENTENCE_END.split(paragraph.strip(), 1)[0], RUN_TITLE_MAX)


def _step_title(text: str) -> str | None:
    """The live header: the first sentence of the latest paragraph (VS1)."""
    latest = text[text.rfind("\n\n") + 2:] if "\n\n" in text else text
    return _first_sentence(latest) if latest.strip() else None


def thinking_steps(text: str) -> tuple[str, ...]:
    """One line per paragraph of the thinking summary."""
    return tuple(_first_sentence(p) for p in text.split("\n\n") if p.strip())


def plan_items(tool: str, args: dict[str, Any]) -> tuple[PlanItem, ...] | None:
    """The plan a ``TodoWrite`` call sets; None for any other call."""
    if tool != "TodoWrite" or not isinstance(args.get("todos"), list):
        return None
    return tuple(PlanItem(text=str(t.get("content") or ""), state=PLAN_STATES.get(t.get("status"), "todo"))
                 for t in args["todos"] if isinstance(t, dict))
