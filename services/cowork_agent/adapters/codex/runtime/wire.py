"""codex's app-server notifications as contract events (storm ch 03 §5, §8).

``Wire`` keeps the parse state of one session (its root thread, the turn in
progress, the blocks being streamed, the open tool calls, the sub-agent
runs) and turns each notification into ``Out`` records: an event class and
its fields. The session stamps the envelope (``seq``, ``ts``, ids) and
delivers them. Responses and server requests are the session's; every
notification is read here.

Rules this follows (shapes from codex 0.162.1, live):

* the root thread's turns are the session's turns; a turn codex starts on
  its own (none was sent) is ``TurnStarted(origin="injected")``;
* ``SessionIdle`` once the root thread is ``idle`` and its turn has ended.
  codex reports ``thread/status/changed idle`` just *before*
  ``turn/completed``, so the idle waits for the turn's end;
* a child thread is a sub-agent run (``run`` = its thread id): codex
  reports its start and end as ``subAgentActivity`` items on the parent,
  and streams the child's own items unasked (VF1a);
* a tool call still open when its turn ends is finished as ``cancelled``
  (an interrupted command gets no ``item/completed``), so every
  ``ToolStarted`` is finished; a text or thinking block an interrupt cut off
  is recorded as it stood;
* ``Usage`` is the root turn's own tokens: codex reports its thread's
  running total, so the turn's are the difference from its start;
* unknown wire data is reported as a ``Notice`` (once per kind per
  session), never dropped silently and never a crash.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from services.cowork_agent.adapters.codex.runtime.account import windows, worst
from services.cowork_agent.adapters.codex.runtime.tools import (
    Call,
    describe_file,
    describe_item,
    diff_stat,
    file_call_id,
    file_changes,
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
    RunProgress,
    RunStarted,
    SessionIdle,
    TextDelta,
    TextDone,
    ThinkingDelta,
    ThinkingDone,
    TokenUsage,
    ToolFinished,
    ToolInputDelta,
    ToolOutputDelta,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)

log = logging.getLogger(__name__)

#: A run's title is one line of at most this many characters (contract RunInfo).
RUN_TITLE_MAX = 80
PLAN_STATES = {"pending": "todo", "inProgress": "doing", "completed": "done"}
TURN_STATUS = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}
#: How each finished item's status reads as a tool call's.
TOOL_STATUS = {"completed": "ok", "failed": "error", "declined": "denied", "cancelled": "cancelled",
               "interrupted": "cancelled"}
#: codex's error kinds worth trying again later (ch 07 §5.1): the engine
#: re-queues the input with backoff instead of consuming it.
RETRYABLE_ERRORS = {"serverOverloaded": "overloaded", "internalServerError": "api_error",
                    "httpConnectionFailed": "api_error", "responseStreamConnectionFailed": "api_error",
                    "responseStreamDisconnected": "api_error", "responseTooManyFailedAttempts": "api_error",
                    # As claude's 429: the account's rate-limit event closes the pool until the reset first.
                    "usageLimitExceeded": "api_error", "rateLimitExceeded": "api_error"}
#: Notifications that carry nothing the contract needs.
IGNORED = frozenset({
    "thread/started", "thread/settings/updated", "account/updated", "remoteControl/status/changed",
    "turn/moderationMetadata", "thread/name/updated", "skills/changed", "hook/started", "hook/completed",
    "thread/goal/updated", "thread/goal/cleared", "item/autoApprovalReview/started",
    "item/autoApprovalReview/completed", "autoApprovalReview/strictReviewRequired", "fs/changed",
    "app/list/updated", "model/verification", "model/safetyBuffering/updated", "thread/queue/changed",
    "thread/environment/connected", "thread/environment/disconnected", "thread/attachment/updated",
    "thread/prediction/updated", "thread/project/updated", "project/changed", "account/login/completed",
    "mcpServer/oauthLogin/completed", "turn/diff/updated", "item/fileChange/outputDelta",
    "item/commandExecution/terminalInteraction", "item/mcpToolCall/progress", "item/reasoning/textDelta",
    "serverRequest/resolved", "thread/archived", "thread/unarchived", "thread/closed",
    "thread/deleted", "thread/reverted", "account/gatewayOAuth/changed", "mcpServer/event/stream/notification",
    "externalAgentConfig/import/progress", "externalAgentConfig/import/completed", "process/outputDelta",
    "process/exited", "command/exec/outputDelta", "windows/worldWritableWarning", "windowsSandbox/setupCompleted",
    "fuzzyFileSearch/sessionUpdated", "fuzzyFileSearch/sessionCompleted", "thread/compacted",
    # codex 0.164: the thread's unread marker; and the voice sessions (``thread/realtime/*``), which XO never starts.
    "thread/readState/changed", "thread/realtime/started", "thread/realtime/closed", "thread/realtime/error",
    "thread/realtime/sdp", "thread/realtime/itemAdded", "thread/realtime/item/started", "thread/realtime/item/completed",
    "thread/realtime/transcript/delta", "thread/realtime/transcript/done", "thread/realtime/item/transcript/delta",
    "thread/realtime/outputAudio/delta",
})
#: Families of notifications that carry nothing the contract needs, whatever
#: codex adds to them (voice sessions).
IGNORED_PREFIXES = ("thread/realtime/",)
#: codex refreshing its login mid-session: worth a log line, not the person's news.
LOGGED = frozenset({"modelProvider/authRecoveryStarted", "modelProvider/authRecoveryCompleted"})
#: Items that are not steps of their own (the person's input, codex's hooks).
IGNORED_ITEMS = frozenset({"userMessage", "hookPrompt", "functionCallOutput", "sleep"})
#: Items that are the agent's own words: its messages, and the plan codex's
#: plan mode ends with (it is that turn's answer).
WORDS = frozenset({"agentMessage", "plan"})
#: The warning codex prints when it starts without bubblewrap: expected, as
#: no session runs codex's sandbox (§0.67).
SANDBOX_WARNING = "bubblewrap"


@dataclass(frozen=True)
class Out:
    """One event to deliver: its class and fields (``turn_id`` and ``run``
    included where they apply); the session adds the envelope."""

    cls: type[Event]
    fields: dict[str, Any]


@dataclass
class _Block:
    block_id: str
    kind: str                     # text | thinking
    run: str | None
    shown: bool
    text: str = ""
    started: float = field(default_factory=time.monotonic)


@dataclass
class _OpenTool:
    turn_id: str | None
    run: str | None
    shown: bool


@dataclass
class _Run:
    info: RunInfo
    turn: str | None = None       # the child's turn in progress
    turn_of_root: str | None = None   # the root turn it was started in
    tokens: int = 0
    total: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)
    stopping: bool = False
    background_sent: bool = False


class Wire:
    """The parse state of one session's notifications."""

    def __init__(self, *, cwd: str, runs_detail: str, coordinator: bool, thinking: bool = True) -> None:
        self.cwd = cwd
        self.runs_detail = runs_detail
        self.coordinator = coordinator
        self.thinking = thinking
        self.thread_id: str | None = None          # the root thread
        self.current_turn: str | None = None
        self.denied: set[str] = set()              # calls the host denied (their item says "declined")
        self.root_idle = True
        self._awaiting_idle = False
        self._origins: list[str] = []              # origins of turns this side started, oldest first
        self._blocks: dict[str, _Block] = {}
        self._tools: dict[str, _OpenTool] = {}
        self._items: dict[str, dict[str, Any]] = {}   # every tool item seen, by id (approvals join on it)
        self._item_runs: dict[str, str | None] = {}
        self._completed_items: set[str] = set()
        self._held_items: dict[str, int] = {}       # server requests whose handlers still need the item
        self._runs: dict[str, _Run] = {}
        self._runs_done: set[str] = set()
        self._final: dict[str, str] = {}           # turn -> its answer so far
        self._total: dict[str, int] | None = None  # the root thread's running token total
        self._baseline: dict[str, int] | None = None
        self._usage: TokenUsage | None = None
        self._unknown: set[str] = set()
        self._turns_seen: set[str] = set()
        self._limits: tuple | None = None           # the last reported rate-limit state
        self.plan_mode = False                      # the root's turns run in codex's plan mode
        self._plans: dict[str, str] = {}            # turn -> the plan it ended with
        #: A plan mode turn ended with this plan (turn, text): its input stays
        #: open until the host says whether to go ahead (as claude's plan mode
        #: asks inside its turn).
        self.pending_plan: tuple[str, str] | None = None

    # ── turns ────────────────────────────────────────────────────────────

    def expect_turn(self, origin: str) -> None:
        """This side is starting a turn (``turn/start``): its ``TurnStarted``
        carries ``origin``."""
        self._origins.append(origin)

    def drop_expected(self) -> None:
        """A ``turn/start`` this side sent failed: no turn of it will come."""
        if self._origins:
            self._origins.pop()

    def start_turn(self, turn_id: str) -> list[Out]:
        """The root turn ``turn_id`` began. Said twice (``turn/start``'s
        answer and the ``turn/started`` notification, in either order, the
        turn perhaps already over), it starts once."""
        if turn_id in self._turns_seen:
            return []
        self._turns_seen.add(turn_id)
        outs = self.close_turn(status="interrupted", error=None) if self.current_turn else []
        origin = self._origins.pop(0) if self._origins else "injected"
        self.current_turn = turn_id
        self.root_idle = False
        self._awaiting_idle = False
        self._baseline = dict(self._total) if self._total else None
        self._usage = None
        return [*outs, Out(TurnStarted, {"turn_id": turn_id, "origin": origin})]

    def close_turn(self, *, status: str, error: str | None) -> list[Out]:
        """End the open turn when the process is gone (closed, crashed)."""
        if self.current_turn is None:
            return []
        turn = self.current_turn
        outs = self._cancel_open_tools(turn)
        if status == "interrupted":
            outs += self._cut_off_blocks(turn)
        outs.append(Out(TurnFinished, {"turn_id": turn, "status": status, "error": error,
                                       "text": self._final.pop(turn, None), "usage": self._usage}))
        self.current_turn = None
        return outs

    def run_title(self, run: str | None) -> str | None:
        found = self._runs.get(run) if run else None
        return found.info.title if found else None

    def run_turn(self, run: str) -> str | None:
        found = self._runs.get(run)
        return found.turn if found else None

    def live_runs(self) -> list[str]:
        return [r for r in self._runs if r not in self._runs_done]

    def mark_stopping(self, run: str) -> None:
        if run in self._runs:
            self._runs[run].stopping = True

    def item(self, item_id: object) -> dict[str, Any] | None:
        """A tool item codex reported, by id: a file change's request names
        only its item (ch 03 §2)."""
        return self._items.get(item_id) if isinstance(item_id, str) else None

    def hold_item(self, item_id: object) -> None:
        """Pin before scheduling a request handler: completion can arrive first."""
        if isinstance(item_id, str):
            self._held_items[item_id] = self._held_items.get(item_id, 0) + 1

    def release_item(self, item_id: object) -> None:
        if isinstance(item_id, str):
            remaining = self._held_items.get(item_id, 0) - 1
            if remaining > 0:
                self._held_items[item_id] = remaining
            else:
                self._held_items.pop(item_id, None)
            self._prune_item(item_id)

    def _remember_item(self, item_id: str, item: dict[str, Any], run: str | None) -> None:
        self._items[item_id] = item
        self._item_runs[item_id] = run

    def _finish_items(self, run: str | None) -> None:
        for item_id, owner in list(self._item_runs.items()):
            if owner == run:
                self._completed_items.add(item_id)
                self._prune_item(item_id)

    def _prune_item(self, item_id: str) -> None:
        if item_id not in self._completed_items or item_id in self._held_items:
            return
        self._items.pop(item_id, None)
        self._item_runs.pop(item_id, None)
        self._completed_items.discard(item_id)
        self.denied.difference_update(cid for cid in list(self.denied)
                                      if cid == item_id or cid.startswith(f"{item_id}:"))

    def open_mcp_call(self, thread_id: object, server: str, tool: str | None) -> str | None:
        """The MCP call codex is asking about: the latest open ``mcpToolCall``
        of that thread on that server (and tool, where known)."""
        run = self.run_of(thread_id)
        for item_id in reversed(list(self._tools)):
            item = self._items.get(item_id) or {}
            if item.get("type") == "mcpToolCall" and item.get("server") == server and self._tools[item_id].run == run \
                    and (tool is None or item.get("tool") == tool):
                return item_id
        return None

    def run_of(self, thread_id: object) -> str | None:
        """The run a thread is, or None for the root (and for a thread this
        session never heard of, which can only be its own)."""
        if not isinstance(thread_id, str) or thread_id == self.thread_id:
            return None
        return thread_id if thread_id in self._runs else None

    def is_foreign(self, thread_id: object) -> bool:
        """A thread that is neither the root nor one of its runs (before the
        root's id is known, what comes is the root's: its start's own lines)."""
        return isinstance(thread_id, str) and self.thread_id is not None and thread_id != self.thread_id \
            and thread_id not in self._runs

    def cancel_all_tools(self) -> list[Out]:
        outs = [Out(ToolFinished, {"call_id": cid, "status": "cancelled", "turn_id": t.turn_id, "run": t.run})
                for cid, t in self._tools.items() if t.shown]
        self._tools.clear()
        for item_id in list(self._items):
            self._completed_items.add(item_id)
            self._prune_item(item_id)
        return outs

    def end_live_runs(self, summary: str) -> list[Out]:
        live = self.live_runs()
        self._runs_done.update(live)
        return [Out(RunFinished, {"run": run, "status": "stopped", "summary": summary, "turn_id": None})
                for run in live]

    def _cancel_open_tools(self, turn_id: str) -> list[Out]:
        ended = [cid for cid, t in self._tools.items() if t.turn_id == turn_id and t.run is None]
        outs = []
        for cid in ended:
            tool = self._tools.pop(cid)
            if tool.shown:
                outs.append(Out(ToolFinished, {"call_id": cid, "status": "cancelled", "turn_id": turn_id,
                                               "run": None}))
        self._finish_items(None)
        return outs

    def _cut_off_blocks(self, turn_id: str | None) -> list[Out]:
        outs = []
        for key, block in list(self._blocks.items()):
            if block.run is not None:
                continue
            del self._blocks[key]
            if not block.text or not block.shown:
                continue
            if block.kind == "text":
                outs.append(Out(TextDone, {"block_id": block.block_id, "text": block.text, "turn_id": turn_id}))
            else:
                outs.append(Out(ThinkingDone, {"block_id": block.block_id, "steps": thinking_steps(block.text),
                                               "turn_id": turn_id,
                                               "duration_ms": int((time.monotonic() - block.started) * 1000)}))
        return outs

    # ── notifications ────────────────────────────────────────────────────

    def unknown(self, what: str) -> list[Out]:
        if what in self._unknown:
            return []
        self._unknown.add(what)
        return [Out(Notice, {"code": "unknown_wire", "turn_id": self.current_turn,
                             "message": f"codex sent something this runtime doesn't know ({what}); "
                                        "further ones of this kind are not reported again"})]

    def notification(self, method: str, params: dict[str, Any]) -> list[Out]:
        if self.is_foreign(params.get("threadId")):
            return []                       # a thread this session doesn't own (or not yet: its start comes first)
        handler = _HANDLERS.get(method)
        if handler is not None:
            outs = handler(self, params)
            if method == "item/completed":
                item_id = _item(params).get("id")
                if isinstance(item_id, str) and item_id in self._items:
                    self._completed_items.add(item_id)
                    self._prune_item(item_id)
            return outs
        if method in IGNORED or method.startswith(IGNORED_PREFIXES):
            return []
        if method in LOGGED:
            log.info("codex: %s", method)          # its params may name the account: not logged
            return []
        if method in ("warning", "guardianWarning", "deprecationNotice", "configWarning"):
            return self._warning(method, params)
        return self.unknown(method)

    def _shown(self, run: str | None, kind: str) -> bool:
        if run is None:
            return True
        return self.runs_detail == "text" or (self.runs_detail == "steps" and kind == "tool")

    def _turn_started(self, params: dict[str, Any]) -> list[Out]:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            return self.unknown("turn/started without a turn id")
        run = self.run_of(params.get("threadId"))
        if run is not None:
            found = self._runs[run]
            found.turn = turn_id
            if run in self._runs_done:
                # codex's children persist: a follow-up task (send_input,
                # followup_task) is the same run working again.
                self._runs_done.discard(run)
                found.stopping, found.background_sent = False, False
                found.turn_of_root, found.started = self.current_turn, time.monotonic()
                return [Out(RunStarted, {"info": found.info, "run": run, "turn_id": self.current_turn})]
            return []
        return self.start_turn(turn_id)

    def _turn_completed(self, params: dict[str, Any]) -> list[Out]:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id")
        run = self.run_of(params.get("threadId"))
        status = TURN_STATUS.get(turn.get("status"), "failed")
        error = turn.get("error") if isinstance(turn.get("error"), dict) else None
        if run is not None:
            return self._run_turn_ended(run, status, error)
        if turn_id != self.current_turn or self.current_turn is None:
            return []                       # a turn this session already closed
        outs = self._cancel_open_tools(turn_id)
        if status == "interrupted":
            outs += self._cut_off_blocks(turn_id)
        message = None
        if status == "failed":
            message = (error or {}).get("message") or "the turn failed"
            outs += _api_failure(error, message, turn_id)
        if self._usage is not None:
            outs.append(Out(Usage, {"usage": self._usage, "turn_id": turn_id}))
        text = self._final.pop(turn_id, None)
        outs.append(Out(TurnFinished, {"turn_id": turn_id, "status": status, "text": text,
                                       "structured": _structured(text), "error": message, "usage": self._usage}))
        self.current_turn = None
        self._blocks = {k: v for k, v in self._blocks.items() if v.run is not None}
        outs += self._to_background()
        self._awaiting_idle = True
        plan = self._plans.pop(turn_id, None)
        if self.plan_mode and status == "completed" and plan:
            self.pending_plan = (turn_id, plan)
        elif self.root_idle:
            outs += self._idle()
        return outs

    def release_idle(self) -> list[Out]:
        """The plan's question is settled without a new turn: the input ends."""
        self.pending_plan = None
        return self._idle() if self.root_idle else []

    def _to_background(self) -> list[Out]:
        """Runs still going when the root turn that started them ends now
        outlive it: say so once each (the contract's obligation)."""
        outs = []
        for run_id in self.live_runs():
            run = self._runs[run_id]
            if not run.background_sent:
                run.background_sent = True
                outs.append(Out(RunProgress, {"run": run_id, **self._numbers(run), "background": True,
                                              "turn_id": None}))
        return outs

    def _idle(self) -> list[Out]:
        if not self._awaiting_idle or self.current_turn is not None or self.pending_plan is not None:
            return []
        self._awaiting_idle = False
        return [Out(SessionIdle, {"turn_id": None})]

    def _status(self, params: dict[str, Any]) -> list[Out]:
        status = params.get("status") if isinstance(params.get("status"), dict) else {}
        kind = status.get("type")
        if self.run_of(params.get("threadId")) is not None:
            return []
        if kind == "idle":
            self.root_idle = True
            return self._idle()
        if kind == "active":
            self.root_idle = False
            return []
        if kind == "systemError":
            self.root_idle = True       # the thread does nothing more for this input
            return [Out(Error, {"code": "cli_error", "message": "codex reported a system error on this thread",
                                "retryable": False, "fatal": False, "turn_id": self.current_turn}), *self._idle()]
        return []

    # items

    def _item_started(self, params: dict[str, Any]) -> list[Out]:
        item = _item(params)
        kind = item.get("type")
        thread = params.get("threadId")
        run = self.run_of(thread)
        item_id = item.get("id")
        if kind == "subAgentActivity":
            return self._activity(item, parent=run)
        if not isinstance(item_id, str):
            return self.unknown(f"item/started {kind} without an id")
        if kind in IGNORED_ITEMS:
            return []
        if kind in WORDS:
            self._block(item_id, "text", run)
            return []
        if kind == "reasoning":
            self._block(item_id, "thinking", run)
            return []
        if kind == "contextCompaction":
            return [Out(Notice, {"code": "compacted", "message": "codex compacted the conversation's context",
                                 "turn_id": self.current_turn, "run": run})]
        if kind in ("enteredReviewMode", "exitedReviewMode"):
            return [Out(Notice, {"code": kind, "message": f"codex {kind}", "turn_id": self.current_turn,
                                 "run": run})]
        if kind == "fileChange":
            self._remember_item(item_id, item, run)
            return self._files_started(item_id, item, run)
        call = describe_item(item)
        if call is None:
            return self.unknown(f"item {kind}")
        self._remember_item(item_id, item, run)
        if item_id in self._tools:
            return []
        shown = self._shown(run, "tool")
        self._tools[item_id] = _OpenTool(turn_id=self.current_turn, run=run, shown=shown)
        if run is not None:
            self._runs[run].tool_calls += 1
        if not shown:
            return []
        return [Out(ToolStarted, {"call_id": item_id, "tool": call.tool, "kind": call.kind, "title": call.title,
                                  "target": call.target, "input": _tool_input(item, call), "turn_id": self.current_turn,
                                  "run": run})]

    # one apply_patch, one call per file (as claude's one Write or Edit per file)

    def _files_started(self, item_id: str, item: dict[str, Any], run: str | None) -> list[Out]:
        outs = []
        for index, change in enumerate(file_changes(item)):
            outs += self._file_started(file_call_id(item_id, index), change, run)
        return outs

    def _file_started(self, call_id: str, change: dict[str, Any], run: str | None) -> list[Out]:
        if call_id in self._tools:
            return []
        shown = self._shown(run, "tool")
        self._tools[call_id] = _OpenTool(turn_id=self.current_turn, run=run, shown=shown)
        if run is not None:
            self._runs[run].tool_calls += 1
        if not shown:
            return []
        call = describe_file(change)
        return [Out(ToolStarted, {"call_id": call_id, "tool": call.tool, "kind": call.kind, "title": call.title,
                                  "target": call.target, "input": {"paths": list(call.paths)},
                                  "turn_id": self.current_turn, "run": run})]

    def _files_finished(self, item_id: str, item: dict[str, Any], run: str | None) -> list[Out]:
        status = TOOL_STATUS.get(item.get("status"), "ok")
        if status != "ok" and (item_id in self.denied or file_call_id(item_id, 0) in self.denied):
            status = "denied"
        outs: list[Out] = []
        changes = file_changes(item)
        for index, change in enumerate(changes):
            call_id = file_call_id(item_id, index)
            outs += self._file_started(call_id, change, run)        # one that came whole starts now
            tool = self._tools.pop(call_id)
            if tool.shown:
                outs.append(Out(ToolFinished, {"call_id": call_id, "status": status, "summary": None,
                                               "diff_stat": diff_stat([change]), "turn_id": self.current_turn,
                                               "run": run}))
        prefix = f"{item_id}:"
        for call_id in [cid for cid in self._tools if cid.startswith(prefix)]:    # files the patch no longer names
            tool = self._tools.pop(call_id)
            if tool.shown:
                outs.append(Out(ToolFinished, {"call_id": call_id, "status": status, "turn_id": self.current_turn,
                                               "run": run}))
        return outs

    def _block(self, item_id: str, kind: str, run: str | None) -> _Block:
        block = self._blocks.get(item_id)
        if block is None:
            shown = self._shown(run, "text") and (kind == "text" or self.thinking)
            block = self._blocks[item_id] = _Block(block_id=item_id, kind=kind, run=run, shown=shown)
        return block

    def _item_completed(self, params: dict[str, Any]) -> list[Out]:
        item = _item(params)
        kind = item.get("type")
        run = self.run_of(params.get("threadId"))
        item_id = item.get("id")
        if kind == "subAgentActivity":
            return self._activity(item, parent=run)
        if not isinstance(item_id, str) or kind in IGNORED_ITEMS:
            return []
        if kind in WORDS:
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            block = self._blocks.pop(item_id, None) or _Block(item_id, "text", run, self._shown(run, "text"))
            if run is None and self.current_turn and text and kind == "plan":
                self._plans[self.current_turn] = text
            if run is None and self.current_turn and text:
                if kind == "plan" or item.get("phase") != "commentary" or self.current_turn not in self._final:
                    self._final[self.current_turn] = text
            if not block.shown or not text:
                return []
            return [Out(TextDone, {"block_id": item_id, "text": text, "turn_id": self.current_turn, "run": run})]
        if kind == "reasoning":
            block = self._blocks.pop(item_id, None)
            summary = item.get("summary") if isinstance(item.get("summary"), list) else []
            text = "\n\n".join(s for s in summary if isinstance(s, str) and s.strip()) or (block.text if block else "")
            if not text or (block is not None and not block.shown) or (block is None and not (
                    self.thinking and self._shown(run, "text"))):
                return []
            started = block.started if block else time.monotonic()
            return [Out(ThinkingDone, {"block_id": item_id, "steps": thinking_steps(text),
                                       "duration_ms": int((time.monotonic() - started) * 1000),
                                       "turn_id": self.current_turn, "run": run})]
        if kind in ("contextCompaction", "enteredReviewMode", "exitedReviewMode"):
            return []
        if kind == "fileChange":
            self._remember_item(item_id, item, run)
            return self._files_finished(item_id, item, run)
        call = describe_item(item)
        if call is None:
            return []
        self._remember_item(item_id, item, run)
        tool = self._tools.pop(item_id, None)
        outs: list[Out] = []
        if tool is None:
            # Finished without being reported started (it came whole): start it now.
            shown = self._shown(run, "tool")
            if not shown:
                return []
            outs.append(Out(ToolStarted, {"call_id": item_id, "tool": call.tool, "kind": call.kind,
                                          "title": call.title, "target": call.target,
                                          "input": _tool_input(item, call), "turn_id": self.current_turn,
                                          "run": run}))
        elif not tool.shown:
            return []
        status = TOOL_STATUS.get(item.get("status"), "ok")
        if kind == "commandExecution" and status == "ok" and isinstance(item.get("exitCode"), int) \
                and item["exitCode"] != 0:
            status = "error"
        if kind == "mcpToolCall" and item.get("error"):
            status = "error"
        if item_id in self.denied and status != "ok":
            status = "denied"
        fields: dict[str, Any] = {"call_id": item_id, "status": status, "summary": _tool_summary(item, call),
                                  "turn_id": self.current_turn, "run": run}
        if kind == "fileChange":
            fields["diff_stat"] = diff_stat(item.get("changes"))
        outs.append(Out(ToolFinished, fields))
        if kind == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
            outs += self._v1_spawned(item, parent=run)
        return outs

    def _text_delta(self, params: dict[str, Any]) -> list[Out]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            return []
        run = self.run_of(params.get("threadId"))
        block = self._block(item_id, "text", run)
        block.text += delta
        if not block.shown:
            return []
        return [Out(TextDelta, {"block_id": item_id, "delta": delta, "turn_id": self.current_turn, "run": run})]

    def _summary_delta(self, params: dict[str, Any]) -> list[Out]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            return []
        run = self.run_of(params.get("threadId"))
        block = self._block(item_id, "thinking", run)
        block.text += delta
        if not block.shown:
            return []
        return [Out(ThinkingDelta, {"block_id": item_id, "delta": delta, "step_title": _step_title(block.text),
                                    "turn_id": self.current_turn, "run": run})]

    def _summary_part(self, params: dict[str, Any]) -> list[Out]:
        item_id = params.get("itemId")
        if not isinstance(item_id, str):
            return []
        block = self._block(item_id, "thinking", self.run_of(params.get("threadId")))
        if block.text and not block.text.endswith("\n\n"):
            block.text += "\n\n"
        return []

    def _output_delta(self, params: dict[str, Any]) -> list[Out]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        tool = self._tools.get(item_id) if isinstance(item_id, str) else None
        if tool is None or not tool.shown or not isinstance(delta, str) or not delta:
            return []
        return [Out(ToolOutputDelta, {"call_id": item_id, "delta": delta, "turn_id": self.current_turn,
                                      "run": tool.run})]

    def _patch_updated(self, params: dict[str, Any]) -> list[Out]:
        """The patch codex is writing grows: "Writing `app.py` · 3.4 KB"."""
        item_id = params.get("itemId")
        changes = params.get("changes") if isinstance(params.get("changes"), list) else []
        run = self.run_of(params.get("threadId"))
        if not isinstance(item_id, str) or not self._shown(run, "tool"):
            return []
        outs = []
        for index, change in enumerate(changes):
            call_id = file_call_id(item_id, index)
            if not isinstance(change, dict) or call_id not in self._tools:
                continue                    # that file's call is already over
            size = len(change["diff"].encode()) if isinstance(change.get("diff"), str) else 0
            outs.append(Out(ToolInputDelta, {"call_id": call_id, "bytes_so_far": size, "turn_id": self.current_turn,
                                             "run": run}))
        return outs

    def _plan(self, params: dict[str, Any]) -> list[Out]:
        steps = params.get("plan") if isinstance(params.get("plan"), list) else []
        items = tuple(PlanItem(text=str(s.get("step") or ""), state=PLAN_STATES.get(s.get("status"), "todo"))
                      for s in steps if isinstance(s, dict))
        return [Out(PlanUpdated, {"items": items, "turn_id": self.current_turn,
                                  "run": self.run_of(params.get("threadId"))})]

    def _tokens(self, params: dict[str, Any]) -> list[Out]:
        usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
        total = _breakdown(usage.get("total"))
        last = _breakdown(usage.get("last"))
        run = self.run_of(params.get("threadId"))
        if run is not None:
            found = self._runs[run]
            found.tokens, found.total = total.get("totalTokens", 0), total
            if run in self._runs_done:
                return []
            return [Out(RunProgress, {"run": run, **self._numbers(found), "turn_id": self.current_turn})]
        if self._baseline is None:
            self._baseline = {k: total.get(k, 0) - last.get(k, 0) for k in total}
        self._total = total
        base = self._baseline
        window = usage.get("modelContextWindow")

        def spent(key: str) -> int:
            return max(total.get(key, 0) - base.get(key, 0), 0)

        self._usage = token_usage({key: spent(key) for key in total}, context_tokens=last.get("inputTokens") or None,
                                  context_window=window if isinstance(window, int) and not isinstance(window, bool)
                                  else None)
        return []                           # the turn's end reports it, once (as claude's result does)

    def _rate_limits(self, params: dict[str, Any]) -> list[Out]:
        """One ``RateLimit`` per snapshot: its worst window, which is the
        account's state (the engine sets its pool from each event alone).
        codex repeats its limits after every model call: only a change is
        reported."""
        w = worst(windows(params.get("rateLimits")))
        if w is None:
            return []
        state = (w.name, w.status, w.resets_at, None if w.utilization is None else round(w.utilization, 2), w.overage)
        if self._limits == state:
            return []
        self._limits = state
        return [Out(RateLimit, {"status": w.status, "window": w.name, "resets_at": w.resets_at,
                                "utilization": w.utilization, "overage": w.overage, "turn_id": self.current_turn})]

    def _error(self, params: dict[str, Any]) -> list[Out]:
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        message = str(error.get("message") or "codex reported an error")
        if params.get("willRetry"):
            return [Out(Notice, {"code": "api_retry", "message": one_line(message), "turn_id": self.current_turn,
                                 "run": self.run_of(params.get("threadId"))})]
        return []                           # the turn's end reports it (``turn/completed``, failed)

    def _warning(self, method: str, params: dict[str, Any]) -> list[Out]:
        message = params.get("message") or params.get("summary") or ""
        if not isinstance(message, str) or not message or SANDBOX_WARNING in message:
            return []
        key = f"{method}:{message}"
        if key in self._unknown:
            return []
        self._unknown.add(key)
        return [Out(Notice, {"code": "cli_warning", "message": one_line(message), "turn_id": self.current_turn})]

    def _rerouted(self, params: dict[str, Any]) -> list[Out]:
        return [Out(Notice, {"code": "model_fallback", "turn_id": self.current_turn,
                             "message": f"codex moved this turn from {params.get('fromModel')} to "
                                        f"{params.get('toModel')} ({params.get('reason')})"})]

    def _mcp_status(self, params: dict[str, Any]) -> list[Out]:
        if params.get("status") != "failed":
            return []
        name = params.get("name")
        why = one_line(str(params.get("error") or params.get("failureReason") or "unknown"))
        return [Out(Notice, {"code": "mcp_failed", "turn_id": self.current_turn,
                             "message": f"the MCP server {name} failed to start: {why}"})]

    # sub-agent runs

    def _activity(self, item: dict[str, Any], *, parent: str | None) -> list[Out]:
        child = item.get("agentThreadId")
        kind = item.get("kind")
        if not isinstance(child, str) or not child:
            return self.unknown("subAgentActivity without agentThreadId")
        if kind == "started":
            if child in self._runs:
                return []
            path = item.get("agentPath") if isinstance(item.get("agentPath"), str) else ""
            spawn = item.get("id") if isinstance(item.get("id"), str) else None
            return self._run_started(child, parent=parent, path=path, spawn_call_id=spawn,
                                     model=item.get("model"), title=None)
        if kind in ("completed", "failed", "stopped", "interrupted"):
            if child not in self._runs:
                return []
            status = {"completed": "completed", "failed": "failed"}.get(kind, "stopped")
            return self._run_finished(child, status, None)
        return self.unknown(f"subAgentActivity/{kind}")

    def _v1_spawned(self, item: dict[str, Any], *, parent: str | None) -> list[Out]:
        """codex's older multi-agent tools report a spawn as a finished
        ``collabAgentToolCall`` naming the new threads."""
        outs = []
        prompt = item.get("prompt") if isinstance(item.get("prompt"), str) else None
        for child in item.get("receiverThreadIds") or []:
            if isinstance(child, str) and child not in self._runs:
                outs += self._run_started(child, parent=parent, path="", spawn_call_id=item.get("id"),
                                          model=item.get("model"), title=prompt)
        return outs

    def _run_started(self, child: str, *, parent: str | None, path: str, spawn_call_id: object, model: object,
                     title: str | None) -> list[Out]:
        segments = [s for s in path.split("/") if s]
        depth = max(len(segments) - 1, 1) if segments else (self._runs[parent].info.depth + 1 if parent else 1)
        name = segments[-1].replace("_", " ") if segments else ""
        info = RunInfo(run_id=child, parent_run_id=parent,
                       spawn_call_id=spawn_call_id if isinstance(spawn_call_id, str) else None,
                       kind="worker" if self.coordinator else "subagent", agent_type=None,
                       title=one_line(title or name or "a sub-agent", RUN_TITLE_MAX), background=False, depth=depth,
                       model=model if isinstance(model, str) else None)
        self._runs[child] = _Run(info=info, turn_of_root=self.current_turn)
        return [Out(RunStarted, {"info": info, "run": child, "turn_id": self.current_turn})]

    def _run_turn_ended(self, run: str, status: str, error: dict | None) -> list[Out]:
        found = self._runs[run]
        found.turn = None
        outs = [Out(ToolFinished, {"call_id": cid, "status": "cancelled", "turn_id": self.current_turn, "run": run})
                for cid, t in list(self._tools.items()) if t.run == run and t.shown]
        for cid in [cid for cid, t in self._tools.items() if t.run == run]:
            del self._tools[cid]
        self._finish_items(run)
        if status == "interrupted" or found.stopping:
            return outs + self._run_finished(run, "stopped", "stopped by the person" if found.stopping else None)
        if status == "failed":
            return outs + self._run_finished(run, "failed", (error or {}).get("message"))
        return outs + self._run_finished(run, "completed", None)

    def finish_run(self, run: str, status: str, summary: str | None) -> list[Out]:
        """A run this side ended itself (stopped while it had no turn)."""
        return self._run_finished(run, status, summary)

    def _run_finished(self, run: str, status: str, summary: str | None) -> list[Out]:
        if run in self._runs_done:
            return []
        self._runs_done.add(run)
        usage = token_usage(self._runs[run].total) if self._runs[run].total else None
        return [Out(RunFinished, {"run": run, "status": status, "summary": summary, "usage": usage,
                                  "turn_id": self.current_turn})]

    def _numbers(self, run: _Run) -> dict[str, Any]:
        return {"tokens": run.tokens, "tool_calls": run.tool_calls,
                "duration_ms": int((time.monotonic() - run.started) * 1000)}


def _item(params: dict[str, Any]) -> dict[str, Any]:
    return params["item"] if isinstance(params.get("item"), dict) else {}


def _breakdown(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}


def token_usage(counts: dict[str, int], *, context_tokens: int | None = None,
                context_window: int | None = None) -> TokenUsage:
    """codex's token counts in the contract's terms: its ``inputTokens``
    include the cached ones, which the contract counts apart."""
    cached = counts.get("cachedInputTokens", 0)
    return TokenUsage(input=max(counts.get("inputTokens", 0) - cached, 0), output=counts.get("outputTokens", 0),
                      cache_read=cached, cache_write=counts.get("cacheWriteInputTokens", 0),
                      reasoning=counts.get("reasoningOutputTokens", 0), context_tokens=context_tokens,
                      context_window=context_window)


def _tool_input(item: dict[str, Any], call: Call) -> dict[str, Any]:
    """What the presenter may show of a call's input: the neutral fields."""
    out: dict[str, Any] = {}
    if call.command:
        out["command"] = call.command
        if isinstance(item.get("cwd"), str):
            out["cwd"] = item["cwd"]
    if call.paths:
        out["paths"] = list(call.paths)
    if call.mcp:
        out["server"], out["tool"] = call.mcp
        if isinstance(item.get("arguments"), dict):
            out["arguments"] = item["arguments"]
    if call.url:
        out["query" if call.kind == "web_search" else "url"] = call.url
    return out


def _tool_summary(item: dict[str, Any], call: Call) -> str | None:
    kind = item.get("type")
    if kind == "commandExecution":
        output = item.get("aggregatedOutput")
        if isinstance(output, str) and output.strip():
            return one_line(output.strip().splitlines()[-1])
        code = item.get("exitCode")
        return f"exit {code}" if isinstance(code, int) and code != 0 else None
    if kind == "mcpToolCall":
        error = item.get("error")
        if isinstance(error, dict) and error.get("message"):
            return one_line(str(error["message"]))
    return None


def _structured(text: str | None) -> Any:
    """An answer under an output schema is JSON text; anything else isn't structured."""
    if not text or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _api_failure(error: dict | None, message: str, turn: str | None) -> list[Out]:
    """A turn that failed on the API's side (overloaded, a 5xx, a dropped
    stream): one retryable ``Error`` before the ``TurnFinished``, so the
    engine re-queues the input with backoff (ch 07 §5.1)."""
    info = (error or {}).get("codexErrorInfo")
    name = info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) else None
    code = RETRYABLE_ERRORS.get(name or "")
    if code is None:
        return []
    detail = info.get(name) if isinstance(info, dict) and isinstance(info.get(name), dict) else {}
    status = detail.get("httpStatusCode")
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
        return []                       # the request itself was refused: trying again won't help
    return [Out(Error, {"code": code, "message": message, "retryable": True, "fatal": False, "turn_id": turn})]


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _first_sentence(paragraph: str) -> str:
    return one_line(_SENTENCE_END.split(paragraph.strip(), 1)[0], RUN_TITLE_MAX)


def _step_title(text: str) -> str | None:
    """The live header: codex starts each summary paragraph with a bold
    title (``**Reading the config**``); else the paragraph's first sentence."""
    latest = text[text.rfind("\n\n") + 2:] if "\n\n" in text else text
    if not latest.strip():
        return None
    bold = _BOLD.match(latest.strip())
    return one_line(bold.group(1), RUN_TITLE_MAX) if bold else _first_sentence(latest)


def thinking_steps(text: str) -> tuple[str, ...]:
    """One line per paragraph of the summary, its bold title where it has one."""
    steps = []
    for paragraph in text.split("\n\n"):
        if not paragraph.strip():
            continue
        bold = _BOLD.match(paragraph.strip())
        steps.append(one_line(bold.group(1), RUN_TITLE_MAX) if bold else _first_sentence(paragraph))
    return tuple(steps)


_HANDLERS = {
    "turn/started": Wire._turn_started,
    "turn/completed": Wire._turn_completed,
    "thread/status/changed": Wire._status,
    "item/started": Wire._item_started,
    "item/completed": Wire._item_completed,
    "item/agentMessage/delta": Wire._text_delta,
    "item/plan/delta": Wire._text_delta,
    "item/reasoning/summaryTextDelta": Wire._summary_delta,
    "item/reasoning/summaryPartAdded": Wire._summary_part,
    "item/commandExecution/outputDelta": Wire._output_delta,
    "item/fileChange/patchUpdated": Wire._patch_updated,
    "turn/plan/updated": Wire._plan,
    "thread/tokenUsage/updated": Wire._tokens,
    "account/rateLimits/updated": Wire._rate_limits,
    "error": Wire._error,
    "model/rerouted": Wire._rerouted,
    "mcpServer/startupStatus/updated": Wire._mcp_status,
}
