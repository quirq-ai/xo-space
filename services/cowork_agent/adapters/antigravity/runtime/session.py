"""One agy session: an ``agy`` stream-json process for its conversation,
and the socket XO's plugin reaches it through (storm ch 04 §1–§9).

agy has no control channel. What the runtime needs from inside agy comes
through XO's plugin (``install.py``): its hook asks the runtime about every
tool call of the session and of its sub-agents, adds the role's prompt when
the conversation starts and the turn's hidden context before each turn, and
ends a turn after a terminal comms tool; its MCP relay hands every comms and
Composio message to the runtime, which makes the HTTP call with the token.

Answers, by mode (the way claude's modes would, §0.70; ``auto`` is trusted, as
codex's is):

* every mode: agy's bookkeeping between its own agents and writes into its
  own folder for the conversation are approved; a file-tool write to a
  protected path (XO's, git's, any agent's project config, ``.agents/**``
  included) is refused; a sub-agent never talks through comms; agy's own
  permission tools are told to just try the action;
* a coordinator: everything else reaches the host (core's read-only policy);
* ``auto`` (and ``bypass``): everything else runs, nobody asked; an MCP tool
  of a server that isn't auto-approved still reaches the host;
* ``accept_edits``: the session's own edits in its folder run; the rest
  reaches the host;
* ``plan``: agy's plan mode; no file changes until XO's plan approval;
* ``ask``, ``deny_unasked``: the host.

agy exits on an interrupt, and takes model, effort and plan mode only on its
command line, so this session outlives its processes: the next send starts
agy again on the same conversation (``--conversation``). A process that ends
when nothing asked it to ends the session: the final events are
``TurnFinished``, ``ToolFinished(cancelled)``, ``RunFinished(stopped)``,
``Error``, then ``SessionClosed``, and the engine resumes by native id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from services.cowork_agent.adapters.antigravity.runtime import argv as a
from services.cowork_agent.adapters.antigravity.runtime import procs
from services.cowork_agent.adapters.antigravity.runtime.account import (
    UsageGuard,
    Window,
    agy_error,
    is_quota,
    login_fingerprint,
)
from services.cowork_agent.adapters.antigravity.runtime.env import agy_home, session_env
from services.cowork_agent.adapters.antigravity.runtime.hooks import HookServer
from services.cowork_agent.adapters.antigravity.runtime.install import agy_server_name, spec_server_name
from services.cowork_agent.adapters.antigravity.runtime.mcp_relay import Upstream, tool_result_ok, unmounted
from services.cowork_agent.adapters.antigravity.runtime.transcript import Tail, transcript_file
from services.cowork_agent.adapters.antigravity.runtime.tools import (
    INTERNAL,
    PERMISSION_ASKS,
    QUESTIONS,
    Call,
    artifact_write,
    describe,
    inside,
    own_edit,
    protected_any,
)
from services.cowork_agent.adapters.antigravity.runtime.wire import DENIED, Out, Wire
from services.cowork_agent.adapters.contract import (
    Effort,
    Error,
    Event,
    Host,
    Notice,
    PermissionDecision,
    PermissionMode,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    ProcessHandle,
    Question,
    QuestionAnswered,
    QuestionAsked,
    RateLimit,
    RunStarted,
    RuntimeFailure,
    SessionClosed,
    SessionIdle,
    SessionSpec,
    SessionStarted,
    SupportItem,
    SupportReport,
    TextDone,
    ThinkingDone,
    TurnFinished,
    TurnStarted,
    Usage,
    UserInput,
)
from utils.commands import LineTooLong

log = logging.getLogger(__name__)

#: How long agy has to print ``init`` (it signs in and loads its plugins first).
START_TIMEOUT_S = 120.0
#: After SIGTERM, how long the process group has before SIGKILL.
KILL_GRACE_S = 3.0
READER_DRAIN_S = 5.0
#: How often a live sub-agent's transcript is read.
FOLLOW_EVERY_S = 0.5
#: How long, after a process's first message, XO's comms relay has to check
#: in before the turn fails as retryable (agy starts plugin MCP servers then).
COMMS_START_TIMEOUT_S = 30.0
EVENT_QUEUE_SIZE = 1024
#: Hidden contexts waiting for agy's PreInvocation hook, at most.
CONTEXTS_KEPT = 64
STDERR_KEEP_LINES = 40

HOST_ERROR = "host error"
DEFAULT_DENY = "Denied by XO."
PROTECTED = ("XO refuses this write: {pattern} is a protected path (XO's own state, git's, or an agent's project "
             "config, which runs code). Leave it alone, or ask the person.")
PLAN_ONLY = "You are in plan mode: make no changes until the person approves your plan. Write the plan instead."
SUBAGENT_COMMS = ("Only the root agent talks through {server}. Report back to your parent agent in your final "
                  "message instead.")
DEFINE_REFUSED = ("XO can only see sub-agents of the type `self`. Start one with invoke_subagent and TypeName "
                  "self instead of defining a new type.")
PERMISSION_TOOL = ("XO checks each action when you take it, so there is nothing to ask for in advance: go ahead "
                   "with the action itself.")
ANSWERED = "XO asked the person. The person answered: {answer}"
NOT_ANSWERED = "XO asked the person, who gave no answer. Go on without it, or ask again later."
PLAN_APPROVED = "The person approved your plan. Carry it out now."
DENY_NOTE = "XO denied {what}: {why}"
#: The kinds of action each neutral tool ref a spec may deny stands for.
DENIED_KINDS = {"edit": ("edit_file",), "create": ("create_file",), "delete": ("delete_file",),
                "shell": ("command",), "web_fetch": ("web_fetch",), "web_search": ("web_search",),
                "subagents": ("subagent",), "ask_user": ("ask",)}
DENIED_TOOLS = {"schedule": ("schedule",)}
WRITES = ("create_file", "edit_file", "delete_file")
_WITHDRAWN = object()

COORDINATOR_ADDENDUM = ("You can't change files: XO refuses edits and any command that writes. Read code yourself "
                        "with read_file, list_dir, grep_search and read-only commands (rg, sed -n, git log, git "
                        "show); for wide reading you may use sub-agents. Never guess.")
SERVERS_NOTE = ("XO's MCP servers appear to you under agy's names: {names}. Call their tools with call_mcp_tool "
                "and that ServerName.")
TERMINAL_NOTE = ("End every turn with one of the calls your instructions above name for ending a turn (such as "
                 "{tools}, or a call that hands the work on) as your last call, and say nothing after it. Calling "
                 "one doesn't end the turn by itself until the work is done: a status or checklist update before "
                 "or during the work is not the end, so carry on with the work in the same turn.")
SUBAGENT_NOTE = ("Sub-agents you start get your tools, but only you talk through {servers}: XO refuses their "
                 "calls. Tell each sub-agent to report back to you in its final message.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AgySession:
    """``contract.Session`` on agy."""

    def __init__(self, *, session_id: str, spec: SessionSpec, report: SupportReport, host: Host, binary: str,
                 folder: Path, guard: UsageGuard, plugin_check: Callable[[], Any] | None = None,
                 usage_refresh: Callable[[], Any] | None = None) -> None:
        self.id = session_id
        self.native_id = ""
        self.report = report
        self.state = "starting"
        self.cli_version: str | None = None
        self.model: str | None = spec.model
        self._spec = spec
        self._mode: str = spec.permissions.mode
        self._effort: str | None = spec.effort
        self._host = host
        self._binary = binary
        self._folder = folder
        self._guard = guard
        self._plugin_check = plugin_check
        self._usage_refresh = usage_refresh
        self._coordinator = spec.orchestration == "coordinator"
        self._denied_kinds = frozenset(k for ref in spec.tools.deny for k in DENIED_KINDS.get(ref, ()))
        self._denied_tools = frozenset(t for ref in spec.tools.deny for t in DENIED_TOOLS.get(ref, ()))
        self._servers = {s.name: s for s in spec.mcp_servers}
        self._wire = Wire(cwd=spec.cwd, runs_detail=spec.runs_detail)
        self._seq = 0
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._bootstrap: deque[Event] = deque()
        self._early: list[Out] = []              # what agy said before SessionStarted could go out
        self._input_open = False                 # a turn or a send since the last SessionIdle
        self._loop_idle = False                  # agy's Stop hook said its loop ended with nothing running
        self._tail: list[Event] = []
        self._finished = asyncio.Event()
        self._emit_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._consumer = False
        self._handle: ProcessHandle | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._init: asyncio.Future | None = None
        self._planned: str | None = None         # why the current process is being stopped, when it is
        self._process_done: asyncio.Event = asyncio.Event()
        self._process_done.set()
        self._sent_in_process = 0
        self._restart = False                    # model, effort or plan mode changed: a new process next send
        self._server: HookServer | None = None
        self._http: httpx.AsyncClient | None = None
        self._upstreams: dict[str, Upstream] = {}
        self._relays_up: set[str] = set()
        self._held: set[asyncio.Task] = set()     # host calls in flight
        self._session_allowed: set[str] = set()
        self._contexts: dict[str, deque[str | None]] = {}
        self._notes: list[str] = []
        self._terminal_done = False
        self._plan_text: str | None = None
        self._plan_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._followers: dict[str, asyncio.Task] = {}
        self._thinking_tail: Tail | None = None
        self._fingerprint: str | None = None
        self._rate: tuple[str, str] | None = None
        self._end_reason: str | None = None
        self._ending = False
        self._closing: asyncio.Task | None = None
        self._started = False

    # ── start ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the socket, start agy on the conversation, and wait for its
        ``init``. Raises RuntimeFailure and leaves nothing behind when it
        can't."""
        try:
            await self._attach()
        except BaseException:
            await self._discard()
            raise
        async with self._emit_lock:
            early, self._early = self._early, []
            for out in [Out(SessionStarted, {"model": self.model, "cwd": self._spec.cwd,
                                             "cli_version": self.cli_version}), *early]:
                self._bootstrap.append(self._stamp(out))
            self._started = True
        self.state = "idle"

    async def _attach(self) -> None:
        spec = self._spec
        if not await asyncio.to_thread(os.path.isdir, spec.cwd):
            raise self._missing_cwd()
        if self._plugin_check is not None:
            await self._plugin_check()
        self._fingerprint = await asyncio.to_thread(login_fingerprint)
        self._server = HookServer(self._folder, self, label=f"agy session {self.id}")
        await self._server.start()
        if spec.mcp_servers:
            self._http = httpx.AsyncClient()
            self._upstreams = {s.name: Upstream(s, self._http) for s in spec.mcp_servers}
        await self._launch(spec.resume)
        if spec.thinking == "summary":
            path = await asyncio.to_thread(transcript_file, self.native_id)
            self._thinking_tail = await asyncio.to_thread(Tail(path).skip_to_end)

    def _missing_cwd(self) -> RuntimeFailure:
        return RuntimeFailure("spec_refused", f"the session's folder {self._spec.cwd} doesn't exist; recreate it, "
                                              "or open the session with a folder that exists")

    async def _launch(self, resume: str | None) -> None:
        """One agy process on the conversation (``resume``), up to its ``init``."""
        spec = self._spec
        env = await asyncio.to_thread(session_env, spec_env=spec.env, socket=self._server.path if self._server
                                      else None, label=f"agy session {self.id}")
        schema_file = await asyncio.to_thread(self._schema_file)
        command = a.argv(self._binary, spec, mode=self._mode, resume=resume, model=self.model or spec.model,
                         effort=self._effort, schema_file=schema_file)
        loop = asyncio.get_running_loop()
        self._init = loop.create_future()
        self._planned = None
        self._sent_in_process = 0
        try:
            handle = await self._host.launcher.spawn(command, cwd=spec.cwd, env=env, label=f"storm agy {self.id}")
        except OSError as exc:
            if not await asyncio.to_thread(os.path.isdir, spec.cwd):
                raise self._missing_cwd() from None
            raise RuntimeFailure("cli_missing", f"the agy CLI can't be started ({self._binary}: "
                                                f"{exc.strerror or exc}); install it, or point AGY_CLI_PATH at an "
                                                "executable agy") from exc
        self._handle = handle
        self._process_done = asyncio.Event()
        self._stderr.clear()
        self._reader = asyncio.create_task(self._read(handle), name=f"agy-reader:{self.id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(handle), name=f"agy-stderr:{self.id}")
        try:
            init = await asyncio.wait_for(asyncio.shield(self._init), START_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"agy didn't start within {START_TIMEOUT_S:g} s"
                                 + self._stderr_tail(), retryable=True) from exc
        conversation = init.get("conversation_id")
        if not isinstance(conversation, str) or not conversation:
            raise RuntimeFailure("protocol", "agy started without a conversation id" + self._stderr_tail())
        if resume and conversation != resume:
            # agy starts a new, empty conversation for an id it doesn't have:
            # carrying on would lose the session's history without a word.
            self._planned = "start_failed"
            await handle.terminate(KILL_GRACE_S)
            raise RuntimeFailure("session_not_found", f"agy has no conversation {resume}; XO can replace it only "
                                                      "if no input was ever submitted")
        self.native_id = conversation
        self._wire.conversation = conversation
        details = init.get("init") if isinstance(init.get("init"), Mapping) else {}
        if isinstance(details.get("model"), str) and details["model"]:
            self.model = details["model"]

    def _schema_file(self) -> str | None:
        if self._spec.output_schema is None:
            return None
        self._folder.mkdir(parents=True, exist_ok=True)
        path = self._folder / "schema.json"
        path.write_text(json.dumps(self._spec.output_schema), encoding="utf-8")
        return str(path)

    def _stderr_tail(self) -> str:
        tail = " | ".join(list(self._stderr)[-5:])[:500]
        return f": {tail}" if tail else ""

    async def _discard(self) -> None:
        """Undo a start that failed: the process, the tasks, the socket."""
        self._end_reason = "start_failed"
        self._planned = self._planned or "start_failed"
        if self._handle is not None:
            await self._handle.terminate(KILL_GRACE_S)
        for task in (self._reader, self._stderr_task):
            if task is not None:
                await _cancel_and_wait(task, READER_DRAIN_S)
        await self._release()
        self.state = "failed"

    async def _release(self) -> None:
        if self._server is not None:
            await self._server.close()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
        await asyncio.to_thread(shutil.rmtree, self._folder, True)

    # ── events ───────────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[Event]:
        if self._consumer:
            raise RuntimeError(f"session {self.id}: events() already has a consumer")
        self._consumer = True
        while self._bootstrap:
            yield self._bootstrap.popleft()
        while True:
            if self._finished.is_set() and self._queue.empty():
                for ev in self._tail:
                    yield ev
                return
            get = asyncio.ensure_future(self._queue.get())
            done = asyncio.ensure_future(self._finished.wait())
            try:
                await asyncio.wait({get, done}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                if not get.done():
                    get.cancel()
            if get.done() and not get.cancelled():
                yield get.result()

    def _stamp(self, out: Out, raw: dict[str, Any] | None = None) -> Event:
        self._seq += 1
        fields = dict(out.fields)
        fields.setdefault("turn_id", self._wire.current_turn)
        return out.cls(session=self.id, native_id=self.native_id or None, ts=_now(), seq=self._seq,
                       raw=raw if self._spec.debug_raw else None, **fields)

    async def _deliver(self, outs: list[Out], *, raw: dict[str, Any] | None = None) -> None:
        if not outs:
            return
        async with self._emit_lock:
            if self._ending:
                return
            if not self._started:
                self._early.extend(outs)                 # SessionStarted comes first, always
                return
            for out in outs:
                if out.cls is TurnStarted:
                    self._input_open = True
                    self._loop_idle = False
                await self._queue.put(self._stamp(out, raw))

    # ── the reader ───────────────────────────────────────────────────────

    async def _read(self, handle: ProcessHandle) -> None:
        failure: Out | None = None
        try:
            async for raw in handle.lines():
                await self._dispatch(raw)
        except LineTooLong as exc:
            failure = Out(Error, {"code": "protocol", "message": str(exc), "fatal": True})
            self._planned = None
            await handle.terminate(KILL_GRACE_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a reader bug must still end the process cleanly
            log.exception("agy session %s: the reader failed", self.id)
            failure = Out(Error, {"code": "protocol", "message": f"reading agy's output failed: {exc}",
                                  "fatal": True})
            self._planned = None
            await handle.terminate(KILL_GRACE_S)
        code = await handle.wait()
        await self._process_ended(handle, code, failure)

    async def _dispatch(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            await self._deliver(self._wire.unknown("a line that is not JSON"))
            return
        if not isinstance(msg, dict):
            await self._deliver(self._wire.unknown("a line that is not a JSON object"))
            return
        if msg.get("event") == "init":
            if self._init is not None and not self._init.done():
                self._init.set_result(msg)
            return
        outs = self._wire.line(msg)
        await self._deliver(outs, raw=msg)
        if self._spec.thinking == "summary" and any(out.cls in (TextDone, Usage) for out in outs):
            await self._root_thinking()
        for out in outs:
            if out.cls is RunStarted and out.fields["run"] not in self._followers:
                run = out.fields["run"]
                self._followers[run] = asyncio.create_task(self._follow(run), name=f"agy-run:{self.id}:{run}")
        if any(out.cls is TurnFinished for out in outs):
            await self._turn_ended(next(o for o in reversed(outs) if o.cls is TurnFinished))

    async def _turn_ended(self, finished: Out) -> None:
        self._terminal_done = False
        await self._check_login()
        self._refresh_usage_later()
        status = finished.fields.get("status")
        if self._mode == "plan" and status == "completed" and self._wire.pending == 0 and not self._ending:
            plan = self._plan_text or finished.fields.get("text")
            self._plan_text = None
            if plan:
                turn = finished.fields.get("turn_id")
                self._plan_task = asyncio.create_task(self._approve_plan(turn, plan), name=f"agy-plan:{self.id}")
                return
        await self._idle_if_done()

    async def _idle_if_done(self, *, force: bool = False) -> None:
        """``SessionIdle`` once agy has run every message sent to it and its
        loop has stopped with nothing left running (its ``Stop`` hook's
        ``fullyIdle``: a sub-agent still at work wakes the root again when it
        reports); ``force`` when agy's process is gone. Once per input."""
        if self._wire.pending or self._wire.current is not None or self._plan_task is not None:
            return
        if not self._input_open or not (self._loop_idle or force):
            return
        self._input_open = False
        await self._deliver([Out(SessionIdle, {})])
        if self.state in ("running", "waiting_host"):
            self.state = "idle"

    async def _read_stderr(self, handle: ProcessHandle) -> None:
        async for line in handle.stderr():
            text = line.decode(errors="replace").rstrip()
            self._stderr.append(text)
            error = agy_error(text)
            if error is not None and is_quota(error):
                await self._deliver([Out(RateLimit, {"status": "limited", "window": None})])

    async def _process_ended(self, handle: ProcessHandle, code: int | None, failure: Out | None) -> None:
        """One agy process is gone. Asked for (an interrupt, a restart): the
        session goes on, and the next send starts agy again. Otherwise the
        session ends."""
        if handle is not self._handle:
            return
        if self._stderr_task is not None:
            done, _ = await asyncio.wait({self._stderr_task}, timeout=KILL_GRACE_S)
            if not done:
                await _cancel_and_wait(self._stderr_task, KILL_GRACE_S)
        if self._init is not None and not self._init.done():
            self._init.set_exception(RuntimeFailure("process_died", f"agy exited (code {code}) before it started"
                                                    + self._stderr_tail(), retryable=True))
        planned = self._planned if failure is None else None
        self._handle = None
        if planned in ("interrupted", "restart", "plan"):
            outs = self._wire.close_turn("interrupted", None) if self._wire.current is not None else []
            outs += self._wire.cancel_tools(root_only=True)
            dropped = self._wire.pending_turns()
            for turn in dropped:
                self._wire.drop_expected(turn)
            if planned == "interrupted":
                outs += self._wire.end_live_runs("stopped with the session's turn")
            await self._deliver(outs)
            self._process_done.set()
            if planned == "interrupted" or dropped:     # a restart is for the send that follows it
                await self._idle_if_done(force=True)
            return
        self._process_done.set()
        if planned == "start_failed" or not self._started:
            return
        await self._ended(code, failure, reason=self._end_reason or "process_died")

    async def _ended(self, code: int | None, failure: Out | None, *, reason: str) -> None:
        """The session is over: release everything, report it, end events()."""
        for task in list(self._held):
            task.cancel()
        quiet = reason in ("closed", "interrupted")
        tail = self._stderr_tail()
        async with self._emit_lock:
            turn = self._wire.current_turn
            outs: list[Out] = []
            if failure is not None:
                outs.append(Out(failure.cls, {**failure.fields, "turn_id": turn}))
            elif reason == "process_died":
                outs.append(Out(Error, {"code": "process_died", "fatal": True, "retryable": True, "turn_id": turn,
                                        "message": f"agy exited unexpectedly with code {code}{tail}"}))
            if self._wire.current is not None:
                outs += self._wire.close_turn("interrupted" if quiet else "failed",
                                              None if quiet else f"agy exited ({code})")
            outs += self._wire.cancel_tools()
            outs += self._wire.end_live_runs("the session's process ended")
            outs.append(Out(SessionClosed, {"reason": reason, "exit_code": code}))
            self._ending = True
            self._tail.extend(self._stamp(out) for out in outs)
        self.state = "closed" if reason == "closed" else "failed"
        self._finished.set()

    async def _write(self, data: bytes) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeFailure("process_died", "agy is not running")
        async with self._write_lock:
            try:
                await handle.write(data)
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "agy's input is closed: the process has ended") from exc

    async def _root_thinking(self) -> None:
        """agy prints no thinking: it is read per step from the
        conversation's transcript once the step is done (emulated)."""
        if self._thinking_tail is None:
            return
        outs = []
        for step in await asyncio.to_thread(self._thinking_tail.read):
            thinking = step.get("thinking")
            if step.get("type") == "PLANNER_RESPONSE" and isinstance(thinking, str) and thinking.strip():
                lines = tuple(line.strip() for line in thinking.strip().splitlines() if line.strip())[:20]
                outs.append(Out(ThinkingDone, {"block_id": f"{self.native_id}:{step['step_index']}:t",
                                               "duration_ms": None, "steps": lines}))
        await self._deliver(outs)

    async def _follow(self, run: str) -> None:
        """A sub-agent's steps, from its own transcript as agy writes it: agy
        doesn't stream them. Its last model turn with no tool call is its end."""
        tail = Tail(await asyncio.to_thread(transcript_file, run))
        detail = self._spec.runs_detail
        calls: dict[int, str] = {}
        try:
            while run in self._wire.live_runs() and not self._ending:
                outs: list[Out] = []
                for step in await asyncio.to_thread(tail.read):
                    index, kind = step["step_index"], step.get("type")
                    if kind == "PLANNER_RESPONSE":
                        tool_calls = [c for c in step.get("tool_calls") or () if isinstance(c, Mapping)]
                        for n, call in enumerate(tool_calls, start=1):
                            calls[index + n] = f"{run}:{index + n}"
                            if detail != "lifecycle":
                                args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
                                outs += self._wire.tool_started(calls[index + n], str(call.get("name") or "tool"),
                                                                args, self._wire.current_turn, run)
                        content = step.get("content") if isinstance(step.get("content"), str) else ""
                        if detail == "text" and content.strip():
                            outs.append(Out(TextDone, {"block_id": f"{run}:{index}", "text": content.strip(),
                                                       "run": run}))
                        if not tool_calls and step.get("status") == "DONE":
                            outs += self._wire.finish_run(run, "completed", content.strip() or None)
                    elif kind == "GENERIC" and index in calls and step.get("status") in ("DONE", "ERROR"):
                        call_id = calls.pop(index)
                        error = str(step.get("error") or "")
                        status = "ok" if step.get("status") == "DONE" else \
                            "denied" if call_id in self._wire.denied or DENIED.match(error.strip('"')) else "error"
                        outs += self._wire.tool_finished(call_id, status, None)
                await self._deliver(outs)
                if run not in self._wire.live_runs():
                    break
                await asyncio.sleep(FOLLOW_EVERY_S)
        finally:
            self._followers.pop(run, None)

    # ── the plugin's calls (hooks.Handler) ───────────────────────────────

    def _root(self, payload: Mapping[str, Any]) -> bool:
        conv = payload.get("conversationId")
        return not conv or conv == self.native_id

    async def on_hook(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if event == "PreToolUse":
                return await self._pre_tool(payload)
            if event == "SessionStart":
                return self._session_start(payload)
            if event == "PreInvocation":
                return self._pre_invocation(payload)
            if event == "PostInvocation":
                if self._root(payload) and self._terminal_done:
                    self._terminal_done = False
                    return {"terminationBehavior": "terminate"}
                return {}
            if event == "Stop" and self._root(payload):
                self._loop_idle = payload.get("fullyIdle") is not False
                if self._loop_idle and self._wire.current is None:
                    asyncio.ensure_future(self._idle_if_done())
            return {}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bug here refuses the call, never allows it
            log.exception("agy session %s: answering agy's %s failed", self.id, event)
            if event == "PreToolUse":
                return {"decision": "deny", "reason": HOST_ERROR}
            return {}

    def _session_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self._root(payload):
            return {}
        text = self.role_text()
        if not text:
            return {}
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}

    def role_text(self) -> str:
        """The role's prompt, and what agy needs told about XO's tools."""
        spec = self._spec
        parts = [spec.system_prompt.text] if spec.system_prompt and spec.system_prompt.text else []
        if self._coordinator:
            parts.append(COORDINATOR_ADDENDUM)
        if spec.mcp_servers:
            parts.append(SERVERS_NOTE.format(names=", ".join(f"`{s.name}` is `{agy_server_name(s.name)}`"
                                                             for s in spec.mcp_servers)))
        terminal = [f"`{t}` on `{agy_server_name(s.name)}`" for s in spec.mcp_servers for t in s.terminal_tools]
        if terminal:
            parts.append(TERMINAL_NOTE.format(tools=", ".join(terminal)))
            parts.append(SUBAGENT_NOTE.format(servers=" or ".join(
                f"`{agy_server_name(s.name)}`" for s in spec.mcp_servers if s.terminal_tools)))
        return "\n\n".join(parts)

    def _pre_invocation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self._root(payload) or payload.get("invocationNum") != 0:
            return {}
        text = payload.get("lastUserInput") if isinstance(payload.get("lastUserInput"), str) else None
        waiting = self._contexts.get(text) if text is not None else None
        context = waiting.popleft() if waiting else None
        if waiting is not None and not waiting:
            self._contexts.pop(text, None)
        if not context:
            return {}
        return {"injectSteps": [{"ephemeralMessage": context}]}

    async def _pre_tool(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        call_obj = payload.get("toolCall") if isinstance(payload.get("toolCall"), Mapping) else {}
        tool = str(call_obj.get("name") or "")
        args = call_obj.get("args") if isinstance(call_obj.get("args"), Mapping) else {}
        conv = payload.get("conversationId") if isinstance(payload.get("conversationId"), str) else self.native_id
        call_id = f"{conv}:{payload.get('stepIdx')}"
        run = None if self._root(payload) else conv
        artifacts = payload.get("artifactDirectoryPath") if isinstance(payload.get("artifactDirectoryPath"),
                                                                       str) else None
        call = await asyncio.to_thread(describe, tool, args, self._spec.cwd)
        approve = {"decision": "auto_approve"}

        if tool in INTERNAL:
            return approve
        if tool in PERMISSION_ASKS:
            return self._deny(call_id, PERMISSION_TOOL)
        if tool == "define_subagent":
            return self._deny(call_id, DEFINE_REFUSED)
        if tool in QUESTIONS:
            return await self._question(call_id, run, args)
        if call.kind == "mcp":
            return await self._mcp_call(call_id, run, call, args)
        if call.kind in ("read", "list", "search") and call.paths and \
                await asyncio.to_thread(self._own_files, call.paths, artifacts):
            return approve
        if call.kind in WRITES:
            if await asyncio.to_thread(artifact_write, call.paths, artifacts):
                if isinstance(args.get("ArtifactMetadata"), Mapping) and \
                        args["ArtifactMetadata"].get("RequestFeedback") and isinstance(args.get("Content"), str):
                    self._plan_text = args["Content"]
                return approve
            pattern = await asyncio.to_thread(protected_any, call.paths, self._spec.cwd)
            if pattern is not None:
                return await self._refused(call_id, run, call, args, PROTECTED.format(pattern=pattern))
        if not self._coordinator and (call.kind in self._denied_kinds or tool in self._denied_tools):
            return await self._refused(call_id, run, call, args,
                                       f"this session may not {call.kind.replace('_', ' ')}")
        if self._coordinator:
            return await self._ask(call_id, run, call, args)
        if self._mode == "plan" and call.kind in WRITES:
            return self._deny(call_id, PLAN_ONLY)
        if self._mode in ("auto", "bypass"):
            return approve
        if self._mode == "accept_edits" and call.kind in WRITES and \
                await asyncio.to_thread(own_edit, call.paths, self._spec.cwd):
            return approve
        return await self._ask(call_id, run, call, args)

    def _own_files(self, paths: tuple[str, ...], artifacts: str | None) -> bool:
        """Reads of agy's own MCP tool schemas, its skills, or this
        conversation's artifacts: agy reads a tool's schema before it calls
        one, where claude has the schema already."""
        home = agy_home()
        roots = [str(home / "mcp"), str(home / "builtin"), str(home / "skills")] + ([artifacts] if artifacts else [])
        return all(any(inside(p, r) for r in roots) for p in paths)

    def _deny(self, call_id: str, reason: str) -> dict[str, Any]:
        self._wire.denied.add(call_id)
        return {"decision": "deny", "reason": reason}

    async def _mcp_call(self, call_id: str, run: str | None, call: Call, args: Mapping[str, Any]) -> dict[str, Any]:
        agy_name = call.mcp[0] if call.mcp else ""
        name = spec_server_name(agy_name)
        server = self._servers.get(name) if name else None
        if server is not None and run is not None and server.terminal_tools:
            return self._deny(call_id, SUBAGENT_COMMS.format(server=agy_name))
        if server is not None and server.auto_approve:
            return {"decision": "auto_approve"}
        neutral = replace(call, mcp=(server.name if server else agy_name, call.mcp[1] if call.mcp else ""))
        return await self._ask(call_id, run, neutral, args)

    def _request(self, call_id: str, run: str | None, call: Call, args: Mapping[str, Any]) -> PermissionRequest:
        return PermissionRequest(
            request_id=f"agy-{call_id}", run=run, call_id=call_id, tool=call.tool, kind=call.kind,
            summary=call.summary, input=dict(args), cwd=self._spec.cwd, paths=call.paths, command=call.command,
            url=call.url, mcp=call.mcp, content_sha256=call.content_sha256, run_title=self._wire.run_title(run),
            scopes=("once", "session"))

    async def _refused(self, call_id: str, run: str | None, call: Call, args: Mapping[str, Any],
                       reason: str) -> dict[str, Any]:
        """A deny the runtime makes itself, reported like any answer."""
        req = self._request(call_id, run, call, args)
        decision = PermissionDecision(allow=False, message=reason)
        await self._deliver([Out(PermissionRequested, {"request": req, "run": run}),
                             Out(PermissionResolved, {"request_id": req.request_id, "decision": decision,
                                                      "run": run})])
        return self._deny(call_id, reason)

    async def _ask(self, call_id: str, run: str | None, call: Call, args: Mapping[str, Any]) -> dict[str, Any]:
        """The host's answer, as long as it takes. Scope ``session`` is kept
        here, never written into agy's settings."""
        req = self._request(call_id, run, call, args)
        key = json.dumps([req.kind, req.command, list(req.paths), req.mcp, req.content_sha256], default=str)
        await self._deliver([Out(PermissionRequested, {"request": req, "run": run})])

        def resolved(answer: object) -> Out:
            decision = answer if isinstance(answer, PermissionDecision) \
                else PermissionDecision(allow=False, message="agy withdrew the request")
            return Out(PermissionResolved, {"request_id": req.request_id, "decision": decision, "run": run})

        if key in self._session_allowed:
            decision = PermissionDecision(allow=True, scope="session")
            await self._deliver([resolved(decision)])
            return {"decision": "auto_approve"}
        decision = await self._ask_host(lambda: self._host.decide_permission(req), resolved=resolved,
                                        valid=lambda d: isinstance(d, PermissionDecision),
                                        fallback=PermissionDecision(allow=False, message=HOST_ERROR))
        if decision.allow:
            if decision.scope == "session":
                self._session_allowed.add(key)
            return {"decision": "auto_approve"}
        if decision.stop_turn:
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.interrupt()))
        return self._deny(call_id, decision.message or DEFAULT_DENY)

    async def _ask_host(self, call: Callable[[], Any], *, resolved: Callable[[object], Out],
                        valid: Callable[[Any], bool], fallback: Any) -> Any:
        """Wait for the host as long as it takes. A host that raises or
        answers something malformed gets ``fallback``, a deny. If agy drops
        the call (the hook's process went away), the wait is cancelled and
        the request reported withdrawn."""
        task = asyncio.ensure_future(call())
        self._held.add(task)
        if self.state in ("idle", "running"):
            self.state = "waiting_host"
        try:
            answer = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.shield(self._deliver([resolved(_WITHDRAWN)]))
            raise
        except Exception:  # noqa: BLE001 - any host failure is a deny, never an allow
            log.exception("agy session %s: the host failed to answer", self.id)
            answer = fallback
        else:
            if not valid(answer):
                log.error("agy session %s: the host answered with a %s", self.id, type(answer).__name__)
                answer = fallback
        finally:
            self._held.discard(task)
            if self.state == "waiting_host" and not self._held:
                self.state = "running" if self._wire.current is not None or self._wire.pending else "idle"
        await self._deliver([resolved(answer)])
        return answer

    async def _question(self, call_id: str, run: str | None, args: Mapping[str, Any]) -> dict[str, Any]:
        raw = args.get("questions") if isinstance(args.get("questions"), list) else []
        questions = tuple({
            "id": str(n), "header": q.get("header"), "question": q.get("question"),
            "options": [{"label": str(o), "description": None} if not isinstance(o, Mapping)
                        else {"label": o.get("label"), "description": o.get("description")}
                        for o in q.get("options") or []],
            "multi": bool(q.get("is_multi_select")), "secret": False,
        } for n, q in enumerate(raw) if isinstance(q, Mapping))
        rid = f"agy-{call_id}"
        question = Question(request_id=rid, run=run, questions=questions, run_title=self._wire.run_title(run))
        await self._deliver([Out(QuestionAsked, {"question": question, "run": run})])
        failed = object()
        answer = await self._ask_host(
            lambda: self._host.answer_question(question),
            resolved=lambda ans: Out(QuestionAnswered, {"request_id": rid, "run": run,
                                                        "answer": dict(ans) if isinstance(ans, Mapping) else {}}),
            valid=lambda ans: isinstance(ans, Mapping), fallback=failed)
        if not isinstance(answer, Mapping) or not answer:
            return self._deny(call_id, NOT_ANSWERED)
        lines = []
        for n, q in enumerate(questions):
            given = answer.get(q["id"], answer.get(str(n)))
            if given is None:
                continue
            value = ", ".join(map(str, given)) if isinstance(given, (list, tuple)) else str(given)
            lines.append(f"{q['question']}: {value}" if q.get("question") else value)
        return self._deny(call_id, ANSWERED.format(answer="; ".join(lines) or json.dumps(dict(answer))))

    def on_mcp_hello(self, server: str) -> None:
        self._relays_up.add(server)

    async def on_mcp(self, server: str, message: Mapping[str, Any]) -> dict[str, Any] | None:
        upstream = self._upstreams.get(server)
        if upstream is None:
            return unmounted(server, message)
        answer = await upstream.send(message)
        if message.get("method") == "tools/call" and tool_result_ok(answer):
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), Mapping) else {}
            conv = meta.get("antigravity.google/conversation_id")
            if params.get("name") in upstream.server.terminal_tools and (conv is None or conv == self.native_id):
                self._terminal_done = True
        return answer

    # ── the operations core calls ────────────────────────────────────────

    def _require_live(self) -> None:
        if self.state in ("closing", "closed", "failed") or self._finished.is_set():
            raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")

    async def send(self, msg: UserInput) -> str:
        """Send one message: agy runs it now, or (a turn running) as its next
        turn. Starts agy again on the conversation if it isn't running."""
        self._require_live()
        self._guard.check(self.model)
        async with self._send_lock:
            await self._check_login()
            await self._withdraw_plan(end_input=False)        # the person moved on before deciding it
            if self._handle is None or (self._restart and self._wire.current is None and not self._wire.pending):
                if self._handle is not None:
                    await self._stop_process("restart", grace_s=KILL_GRACE_S)
                await self._process_done.wait()
                self._restart = False
                await self._launch(self.native_id)
            text = msg.text
            if msg.images:
                text += "".join(f"\n\n[An image is attached at {path}. Read it with your file tool.]"
                                for path in msg.images)
            notes, self._notes = self._notes, []
            context = "\n\n".join([*notes, *([msg.context] if msg.context else [])]) or None
            if context:
                if len(self._contexts) > CONTEXTS_KEPT:      # agy never asked for these: start afresh
                    self._contexts.clear()
                self._contexts.setdefault(text, deque()).append(context)
            turn_id = self._wire.expect_turn(msg.origin)
            self._input_open = True
            try:
                await self._write(a.user_line(text))
            except BaseException:
                self._wire.drop_expected(turn_id)
                self._notes = notes + self._notes
                raise
            self._sent_in_process += 1
            if self._sent_in_process == 1:
                self._watch_comms()
            if self.state == "idle":
                self.state = "running"
            return turn_id

    def _watch_comms(self) -> None:
        """agy starts the plugin's MCP relays at a process's first message; a
        turn whose comms never connected couldn't deliver its result, so it
        fails as retryable instead, as codex's does."""
        needed = {s.name for s in self._spec.mcp_servers if s.terminal_tools}
        if not needed or needed <= self._relays_up:
            return
        if self._watchdog is not None and not self._watchdog.done():
            return
        self._relays_up -= needed

        async def watch() -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + COMMS_START_TIMEOUT_S
            while loop.time() < deadline:
                if needed <= self._relays_up or self._handle is None:
                    return
                await asyncio.sleep(0.2)
            missing = ", ".join(sorted(needed - self._relays_up))
            await self._deliver([Out(Error, {"code": "protocol", "retryable": True,
                                             "message": f"agy didn't start XO's relay for {missing}, so this turn "
                                                        "couldn't reach the conversation; XO will retry"})])
            await self.interrupt()

        self._watchdog = asyncio.create_task(watch(), name=f"agy-comms:{self.id}")

    async def interrupt(self, grace_s: float = 10) -> None:
        """SIGINT: agy ends the turn and its process (V8). The session stays;
        the next send starts agy again on the conversation, which remembers
        the interrupted work."""
        if self._finished.is_set() or self.state == "closing":
            return
        if self._handle is None or (self._wire.current is None and not self._wire.pending):
            await self._withdraw_plan(end_input=True)
            return
        await self._stop_process("interrupted", grace_s=grace_s, sigint=True)

    async def _stop_process(self, why: str, *, grace_s: float, sigint: bool = False) -> None:
        handle = self._handle
        if handle is None:
            return
        self._planned = why
        noted = await asyncio.to_thread(procs.descendants, handle.pid)
        if sigint:
            with contextlib.suppress(ProcessLookupError, OSError):
                await handle.signal(signal.SIGINT)
        else:
            with contextlib.suppress(Exception):
                await handle.close_stdin()
        try:
            await asyncio.wait_for(self._process_done.wait(), grace_s)
        except asyncio.TimeoutError:
            await self._deliver([Out(Notice, {"code": "interrupt_escalated" if sigint else "restart_escalated",
                                              "message": f"agy did not stop within {grace_s:g} s; its process "
                                                         "group was terminated"})])
            await handle.terminate(KILL_GRACE_S)
            await self._await_reader()
        await asyncio.to_thread(procs.stop, noted)

    async def _await_reader(self) -> None:
        reader = self._reader
        if reader is None or reader.done():
            return
        done, _ = await asyncio.wait({reader}, timeout=READER_DRAIN_S)
        if not done:
            await _cancel_and_wait(reader, READER_DRAIN_S)

    async def update(self, *, model: str | None = None, effort: Effort | None = None,
                     permission_mode: PermissionMode | None = None) -> SupportReport:
        """A new model or effort, or leaving or entering plan mode, starts agy
        again on the same conversation (once its turn is over); any other
        mode change applies to XO's answers at once."""
        self._require_live()
        items: list[SupportItem] = []
        if model is not None and model != self.model:
            self.model = model
            self._spec = replace(self._spec, model=model)
            self._restart = True
            items.append(_item("update.model", "a restart on the same conversation", "emulated"))
        if effort is not None and effort != self._effort:
            self._effort = effort
            self._restart = True
            items.append(_item("update.effort", "a restart on the same conversation", "emulated"))
        if permission_mode is not None:
            if permission_mode == "bypass" and self._spec.permissions.mode != "bypass":
                items.append(SupportItem(feature="update.permission_mode", support="refused", mechanism="XO's hook",
                                         enforced_by="none", note="bypass only for a session whose spec asked "
                                                                  "for it"))
            elif self._coordinator and permission_mode in a.COORDINATOR_REFUSED:
                items.append(SupportItem(feature="update.permission_mode", support="refused", mechanism="XO's hook",
                                         enforced_by="none",
                                         note=f"{permission_mode} would let a coordinator's workers act unasked"))
            else:
                if (permission_mode == "plan") != (self._mode == "plan"):
                    self._restart = True
                self._mode = permission_mode
                items.append(SupportItem(feature="update.permission_mode", support="native", mechanism="XO's hook",
                                         enforced_by="hook"))
        return SupportReport(items=tuple(items))

    async def stop_run(self, run_id: str) -> None:
        raise RuntimeFailure("unsupported", "agy has no way in to stop one sub-agent; stop the session's turn "
                                            "instead, which stops its sub-agents")

    async def send_to_run(self, run_id: str, msg: UserInput) -> None:
        raise RuntimeFailure("unsupported", "messaging a sub-agent is not offered this release (§0.82)")

    async def close(self, grace_s: float = 5) -> None:
        """End agy and everything it left running, then the socket.
        Idempotent; safe while a permission is held (the wait is cancelled)."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(grace_s), name=f"agy-close:{self.id}")
        await asyncio.shield(self._closing)

    async def _close(self, grace_s: float) -> None:
        if self._finished.is_set():
            await self._release()
            return
        self._end_reason = "closed"
        self.state = "closing"
        for task in [*self._held, *([self._plan_task] if self._plan_task else []),
                     *([self._watchdog] if self._watchdog else []), *self._followers.values()]:
            task.cancel()
        handle = self._handle
        if handle is not None:
            self._planned = None
            noted = await asyncio.to_thread(procs.descendants, handle.pid)
            with contextlib.suppress(Exception):
                await handle.close_stdin()
            await handle.terminate(grace_s)
            await self._await_reader()
            await asyncio.to_thread(procs.stop, noted)
        if not self._finished.is_set():
            await self._ended(None, None, reason="closed")
        await self._release()

    # ── plans ────────────────────────────────────────────────────────────

    async def _withdraw_plan(self, *, end_input: bool) -> None:
        task = self._plan_task
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task}, timeout=READER_DRAIN_S)
        self._plan_task = None
        if end_input:
            await self._idle_if_done(force=True)

    async def _approve_plan(self, turn: str | None, plan: str) -> None:
        """agy's plan mode ended its turn with a plan: ask the host to go
        ahead, as claude's plan mode does. Yes: the session leaves plan mode
        (agy starts again without it) and carries the plan out. No: the input
        ends, and the reason goes with the next one."""
        req = PermissionRequest(request_id=f"plan-{turn}", run=None, call_id=None, tool="plan", kind="plan",
                                summary="go ahead with this plan", input={"plan": plan}, cwd=self._spec.cwd,
                                plan=plan, scopes=("once",))
        try:
            await self._deliver([Out(PermissionRequested, {"request": req})])
            decision = await self._ask_host(
                lambda: self._host.decide_permission(req),
                resolved=lambda d: Out(PermissionResolved, {
                    "request_id": req.request_id,
                    "decision": d if isinstance(d, PermissionDecision)
                    else PermissionDecision(allow=False, message="the plan was withdrawn")}),
                valid=lambda d: isinstance(d, PermissionDecision),
                fallback=PermissionDecision(allow=False, message=HOST_ERROR))
            if decision.allow and not self._ending:
                self._mode = "ask"
                self._restart = True
                self._plan_task = None
                await self.send(UserInput(text=PLAN_APPROVED, origin="system"))
                return
            if decision.message:
                self._notes.append(DENY_NOTE.format(what="the plan", why=decision.message))
        except asyncio.CancelledError:
            raise
        except RuntimeFailure as exc:
            log.info("agy session %s: carrying out the approved plan failed (%s)", self.id, exc.message)
        finally:
            if self._plan_task is asyncio.current_task():
                self._plan_task = None
        await self._idle_if_done(force=True)

    # ── turn boundaries ──────────────────────────────────────────────────

    async def _check_login(self) -> None:
        """Has the person connected, disconnected or switched the agy login
        since this session started? Reported once per change; the engine
        recycles the session at its next boundary (§0.79)."""
        if self._fingerprint is None:
            return
        now = await asyncio.to_thread(login_fingerprint)
        if now != self._fingerprint:
            self._fingerprint = now
            await self._deliver([Out(Notice, {"code": "credentials_changed",
                                              "message": "the agy login changed since this session started; "
                                                         "recycle the session to use it"})])

    def _refresh_usage_later(self) -> None:
        if self._usage_refresh is None:
            return

        async def refresh() -> None:
            with contextlib.suppress(Exception):
                await self._usage_refresh()
                worst: Window | None = self._guard.worst(self.model)
                if worst is None:
                    return
                key = (worst.name, worst.status)
                if key != self._rate and (worst.status != "ok" or self._rate is not None):
                    self._rate = key
                    await self._deliver([Out(RateLimit, {"status": worst.status, "window": worst.name,
                                                         "resets_at": worst.resets_at,
                                                         "utilization": worst.used})])
        asyncio.ensure_future(refresh())


def _item(feature: str, mechanism: str, support: str = "native") -> SupportItem:
    return SupportItem(feature=feature, support=support, mechanism=mechanism, enforced_by="cli")


async def _cancel_and_wait(task: asyncio.Task, timeout: float) -> None:
    if not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=timeout)


__all__ = ["AgySession", "START_TIMEOUT_S"]
