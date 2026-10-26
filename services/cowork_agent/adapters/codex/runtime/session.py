"""One codex session: its own ``codex app-server --listen stdio://`` process
serving one thread and its sub-agents' threads (storm ch 03 §1–§5; one
process per session, owner 2026-10-22, so the engine sees codex exactly as it
sees claude).

The session owns its process from spawn to reap. One reader task reads the
server's stdout; each request the server sends (an approval, a question)
gets its own task, held with no timeout until the host answers (a pending
JSON-RPC call costs codex nothing, K5). Events go to one bounded queue in
``seq`` order; the last ones (cancelled calls, ``SessionClosed``) are kept
apart, so a consumer that stopped reading can never block ``close``.

Every session runs with no sandbox and ``untrusted`` approvals (§0.67), so
codex asks about every command and every file change. The runtime answers
the way claude's modes do (§0.70, §0.80): a thread's (``accept_edits``) own
edits in its folder are accepted here, as claude's ``acceptEdits`` does; the
rest goes to ``Host.decide_permission``. It never answers
``acceptWithExecpolicyAmendment``, which would write a rule into the
person's codex setup (ch 03 §13 item 3). A denial's reason can't ride on
codex's ``decline``, so it goes in as a system note steered into the turn
(R3).

A session lives as long as its process. When the process ends (``close``,
an ``interrupt`` that had to kill it, a crash), the final events are
``TurnFinished`` for an open turn, ``ToolFinished(cancelled)`` for every open
call, ``RunFinished(stopped)`` for every live sub-agent, ``Error`` for a
crash, then ``SessionClosed``; the engine resumes by native id (the thread)
with a new ``open``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from services.cowork_agent.adapters.codex.runtime import params as p
from services.cowork_agent.adapters.codex.runtime.account import UsageGuard, login_fingerprint
from services.cowork_agent.adapters.codex.runtime.env import session_env
from services.cowork_agent.adapters.codex.runtime.rpc import METHOD_NOT_FOUND, Line, Rpc, RpcError, classify
from services.cowork_agent.adapters.codex.runtime.rpc import initialize_params, version_of
from services.cowork_agent.adapters.codex.runtime.tools import (
    Call,
    describe_change,
    describe_command,
    one_line,
    own_edit,
    skill_read,
)
from services.cowork_agent.adapters.codex.runtime.wire import Out, Wire
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
    RuntimeFailure,
    SessionClosed,
    SessionSpec,
    SessionStarted,
    SupportItem,
    SupportReport,
    TurnFinished,
    UserInput,
)
from utils.commands import LineTooLong

log = logging.getLogger(__name__)

#: How long codex has to answer ``initialize`` and ``thread/start`` (the
#: latter starts its MCP servers).
START_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 30.0
#: After SIGTERM, how long the process group has before SIGKILL.
KILL_GRACE_S = 3.0
#: After the process is gone, how long the reader may take to deliver what it
#: already read before the end is reported without it.
READER_DRAIN_S = 5.0
EVENT_QUEUE_SIZE = 1024
STDERR_KEEP_LINES = 40

HOST_ERROR = "host error"
DEFAULT_DENY = "Denied by XO."
WITHDRAWN = "codex withdrew the request"
STOPPED_NOTE = ("The person stopped sub-agent {title} ({run}). Don't wait for it, and don't start it again "
                "unless they ask.")
DENY_NOTE = "XO denied {what}: {why}"
PLAN_APPROVED = "The person approved your plan. Carry it out now."
#: The kinds of action each neutral tool ref a spec may deny stands for.
DENIED_KINDS = {"edit": ("edit_file",), "create": ("create_file",), "delete": ("delete_file",),
                "shell": ("command",)}
#: Server requests a client of codex may receive that XO doesn't serve.
REFUSED_REQUESTS = frozenset({"account/chatgptAuthTokens/refresh", "attestation/generate"})
_WITHDRAWN = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexSession:
    """``contract.Session`` on one codex app-server."""

    def __init__(self, *, session_id: str, spec: SessionSpec, report: SupportReport, host: Host, binary: str,
                 codex_home: Path, guard: UsageGuard) -> None:
        self.id = session_id
        self.native_id = ""
        self.report = report
        self.state = "starting"
        self.cli_version: str | None = None
        self.model: str | None = spec.model
        self._spec = spec
        self._mode: str = spec.permissions.mode
        self._host = host
        self._binary = binary
        self._codex_home = codex_home
        self._guard = guard
        self._coordinator = spec.orchestration == "coordinator"
        #: What the spec denies that codex asks about: declined here, never asked.
        self._denied_kinds = frozenset(k for ref in spec.tools.deny for k in DENIED_KINDS.get(ref, ()))
        self._wire = Wire(cwd=spec.cwd, runs_detail=spec.runs_detail, coordinator=self._coordinator,
                          thinking=spec.thinking == "summary")
        self._seq = 0
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._tail: list[Event] = []
        self._finished = asyncio.Event()
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        self._emit_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._consumer = False
        self._handle: ProcessHandle | None = None
        self._rpc = Rpc(self._write_bytes)
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._handlers: dict[Any, asyncio.Task] = {}
        self._withdrawn: set[Any] = set()
        self._answered: set[Any] = set()          # requests whose answer is on its way
        self._session_allowed: set[str] = set()
        self._notes: list[str] = []             # system notes waiting for the root's next input
        self._after: dict[Any, Callable[[], Awaitable[None]]] = {}   # what follows a request's answer
        self._collaboration: str | None = "plan" if spec.permissions.mode == "plan" else None
        self._plan_active = False
        self._plan_task: asyncio.Task | None = None
        self._plan_starting: asyncio.Future | None = None   # an approved plan's turn, being started
        self._fingerprint: str | None = None
        self._end_reason: str | None = None
        self._failure: Out | None = None
        self._ending = False
        self._closing: asyncio.Task | None = None
        self._before_start: list[Out] | None = []   # what codex said before SessionStarted could go out

    # ── start ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the app-server, ``initialize``, then start (or resume, or
        fork) the thread. Raises RuntimeFailure and leaves nothing behind
        when it can't."""
        try:
            notices = await self._attach()
        except BaseException:
            await self._discard()
            raise
        early, self._before_start = self._before_start or [], None
        await self._deliver([Out(SessionStarted, {"model": self.model, "cwd": self._spec.cwd,
                                                  "cli_version": self.cli_version}), *notices, *early])
        self.state = "idle"

    async def _attach(self) -> list[Out]:
        spec = self._spec
        if not await asyncio.to_thread(os.path.isdir, spec.cwd):
            raise self._missing_cwd()
        env = await asyncio.to_thread(session_env, spec_env=spec.env,
                                      server_env=[s.env for s in spec.mcp_servers if s.transport == "http"],
                                      label=f"codex session {self.id}")
        self._fingerprint = await asyncio.to_thread(login_fingerprint, self._codex_home)
        try:
            handle = await self._host.launcher.spawn([self._binary, "app-server", "--listen", "stdio://"],
                                                     cwd=spec.cwd, env=env, label=f"storm codex {self.id}")
        except OSError as exc:
            if not await asyncio.to_thread(os.path.isdir, spec.cwd):
                raise self._missing_cwd() from None
            raise RuntimeFailure("cli_missing", f"the codex CLI can't be started ({self._binary}: "
                                                f"{exc.strerror or exc}); install it, or point CODEX_CLI_PATH at "
                                                "an executable codex") from exc
        self._handle = handle
        self._reader = asyncio.create_task(self._read(handle), name=f"codex-reader:{self.id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(handle), name=f"codex-stderr:{self.id}")
        reply = await self._rpc.request("initialize", initialize_params(), timeout=START_TIMEOUT_S)
        self.cli_version = version_of(reply.get("userAgent")) or self.cli_version
        await self._rpc.notify("initialized")
        thread = p.thread_params(spec, mode=self._mode)
        if spec.resume and spec.fork:
            result = await self._rpc.request("thread/fork", {"threadId": spec.resume, **thread},
                                             timeout=START_TIMEOUT_S)
        elif spec.resume:
            result = await self._rpc.request("thread/resume", {"threadId": spec.resume, **thread},
                                             timeout=START_TIMEOUT_S)
        else:
            result = await self._rpc.request("thread/start", thread, timeout=START_TIMEOUT_S)
        info = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = info.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeFailure("protocol", "codex started no thread (thread/start answered without an id)")
        self.native_id = thread_id
        self._wire.thread_id = thread_id
        status = info.get("status") if isinstance(info.get("status"), dict) else {}
        self._wire.root_idle = status.get("type", "idle") == "idle"
        if isinstance(result.get("model"), str):
            self.model = result["model"]
        notices: list[Out] = []
        if spec.model and isinstance(result.get("model"), str) and result["model"] != spec.model:
            notices.append(Out(Notice, {"code": "model_unknown",
                                        "message": f"codex runs {result['model']}, not {spec.model}"}))
        return notices

    def _missing_cwd(self) -> RuntimeFailure:
        return RuntimeFailure("spec_refused", f"the session's folder {self._spec.cwd} doesn't exist; recreate it, "
                                              "or open the session with a folder that exists")

    async def _discard(self) -> None:
        """Undo a start that failed: the process, the tasks."""
        self._end_reason = "start_failed"
        if self._handle is not None:
            await self._handle.terminate(KILL_GRACE_S)
        for task in (self._reader, self._stderr_task):
            if task is not None:
                await _cancel_and_wait(task, READER_DRAIN_S)
        self._rpc.fail_all(RuntimeFailure("process_died", "codex's session failed to start"))
        self.state = "failed"

    # ── events ───────────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[Event]:
        if self._consumer:
            raise RuntimeError(f"session {self.id}: events() already has a consumer")
        self._consumer = True
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

    def _stamp(self, out: Out, raw: dict[str, Any] | None) -> Event:
        self._seq += 1
        fields = dict(out.fields)
        fields.setdefault("turn_id", self._wire.current_turn)
        return out.cls(session=self.id, native_id=self.native_id or None, ts=_now(), seq=self._seq,
                       raw=raw if self._spec.debug_raw else None, **fields)

    async def _deliver(self, outs: list[Out], *, raw: dict[str, Any] | None = None) -> None:
        if not outs:
            return
        if self._before_start is not None:
            self._before_start.extend(outs)     # SessionStarted comes first, always
            return
        async with self._emit_lock:
            for out in outs:
                await self._queue.put(self._stamp(out, raw))
        if self._wire.current_turn is None:
            self._turn_idle.set()
        else:
            self._turn_idle.clear()

    # ── the reader ───────────────────────────────────────────────────────

    async def _read(self, handle: ProcessHandle) -> None:
        failure: Out | None = None
        try:
            async for raw in handle.lines():
                await self._dispatch(raw)
        except LineTooLong as exc:
            failure = Out(Error, {"code": "protocol", "message": str(exc), "fatal": True})
            self._end_reason = self._end_reason or "protocol"
            await handle.terminate(KILL_GRACE_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a reader bug must still end the session cleanly
            log.exception("codex session %s: the reader failed", self.id)
            failure = Out(Error, {"code": "protocol", "message": f"reading codex's output failed: {exc}",
                                  "fatal": True})
            self._end_reason = self._end_reason or "protocol"
            await handle.terminate(KILL_GRACE_S)
        code = await handle.wait()
        await self._ended(code, failure)

    async def _dispatch(self, raw: bytes) -> None:
        line = classify(raw)
        if line.kind == "response":
            self._rpc.resolve(line.msg)
        elif line.kind == "request":
            self._on_request(line)
        elif line.kind == "notification":
            await self._on_notification(line)
        else:
            await self._deliver(self._wire.unknown("a line that is not a JSON-RPC message"))

    async def _on_notification(self, line: Line) -> None:
        method, params = line.method, line.params
        if method == "serverRequest/resolved":
            self._on_resolved(params.get("requestId"))
            return
        if method == "account/rateLimits/updated":
            self._guard.update(params.get("rateLimits"))
        outs = self._wire.notification(method, params)
        await self._deliver(outs, raw=line.msg)
        if method == "thread/status/changed" and params.get("threadId") == self.native_id:
            self._thread_state(params.get("status"))
        if any(out.cls is TurnFinished for out in outs):
            await self._check_login()
        if self._wire.pending_plan is not None and self._plan_task is None:
            turn, plan = self._wire.pending_plan
            self._plan_task = asyncio.create_task(self._approve_plan(turn, plan), name=f"codex-plan:{self.id}")

    def _thread_state(self, status: object) -> None:
        if self.state in ("closing", "closed", "failed") or not isinstance(status, dict):
            return
        kind = status.get("type")
        if kind == "idle":
            self.state = "idle"
        elif kind == "active":
            flags = status.get("activeFlags") if isinstance(status.get("activeFlags"), list) else []
            self.state = "waiting_host" if "waitingOnApproval" in flags or self._handlers else "running"

    async def _read_stderr(self, handle: ProcessHandle) -> None:
        async for line in handle.stderr():
            self._stderr.append(line.decode(errors="replace"))

    async def _ended(self, code: int | None, failure: Out | None) -> None:
        """The process is gone: release everything, report it, end events()."""
        reason = self._end_reason or "process_died"
        failure = failure or self._failure
        for handler in list(self._handlers.values()):
            handler.cancel()
        if self._stderr_task is not None:
            done, _ = await asyncio.wait({self._stderr_task}, timeout=KILL_GRACE_S)
            if not done:
                await _cancel_and_wait(self._stderr_task, KILL_GRACE_S)
        tail = " | ".join(list(self._stderr)[-5:])[:500]
        self._rpc.fail_all(RuntimeFailure("process_died", f"codex exited (code {code}) before answering"
                                                          + (f": {tail}" if tail else "")))
        quiet = reason in ("closed", "interrupted")
        async with self._emit_lock:
            self._ending = True
            turn = self._wire.current_turn
            outs: list[Out] = []
            if failure is not None:
                outs.append(Out(failure.cls, {**failure.fields, "turn_id": turn}))
            elif reason == "process_died":
                outs.append(Out(Error, {"code": "process_died", "fatal": True, "retryable": True, "turn_id": turn,
                                        "message": f"codex exited unexpectedly with code {code}"
                                                   + (f": {tail}" if tail else "")}))
            outs += self._wire.close_turn(status="interrupted" if quiet else "failed",
                                          error=None if quiet else f"codex exited ({code})")
            outs += self._wire.cancel_all_tools()
            outs += self._wire.end_live_runs("the session's process ended")
            outs.append(Out(SessionClosed, {"reason": reason, "exit_code": code}))
            self._tail.extend(self._stamp(out, None) for out in outs)
        self.state = "closed" if reason == "closed" else "failed"
        self._turn_idle.set()
        self._finished.set()

    async def _write_bytes(self, data: bytes) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeFailure("process_died", "codex is not running")
        async with self._write_lock:
            try:
                await handle.write(data)
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "codex's input is closed: the process has ended") from exc

    # ── requests codex sends ─────────────────────────────────────────────

    def _on_request(self, line: Line) -> None:
        request_id = line.msg.get("id")
        if request_id in self._handlers:
            return
        method = line.method
        handlers: dict[str, Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "item/commandExecution/requestApproval": self._command_approval,
            "item/fileChange/requestApproval": self._change_approval,
            "item/permissions/requestApproval": self._permissions_approval,
            "item/tool/requestUserInput": self._user_input,
            "mcpServer/elicitation/request": self._elicitation,
            "item/tool/call": self._tool_call,
            "currentTime/read": self._current_time,
            "applyPatchApproval": self._legacy_approval,
            "execCommandApproval": self._legacy_approval,
        }
        handler = handlers.get(method)
        if handler is None:
            task = asyncio.create_task(self._refuse(request_id, method), name=f"codex-request:{request_id}")
        else:
            task = asyncio.create_task(self._answer(request_id, method, handler, line.params),
                                       name=f"codex-request:{request_id}")
        self._handlers[request_id] = task
        task.add_done_callback(lambda _t: self._handlers.pop(request_id, None))

    async def _refuse(self, request_id: Any, method: str) -> None:
        if method not in REFUSED_REQUESTS:
            await self._deliver(self._wire.unknown(f"request {method}"))
        with contextlib.suppress(RuntimeFailure):
            await self._rpc.respond_error(request_id, METHOD_NOT_FOUND, f"XO doesn't serve {method}")

    async def _answer(self, request_id: Any, method: str,
                      handler: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]],
                      params: dict[str, Any]) -> None:
        """Answer one request of codex's. An approval whose handling fails
        for any reason is declined, never left unanswered (fail closed)."""
        try:
            result = await handler(request_id, params)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bug here must still answer, and an approval is declined
            log.exception("codex session %s: answering %s %s failed", self.id, method, request_id)
            result = _refusal(method)
        self._answered.add(request_id)
        with contextlib.suppress(RuntimeFailure):
            await self._rpc.respond(request_id, result)
        after = self._after.pop(request_id, None)
        if after is not None:
            await after()

    def _on_resolved(self, request_id: object) -> None:
        """codex settled a request itself (its turn ended, it was
        interrupted): stop waiting for the host on it."""
        if request_id in self._answered:
            self._answered.discard(request_id)       # our own answer, confirmed
            return
        task = self._handlers.get(request_id)
        if task is not None and not task.done():
            self._withdrawn.add(request_id)
            task.cancel()

    async def _command_approval(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        run = self._wire.run_of(params.get("threadId"))
        call = describe_command(params)
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else self._spec.cwd
        if await asyncio.to_thread(skill_read, params.get("commandActions"), cwd, self._codex_home):
            return {"decision": "accept"}       # codex reading its own skills, as claude's Skill tool
        item_id = params.get("itemId") if isinstance(params.get("itemId"), str) else None
        req = self._request(request_id, run, item_id, call, {"command": call.command or "", "cwd": cwd},
                            reason=params.get("reason"))
        decision = await self._refused(req) or await self._decide(request_id, req)
        return self._verdict(request_id, decision, req, params)

    async def _change_approval(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        run = self._wire.run_of(params.get("threadId"))
        item_id = params.get("itemId") if isinstance(params.get("itemId"), str) else None
        item = self._wire.item(item_id) or {}
        call = describe_change(item) if item else Call("edit_file", "apply_patch", "Change files", None,
                                                       "change files (codex didn't say which)")
        if self._accepts_edits() and call.paths and await asyncio.to_thread(own_edit, call.paths, self._spec.cwd):
            return {"decision": "accept"}
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        req = self._request(request_id, run, item_id, call, {"changes": changes}, reason=params.get("reason"))
        decision = await self._refused(req) or await self._decide(request_id, req)
        return self._verdict(request_id, decision, req, params)

    async def _refused(self, req: PermissionRequest) -> PermissionDecision | None:
        """A deny the spec itself makes (``tools.deny``), before any host:
        reported like any answer. Never for a coordinator, whose asks all go
        to the host's read-only policy (it denies these too)."""
        if self._coordinator or req.kind not in self._denied_kinds:
            return None
        decision = PermissionDecision(allow=False, message=f"this session may not {req.kind.replace('_', ' ')}")
        await self._deliver([Out(PermissionRequested, {"request": req, "run": req.run}),
                             Out(PermissionResolved, {"request_id": req.request_id, "decision": decision,
                                                      "run": req.run})])
        return decision

    def _accepts_edits(self) -> bool:
        """``accept_edits``, and this runtime's emulated ``auto`` (codex's own
        reviewer can't run here: ch 03 §4, C-auto2), take a thread's own
        edits unasked. Never a coordinator's."""
        return not self._coordinator and self._mode in ("accept_edits", "auto")

    async def _permissions_approval(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """codex asks for more than its sandbox allows. No session runs one
        (§0.67), so this only comes from a sub-agent's or a tool's own
        profile: the host decides, and nothing more is granted on a no."""
        run = self._wire.run_of(params.get("threadId"))
        wanted = params.get("permissions") if isinstance(params.get("permissions"), dict) else {}
        reason = params.get("reason") if isinstance(params.get("reason"), str) else None
        call = Call("other", "permissions", "Grant codex more permissions", None,
                    one_line(f"grant codex more permissions: {reason or json.dumps(wanted)}"))
        req = self._request(request_id, run, params.get("itemId"), call, {"permissions": wanted}, reason=reason)
        if self._coordinator:
            decision = PermissionDecision(allow=False, message="a coordinator is read-only")
            await self._deliver([Out(PermissionRequested, {"request": req, "run": run}),
                                 Out(PermissionResolved, {"request_id": req.request_id, "decision": decision,
                                                          "run": run})])
        else:
            decision = await self._decide(request_id, req)
        granted = {k: v for k, v in wanted.items() if k in ("fileSystem", "network")} if decision.allow else {}
        return {"permissions": granted, "scope": "turn"}

    async def _legacy_approval(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """codex's v1 approval shapes: never expected from a v2 thread, so
        refused (and reported) rather than guessed at."""
        await self._deliver(self._wire.unknown("a v1 approval request"))
        return {"decision": "denied"}

    async def _current_time(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"currentTimeAt": int(datetime.now(timezone.utc).timestamp())}

    async def _tool_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        call_id = params.get("callId") if isinstance(params.get("callId"), str) else str(request_id)
        name = str(params.get("tool") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            result = await self._host.call_tool(call_id, name, args)
        except Exception:  # noqa: BLE001 - a failing host tool is the agent's error, never a crash
            log.exception("codex session %s: host tool %s failed", self.id, name)
            return {"contentItems": [{"type": "inputText", "text": HOST_ERROR}], "success": False}
        return {"contentItems": [{"type": "inputText", "text": result.content}], "success": not result.is_error}

    async def _elicitation(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """An MCP server asks for input. XO's own servers never do; for the
        others the host decides whether codex may go ahead."""
        server = str(params.get("serverName") or "")
        spec_server = next((s for s in self._spec.mcp_servers if s.name == server), None)
        if spec_server is not None and spec_server.auto_approve:
            return {"action": "accept", "content": {}}
        if params.get("mode") not in ("form", "openai/form", "openaiForm"):
            return {"action": "decline"}
        run = self._wire.run_of(params.get("threadId"))
        message = str(params.get("message") or f"{server} asks to go ahead")
        tool = _elicited_tool(params)
        call_id = self._wire.open_mcp_call(params.get("threadId"), server, tool)
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        arguments = meta.get("tool_params") if isinstance(meta.get("tool_params"), dict) else {}
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()
        call = Call("mcp", f"{server}/{tool or 'elicitation'}", one_line(message),
                    f"{server}/{tool}" if tool else server,
                    one_line(f"call `{tool}` on `{server}`" if tool else message), mcp=(server, tool or ""),
                    content_sha256=digest)
        req = self._request(request_id, run, call_id, call, {"server": server, "tool": tool, "arguments": arguments})
        decision = await self._decide(request_id, req)
        return {"action": "accept", "content": {}} if decision.allow else {"action": "decline"}

    async def _user_input(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        run = self._wire.run_of(params.get("threadId"))
        raw = params.get("questions") if isinstance(params.get("questions"), list) else []
        questions = tuple({
            "id": str(q.get("id") or n), "header": q.get("header"), "question": q.get("question"),
            "options": [{"label": o.get("label"), "description": o.get("description")}
                        for o in q.get("options") or [] if isinstance(o, dict)],
            "multi": False, "secret": bool(q.get("isSecret")),
        } for n, q in enumerate(raw) if isinstance(q, dict))
        rid = str(request_id)
        question = Question(request_id=rid, run=run, questions=questions, run_title=self._wire.run_title(run))
        await self._deliver([Out(QuestionAsked, {"question": question, "run": run})])
        failed = object()
        answer = await self._ask_host(
            request_id, lambda: self._host.answer_question(question),
            resolved=lambda a: Out(QuestionAnswered, {"request_id": rid, "run": run,
                                                      "answer": dict(a) if isinstance(a, Mapping) else {}}),
            valid=lambda a: isinstance(a, Mapping), fallback=failed)
        if not isinstance(answer, Mapping):
            return {"answers": {}}
        return {"answers": {str(key): {"answers": [str(v) for v in value] if isinstance(value, (list, tuple))
                                       else [str(value)]} for key, value in answer.items()}}

    def _request(self, request_id: Any, run: str | None, call_id: object, call: Call, inp: dict[str, Any], *,
                 reason: object = None) -> PermissionRequest:
        return PermissionRequest(
            request_id=str(request_id), run=run, call_id=call_id if isinstance(call_id, str) else None,
            tool=call.tool, kind=call.kind, summary=call.summary, input=inp, cwd=self._spec.cwd, paths=call.paths,
            command=call.command, url=call.url, mcp=call.mcp, content_sha256=call.content_sha256,
            run_title=self._wire.run_title(run), reason=reason if isinstance(reason, str) else None,
            scopes=("once", "session"))

    async def _decide(self, request_id: Any, req: PermissionRequest) -> PermissionDecision:
        """The host's answer, as long as it takes. Scope ``session`` is kept
        here, never written to codex's settings (ch 03 §2)."""
        await self._deliver([Out(PermissionRequested, {"request": req, "run": req.run})])

        def resolved(answer: object) -> Out:
            decision = answer if isinstance(answer, PermissionDecision) \
                else PermissionDecision(allow=False, message=WITHDRAWN)
            return Out(PermissionResolved, {"request_id": req.request_id, "decision": decision, "run": req.run})

        key = json.dumps([req.kind, req.command, list(req.paths), req.mcp, req.content_sha256], default=str)
        if key in self._session_allowed:
            decision = PermissionDecision(allow=True, scope="session")
            await self._deliver([resolved(decision)])
            return decision
        decision = await self._ask_host(request_id, lambda: self._host.decide_permission(req),
                                        resolved=resolved, valid=lambda a: isinstance(a, PermissionDecision),
                                        fallback=PermissionDecision(allow=False, message=HOST_ERROR))
        if decision.allow and decision.scope == "session":
            self._session_allowed.add(key)
        return decision

    def _verdict(self, request_id: Any, decision: PermissionDecision, req: PermissionRequest,
                 params: dict[str, Any]) -> dict[str, Any]:
        """codex's answer. A deny with a reason also steers the reason into
        the asking thread's turn once the decline is sent, since ``decline``
        can't carry it (R3)."""
        if decision.allow:
            return {"decision": "accept"}
        if req.call_id:
            self._wire.denied.add(req.call_id)
        if decision.stop_turn:
            return {"decision": "cancel"}
        message = decision.message or DEFAULT_DENY
        thread = params.get("threadId") if isinstance(params.get("threadId"), str) else self.native_id
        turn = params.get("turnId") if isinstance(params.get("turnId"), str) else None
        note = DENY_NOTE.format(what=req.summary, why=message)
        self._after[request_id] = lambda: self._note(thread, turn, note)
        return {"decision": "decline"}

    async def _note(self, thread: str, turn: str | None, text: str) -> None:
        """Steer a system note into a running turn; for the root with no turn
        running, it goes with the next input instead."""
        if turn:
            try:
                await self._rpc.request("turn/steer", {"threadId": thread, "expectedTurnId": turn,
                                                       "input": p.note_input(text)}, timeout=REQUEST_TIMEOUT_S)
                return
            except RuntimeFailure as exc:
                log.info("codex session %s: a note couldn't join turn %s (%s)", self.id, turn, exc.message)
        if thread == self.native_id:
            self._notes.append(text)

    async def _ask_host(self, request_id: Any, call: Callable[[], Awaitable[Any]], *,
                        resolved: Callable[[object], Out], valid: Callable[[Any], bool], fallback: Any) -> Any:
        """Wait for the host, as long as it takes. A host that raises or
        answers something malformed gets ``fallback``, a deny: fail closed.
        A request codex settles itself is recorded as resolved, then the wait
        is cancelled."""
        if self.state in ("idle", "running"):
            self.state = "waiting_host"
        try:
            answer = await call()
        except asyncio.CancelledError:
            if request_id in self._withdrawn:
                self._withdrawn.discard(request_id)
                await asyncio.shield(self._deliver([resolved(_WITHDRAWN)]))
            raise
        except Exception:  # noqa: BLE001 - any host failure is a deny, never an allow
            log.exception("codex session %s: the host failed to answer %s", self.id, request_id)
            answer = fallback
        else:
            if not valid(answer):
                log.error("codex session %s: the host answered %s with a %s", self.id, request_id,
                          type(answer).__name__)
                answer = fallback
        finally:
            if self.state == "waiting_host" and len(self._handlers) <= 1:
                self.state = "running" if self._wire.current_turn else "idle"
        await self._deliver([resolved(answer)])
        return answer

    # ── the operations core calls ────────────────────────────────────────

    def _require_live(self) -> None:
        if self.state in ("closing", "closed", "failed") or self._finished.is_set():
            raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")

    async def send(self, msg: UserInput) -> str:
        """Start a turn, or add to the running one (``turn/steer``: codex
        joins it to the turn in progress, V2), returning that turn's id."""
        self._require_live()
        self._guard.check()
        items = await asyncio.to_thread(p.user_input, msg)
        async with self._send_lock:
            await self._check_login()
            if self._ending or self._handle is None:
                raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")
            await self._withdraw_plan(end_input=False)    # the person moved on before deciding it
            turn = self._wire.current_turn
            if turn is not None:
                steered = await self._steer(turn, items, msg.context)
                if steered:
                    return turn
            return await self._start_turn(msg)

    async def _steer(self, turn: str, items: list[dict[str, Any]], context: str | None) -> bool:
        body: dict[str, Any] = {"threadId": self.native_id, "expectedTurnId": turn, "input": items}
        if context:
            body["additionalContext"] = p.context(context)
        try:
            await self._rpc.request("turn/steer", body, timeout=REQUEST_TIMEOUT_S)
        except RpcError as exc:
            # The turn ended as this was sent: the message starts the next one.
            log.info("codex session %s: steering turn %s failed (%s); starting a turn", self.id, turn, exc.message)
            try:
                await asyncio.wait_for(self._turn_idle.wait(), REQUEST_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise RuntimeFailure("protocol", f"codex refused to add to its running turn: {exc.message}") from exc
            return False
        return True

    async def _start_turn(self, msg: UserInput) -> str:
        notes, self._notes = self._notes, []
        context = "\n\n".join([*notes, *([msg.context] if msg.context else [])]) or None
        effective = replace(msg, context=context)
        collaboration = None
        if self._mode == "plan" and not self._plan_active:
            collaboration, self._plan_active = "plan", True
        elif self._mode != "plan" and self._plan_active:
            collaboration, self._plan_active = "default", False
        body = p.turn_params(self.native_id, effective, spec=self._spec, mode=self._mode, model=self.model,
                             collaboration=collaboration)
        self._wire.plan_mode = self._mode == "plan"
        self._wire.expect_turn(msg.origin)
        try:
            result = await self._rpc.request("turn/start", body, timeout=REQUEST_TIMEOUT_S)
        except RpcError as exc:
            self._wire.drop_expected()
            running = self._wire.current_turn
            if running is not None and await self._steer(running, body["input"], context):
                return running             # codex began a turn of its own meanwhile: this joins it
            self._notes = notes + self._notes
            raise RuntimeFailure("protocol", f"codex didn't start the turn: {exc.message}", retryable=True) from exc
        except BaseException:
            self._wire.drop_expected()
            self._notes = notes + self._notes
            raise
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise RuntimeFailure("protocol", "codex started a turn without an id")
        await self._deliver(self._wire.start_turn(turn_id))
        if self.state == "idle" and self._wire.current_turn == turn_id:
            self.state = "running"
        return turn_id

    async def interrupt(self, grace_s: float = 10) -> None:
        """codex's own interrupt: the turn ends ``interrupted``, the thread
        stays. Live sub-agents go on (L10): their runs move to the background.
        If the turn hasn't ended after ``grace_s``, the process group is
        terminated."""
        if self._handle is None or self._finished.is_set() or self.state == "closing":
            return
        if self._wire.current_turn is None and self._plan_starting is not None:
            # An approved plan's turn is starting: stop it once it has an id.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._plan_starting), REQUEST_TIMEOUT_S)
        turn = self._wire.current_turn
        if turn is None:
            await self._withdraw_plan(end_input=True)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_s
        with contextlib.suppress(RuntimeFailure):
            await self._rpc.request("turn/interrupt", {"threadId": self.native_id, "turnId": turn},
                                    timeout=max(grace_s, 0.01))
        try:
            await asyncio.wait_for(self._turn_idle.wait(), max(deadline - loop.time(), 0))
            return
        except asyncio.TimeoutError:
            pass
        await self._deliver([Out(Notice, {"code": "interrupt_escalated",
                                          "message": f"codex did not stop within {grace_s:g} s; "
                                                     "its process group was terminated"})])
        self._end_reason = self._end_reason or "interrupted"
        handle = self._handle
        if handle is not None:
            await handle.terminate(KILL_GRACE_S)
            await self._await_end(handle)

    async def update(self, *, model: str | None = None, effort: Effort | None = None,
                     permission_mode: PermissionMode | None = None) -> SupportReport:
        """codex takes model, effort and approval policy with each
        ``turn/start``: every change applies from the next turn, no restart."""
        self._require_live()
        items: list[SupportItem] = []
        if model is not None:
            self._spec = replace(self._spec, model=model)
            self.model = model
            items.append(_native("update.model", "turn/start.model"))
        if effort is not None:
            self._spec = replace(self._spec, effort=effort)
            items.append(_native("update.effort", "turn/start.effort"))
        if permission_mode is not None:
            if permission_mode == "bypass" and self._spec.permissions.mode != "bypass":
                items.append(SupportItem(feature="update.permission_mode", support="refused",
                                         mechanism="turn/start.approvalPolicy", enforced_by="none",
                                         note="bypass only for a session whose spec asked for it"))
            elif self._coordinator and permission_mode in p.COORDINATOR_REFUSED:
                items.append(SupportItem(feature="update.permission_mode", support="refused",
                                         mechanism="turn/start.approvalPolicy", enforced_by="none",
                                         note=f"{permission_mode} would let a coordinator's workers act unasked"))
            else:
                self._mode = permission_mode
                items.append(SupportItem(feature="update.permission_mode", support="native",
                                         mechanism="this runtime's answers + turn/start.approvalPolicy",
                                         enforced_by="host"))
        return SupportReport(items=tuple(items))

    async def stop_run(self, run_id: str) -> None:
        """Stop one sub-agent: interrupt its turn, then tell the root it was
        stopped, labelled a system note (§0.69), or its ``wait_agent`` would
        go on waiting. The stop is confirmed by the ``RunFinished`` that
        follows."""
        self._require_live()
        if run_id not in self._wire.live_runs():
            raise RuntimeFailure("session_not_found", f"no live sub-agent {run_id} in session {self.id}")
        self._wire.mark_stopping(run_id)
        child_turn = self._wire.run_turn(run_id)
        if child_turn is not None:
            await self._rpc.request("turn/interrupt", {"threadId": run_id, "turnId": child_turn},
                                    timeout=REQUEST_TIMEOUT_S)
        else:
            await self._deliver(self._wire.finish_run(run_id, "stopped", "stopped by the person"))
        title = self._wire.run_title(run_id) or "a sub-agent"
        await self._note(self.native_id, self._wire.current_turn, STOPPED_NOTE.format(title=title, run=run_id))

    async def send_to_run(self, run_id: str, msg: UserInput) -> None:
        raise RuntimeFailure("unsupported", "messaging a sub-agent is not offered this release (§0.82)")

    async def close(self, grace_s: float = 5) -> None:
        """Terminate the process group and wait until everything is released.
        Idempotent; safe while a permission is held (the wait is cancelled)."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(grace_s), name=f"codex-close:{self.id}")
        await asyncio.shield(self._closing)

    async def _close(self, grace_s: float) -> None:
        if self._finished.is_set():
            return
        self._end_reason = self._end_reason or "closed"
        self.state = "closing"
        for handler in [*self._handlers.values(), *([self._plan_task] if self._plan_task else [])]:
            handler.cancel()
        handle = self._handle
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.close_stdin()
            await handle.terminate(grace_s)
            await self._await_end(handle)
        if not self._finished.is_set():
            await self._ended(None, None)

    async def _await_end(self, handle: ProcessHandle) -> None:
        reader = self._reader
        if reader is None or self._finished.is_set():
            return
        done, _ = await asyncio.wait({reader}, timeout=READER_DRAIN_S)
        if done:
            return
        await _cancel_and_wait(reader, READER_DRAIN_S)
        if not self._finished.is_set():
            await self._ended(await handle.wait(), None)

    async def _withdraw_plan(self, *, end_input: bool) -> None:
        """Withdraw a plan's question still waiting for the host; with
        ``end_input``, its input ends (a Stop), else a new turn follows."""
        task = self._plan_task
        if task is None:
            return
        if self._wire.pending_plan is not None:
            self._withdrawn.add(f"plan-{self._wire.pending_plan[0]}")
        task.cancel()
        await asyncio.wait({task}, timeout=READER_DRAIN_S)
        self._plan_task = None
        if end_input:
            await self._deliver(self._wire.release_idle())
        else:
            self._wire.pending_plan = None

    async def _approve_plan(self, turn: str, plan: str) -> None:
        """codex's plan mode ended its turn with a plan: ask the host to go
        ahead, as claude's plan mode does (its ``ExitPlanMode``). Yes: the
        session leaves plan mode and carries the plan out in a new turn. No:
        the input ends, and the reason goes with the next one."""
        req = PermissionRequest(request_id=f"plan-{turn}", run=None, call_id=None, tool="plan", kind="plan",
                                summary="go ahead with this plan", input={"plan": plan}, cwd=self._spec.cwd,
                                plan=plan, scopes=("once",))
        try:
            decision = await self._decide(req.request_id, req)
            if decision.allow and not self._ending:
                self._mode = "ask"
                self._wire.pending_plan = None
                self._plan_task = None          # this task carries on as the plan's own new input
                self._plan_starting = asyncio.ensure_future(self.send(UserInput(text=PLAN_APPROVED, origin="system")))
                try:
                    await asyncio.shield(self._plan_starting)
                finally:
                    self._plan_starting = None
                return
            if decision.message:
                self._notes.append(DENY_NOTE.format(what="the plan", why=decision.message))
        except asyncio.CancelledError:
            raise
        except RuntimeFailure as exc:
            log.info("codex session %s: carrying out the approved plan failed (%s)", self.id, exc.message)
        finally:
            self._plan_task = None
        await self._deliver(self._wire.release_idle())

    async def _check_login(self) -> None:
        """At a turn boundary: has the person connected, disconnected or
        switched the codex login since this process started? Reported once
        per change as a ``Notice`` with code ``credentials_changed``; the
        engine recycles the session at its next boundary (§0.79)."""
        if self._fingerprint is None:
            return
        now = await asyncio.to_thread(login_fingerprint, self._codex_home)
        if now != self._fingerprint:
            self._fingerprint = now
            await self._deliver([Out(Notice, {"code": "credentials_changed",
                                              "message": "the codex login changed since this process started; "
                                                         "recycle the session to use it"})])


def _native(feature: str, mechanism: str) -> SupportItem:
    return SupportItem(feature=feature, support="native", mechanism=mechanism, enforced_by="cli")


_TOOL_ASKED = re.compile(r'run tool "([^"]+)"\?\s*$')


def _elicited_tool(params: dict[str, Any]) -> str | None:
    """The MCP tool an elicitation asks about. codex 0.162.1 asks about a
    tool call of a server that isn't auto-approved as a form elicitation
    (``_meta.codex_approval_kind: mcp_tool_call``) whose message names the
    tool: 'Allow the x MCP server to run tool "post_message"?'."""
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    for key in ("tool", "tool_name", "toolName"):
        if isinstance(meta.get(key), str) and meta[key]:
            return meta[key]
    found = _TOOL_ASKED.search(str(params.get("message") or ""))
    return found.group(1) if found else None


def _refusal(method: str) -> dict[str, Any]:
    """What a request gets when answering it failed: never a yes."""
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "decline"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline"}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    if method == "item/tool/call":
        return {"contentItems": [{"type": "inputText", "text": HOST_ERROR}], "success": False}
    if method in ("applyPatchApproval", "execCommandApproval"):
        return {"decision": "denied"}
    return {}


async def _cancel_and_wait(task: asyncio.Task, timeout: float) -> None:
    if not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=timeout)
