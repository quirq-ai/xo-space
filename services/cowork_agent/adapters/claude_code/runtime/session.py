"""One claude session: a long-lived stream-json control-protocol process
(storm ch 02 §1–§4).

The session owns its process from spawn to reap. One reader task reads
claude's stdout; each ``can_use_tool`` claude sends gets its own task, held
with no timeout until the host answers (V4). Events go to one bounded queue in
``seq`` order; the last ones (cancelled calls, ``SessionClosed``) are kept
apart, so a consumer that stopped reading can never block ``close``.

A session lives as long as its process. When the process ends (``close``, an
``interrupt`` that had to kill it, a crash), the final events are
``TurnFinished`` for an open turn, ``ToolFinished(cancelled)`` for every open
call, ``Error`` for a crash, then ``SessionClosed``; the engine resumes by
native id with a new ``open``. The one internal restart is ``update(effort)``
(claude takes effort only at launch), done between turns.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import os
import shutil
import threading
import uuid
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from services.cowork_agent.adapters.claude_code.runtime import command
from services.cowork_agent.adapters.claude_code.runtime.account import UsageGuard, login_fingerprint
from services.cowork_agent.adapters.claude_code.runtime.env import session_env
from services.cowork_agent.adapters.claude_code.runtime.probe import spawn_cli
from services.cowork_agent.adapters.claude_code.runtime.tools import content_sha256, describe_call
from services.cowork_agent.adapters.claude_code.runtime.transcript import remove_empty_project_folder
from services.cowork_agent.adapters.claude_code.runtime.wire import Out, Wire
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

#: How long claude has to answer a control request we send (``initialize``
#: includes starting its MCP servers).
INITIALIZE_TIMEOUT_S = 120.0
CONTROL_TIMEOUT_S = 30.0
#: After SIGTERM, how long the process group has before SIGKILL.
KILL_GRACE_S = 3.0
#: After the process is gone, how long the reader may take to deliver what it
#: already read before the end is reported without it.
READER_DRAIN_S = 5.0
#: Events not yet taken by the consumer; past this the reader waits, and so
#: does claude (its stdout pipe fills).
EVENT_QUEUE_SIZE = 1024
STDERR_KEEP_LINES = 40

HOST_ERROR = "host error"
DEFAULT_DENY = "Denied by XO."
WITHDRAWN = "claude withdrew the request"
CONTEXT_HEADER = "[XO context, not the person's words]"
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_WITHDRAWN = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeSession:
    """``contract.Session`` on one claude process."""

    def __init__(self, *, session_id: str, native_id: str, spec: SessionSpec, report: SupportReport, host: Host,
                 binary: str, folder: Path, claude_home: Path, guard: UsageGuard,
                 credential_rules: tuple[str, ...]) -> None:
        self.id = session_id
        self.native_id = native_id
        self.report = report
        self.state = "starting"
        self.cli_version: str | None = None
        self._spec = spec
        self._host = host
        self._binary = binary
        self._folder = folder
        self._claude_home = claude_home
        self._guard = guard
        self._credential_rules = credential_rules
        self._wire = Wire(cwd=spec.cwd, runs_detail=spec.runs_detail, coordinator=spec.orchestration == "coordinator")
        self._seq = 0
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._tail: list[Event] = []
        self._finished = asyncio.Event()
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        self._emit_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._consumer = False
        self._handle: ProcessHandle | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP_LINES)
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, asyncio.Task] = {}
        self._withdrawn: set[str] = set()
        self._session_allowed: set[str] = set()
        self._fingerprint: str | None = None
        self._pending_effort: Effort | None = None
        self._end_reason: str | None = None       # set when this side ends the process
        self._failure: Out | None = None          # what ended it, when known
        self._ending = False
        self._closing: asyncio.Task | None = None
        # One restart at a time, and never a turn starting while the process
        # is being replaced: send and update(effort) hold this lock; the
        # restart itself runs as a task close() can cancel.
        self._attach_lock = asyncio.Lock()
        self._restart_task: asyncio.Task | None = None
        self._retiring: ProcessHandle | None = None   # the process a restart is replacing
        self._retiring_reader: asyncio.Task | None = None
        # Writing the run files and removing the folder run in threads; this
        # orders them, so a removal is final.
        self._folder_lock = threading.Lock()
        self._folder_removed = False

    # ── start ────────────────────────────────────────────────────────────

    async def start(self, *, resume: str | None) -> None:
        """Spawn claude and complete ``initialize``. Raises RuntimeFailure and
        leaves nothing behind when it can't."""
        try:
            notices = await self._attach(resume)
        except BaseException:
            await self._discard()
            raise
        await self._deliver([Out(SessionStarted, {"model": self._spec.model, "cwd": self._spec.cwd,
                                                  "cli_version": self.cli_version}), *notices])
        self.state = "idle"

    async def _attach(self, resume: str | None) -> list[Out]:
        """Spawn the process and complete ``initialize``; returns the notices
        its reply calls for."""
        if not await asyncio.to_thread(os.path.isdir, self._spec.cwd):
            raise self._missing_cwd()
        try:
            files = await asyncio.to_thread(self._write_run_files)
        except OSError as exc:
            raise RuntimeFailure("spec_refused", f"can't write the session's folder {self._folder}: "
                                                 f"{exc.strerror or exc}") from exc
        argv = command.build_argv(self._binary, self._spec, files, native_id=self.native_id, resume=resume)
        env = await asyncio.to_thread(session_env, switches=command.session_switches(self._spec),
                                      spec_env=self._spec.env, label=f"claude session {self.id}")
        self._fingerprint = await asyncio.to_thread(login_fingerprint, self._claude_home)
        try:
            handle = await spawn_cli(self._host.launcher, argv, cwd=self._spec.cwd, env=env,
                                     label=f"storm claude {self.id}")
        except RuntimeFailure:
            if not await asyncio.to_thread(os.path.isdir, self._spec.cwd):
                raise self._missing_cwd() from None     # the spawn failed on the folder, not the CLI
            raise
        self._handle = handle
        self._reader = asyncio.create_task(self._read(handle), name=f"claude-reader:{self.id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(handle), name=f"claude-stderr:{self.id}")
        request: dict[str, Any] = {"subtype": "initialize", "hooks": None}
        agents = command.agent_definitions(self._spec.agents)
        if agents:
            request["agents"] = agents
        reply = await self._control(request, timeout=INITIALIZE_TIMEOUT_S)
        self.cli_version = reply.get("claude_code_version") if isinstance(reply.get("claude_code_version"), str) \
            else self.cli_version
        return self._initialize_notices(reply)

    def _missing_cwd(self) -> RuntimeFailure:
        return RuntimeFailure("spec_refused", f"the session's folder {self._spec.cwd} doesn't exist; recreate it, "
                                              "or open the session with a folder that exists")

    def _initialize_notices(self, reply: dict[str, Any]) -> list[Out]:
        """``initialize`` answers with the starting mode and the models; a
        mismatch with the spec is reported, not fatal."""
        outs: list[Out] = []
        expected = command.permission_mode(self._spec)
        actual = reply.get("current_permission_mode")
        if isinstance(actual, str) and actual != expected:
            outs.append(Out(Notice, {"code": "permission_mode_mismatch",
                                     "message": f"claude started in {actual}, the spec asked for {expected}"}))
        models = reply.get("models")
        if self._spec.model and isinstance(models, list):
            known = {str(m.get(k)) for m in models if isinstance(m, dict) for k in ("value", "resolvedModel")}
            if self._spec.model not in known:
                outs.append(Out(Notice, {"code": "model_unknown",
                                         "message": f"claude doesn't list the model {self._spec.model}"}))
        return outs

    async def _discard(self) -> None:
        """Undo a start that failed: the process, the tasks, the folder."""
        self._end_reason = "start_failed"
        if self._handle is not None:
            await self._handle.terminate(KILL_GRACE_S)
        for task in (self._reader, self._stderr_task):
            if task is not None:
                await _cancel_and_wait(task, READER_DRAIN_S)
        await asyncio.to_thread(self._remove_folders)
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
        return out.cls(session=self.id, native_id=self.native_id, ts=_now(), seq=self._seq,
                       raw=raw if self._spec.debug_raw else None, **fields)

    async def _deliver(self, outs: list[Out], *, raw: dict[str, Any] | None = None) -> None:
        if not outs:
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
            log.exception("claude session %s: the reader failed", self.id)
            failure = Out(Error, {"code": "protocol", "message": f"reading claude's output failed: {exc}",
                                  "fatal": True})
            self._end_reason = self._end_reason or "protocol"
            await handle.terminate(KILL_GRACE_S)
        code = await handle.wait()
        if handle is not self._handle:
            return                          # a process replaced by update(effort): not the session's end
        await self._ended(code, failure)

    async def _dispatch(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            await self._deliver(self._wire.unknown("a line that is not a JSON object"))
            return
        kind = msg.get("type")
        if kind == "control_response":
            self._resolve(msg.get("response") if isinstance(msg.get("response"), dict) else {})
        elif kind == "control_request":
            self._on_request(msg)
        elif kind == "control_cancel_request":
            self._on_cancel(msg.get("request_id"))
        else:
            outs = self._wire.line(msg)
            await self._deliver(outs, raw=msg)
            for out in outs:
                if out.cls is RateLimit:
                    self._guard.update(**{k: out.fields[k] for k in ("status", "window", "resets_at",
                                                                     "utilization", "overage")})
            if any(out.cls is TurnFinished for out in outs):
                await self._check_login()
            if msg.get("type") == "system" and msg.get("subtype") == "session_state_changed":
                self._session_state(msg.get("state"))

    def _session_state(self, state: object) -> None:
        if self.state in ("closing", "closed", "failed"):
            return
        if state == "requires_action":
            self.state = "waiting_host"
        elif state == "running":
            self.state = "running"
        elif state == "idle":
            self.state = "idle"

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
            done, _ = await asyncio.wait({self._stderr_task}, timeout=KILL_GRACE_S)   # the rest of its stderr
            if not done:
                await _cancel_and_wait(self._stderr_task, KILL_GRACE_S)
        tail = " | ".join(list(self._stderr)[-5:])[:500]
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeFailure("process_died", f"claude exited (code {code}) before "
                                                                    f"answering" + (f": {tail}" if tail else "")))
        self._pending.clear()
        await asyncio.to_thread(self._remove_folders)
        quiet = reason in ("closed", "interrupted")
        async with self._emit_lock:
            self._ending = True             # send() can no longer start a turn
            # The crash's Error comes first, naming the turn it ended, so a
            # consumer tells a crash's turn end from an ordinary failure by
            # the signal, never by the order of the events around it.
            turn = self._wire.current_turn
            outs: list[Out] = []
            if failure is not None:
                outs.append(Out(failure.cls, {**failure.fields, "turn_id": turn}))
            elif reason == "process_died":
                outs.append(Out(Error, {"code": "process_died", "fatal": True, "retryable": True, "turn_id": turn,
                                        "message": f"claude exited unexpectedly with code {code}"
                                                   + (f": {tail}" if tail else "")}))
            outs += self._wire.close_turn(status="interrupted" if quiet else "failed",
                                          error=None if quiet else f"claude exited ({code})")
            outs += self._wire.cancel_all_tools()
            outs += self._wire.end_live_runs("the session's process ended")
            outs.append(Out(SessionClosed, {"reason": reason, "exit_code": code}))
            self._tail.extend(self._stamp(out, None) for out in outs)
        self.state = "closed" if reason == "closed" else "failed"
        self._turn_idle.set()
        self._finished.set()

    def _write_run_files(self) -> command.RunFiles:
        """Blocking (a thread). Never once the folder was removed for good: a
        restart cancelled mid-write must not bring it back after close()."""
        with self._folder_lock:
            if self._folder_removed:
                raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")
            return command.write_run_files(self._folder, self._spec, self._credential_rules)

    def _remove_folders(self) -> None:
        """Blocking (a thread): the session's folder, for good."""
        with self._folder_lock:
            self._folder_removed = True
            shutil.rmtree(self._folder, ignore_errors=True)
        if not self._spec.persist:
            remove_empty_project_folder(self._claude_home, self._spec.cwd)

    # ── control requests we send ─────────────────────────────────────────

    async def _write(self, obj: dict[str, Any]) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeFailure("process_died", "claude is not running")
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            try:
                await handle.write(data)
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "claude's input is closed: the process has ended") from exc

    async def _control(self, request: dict[str, Any], *, timeout: float = CONTROL_TIMEOUT_S) -> dict[str, Any]:
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"type": "control_request", "request_id": request_id, "request": request})
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"claude didn't answer {request['subtype']} within {timeout:g} s",
                                 retryable=True) from exc
        finally:
            self._pending.pop(request_id, None)
        if response.get("subtype") == "error":
            raise RuntimeFailure("protocol", f"claude refused {request['subtype']}: {response.get('error')}")
        body = response.get("response")
        return body if isinstance(body, dict) else {}

    def _resolve(self, response: dict[str, Any]) -> None:
        future = self._pending.get(response.get("request_id"))
        if future is not None and not future.done():
            future.set_result(response)

    # ── control requests claude sends ────────────────────────────────────

    def _on_request(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        request = msg.get("request") if isinstance(msg.get("request"), dict) else {}
        if not isinstance(request_id, str) or request_id in self._handlers:
            return
        subtype = request.get("subtype")
        fallback: dict[str, Any] | None = None
        if subtype == "can_use_tool":
            work: Callable[[], Awaitable[dict[str, Any]]] = lambda: self._permission(request_id, request)
            fallback = {"behavior": "deny", "message": HOST_ERROR, "interrupt": False}
        elif subtype == "hook_callback":
            work = self._no_opinion           # none are registered (no hooks, owner 2026-10-14)
        else:
            work = lambda: self._unsupported(subtype)
        task = asyncio.create_task(self._answer(request_id, work, fallback), name=f"claude-request:{request_id}")
        self._handlers[request_id] = task
        task.add_done_callback(lambda _t: self._handlers.pop(request_id, None))

    async def _no_opinion(self) -> dict[str, Any]:
        return {}

    async def _unsupported(self, subtype: object) -> dict[str, Any]:
        await self._deliver(self._wire.unknown(f"control_request/{subtype}"))
        raise RuntimeFailure("unsupported", f"this host doesn't serve {subtype}")

    async def _answer(self, request_id: str, work: Callable[[], Awaitable[dict[str, Any]]],
                      fallback: dict[str, Any] | None) -> None:
        """Answer one request of claude's. A permission whose handling fails
        for any reason is denied, never left unanswered (fail closed)."""
        try:
            body = await work()
            reply = {"subtype": "success", "request_id": request_id, "response": body}
        except asyncio.CancelledError:
            raise
        except RuntimeFailure as exc:
            reply = {"subtype": "error", "request_id": request_id, "error": exc.message}
        except Exception as exc:  # noqa: BLE001 - a bug here must still answer, and a permission is denied
            log.exception("claude session %s: answering %s failed", self.id, request_id)
            reply = {"subtype": "success", "request_id": request_id, "response": fallback} if fallback \
                else {"subtype": "error", "request_id": request_id, "error": str(exc)}
        with contextlib.suppress(RuntimeFailure):
            await self._write({"type": "control_response", "response": reply})

    def _on_cancel(self, request_id: object) -> None:
        task = self._handlers.get(request_id) if isinstance(request_id, str) else None
        if task is not None:
            self._withdrawn.add(request_id)
            task.cancel()

    async def _permission(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        tool = request.get("tool_name") if isinstance(request.get("tool_name"), str) else ""
        args = request.get("input") if isinstance(request.get("input"), dict) else {}
        agent = request.get("agent_id")
        run = agent if isinstance(agent, str) and agent else None
        if tool == "AskUserQuestion":
            return await self._question(request_id, run, args)
        call_id = request.get("tool_use_id") if isinstance(request.get("tool_use_id"), str) else None
        call = describe_call(tool, args, cwd=self._spec.cwd)
        suggestions = request.get("permission_suggestions")
        rule_suggestions = tuple(json.dumps(s, sort_keys=True) for s in suggestions) \
            if isinstance(suggestions, list) else ()
        reason = request.get("decision_reason") or request.get("description")
        req = PermissionRequest(
            request_id=request_id, run=run, call_id=call_id, tool=tool, kind=call.kind, summary=call.summary,
            input=args, cwd=self._spec.cwd, paths=call.paths, command=call.command, url=call.url, mcp=call.mcp,
            plan=call.plan,
            content_sha256=content_sha256(tool, args), run_title=self._wire.run_title(run),
            reason=reason if isinstance(reason, str) else None,
            scopes=("once", "session", "rule") if rule_suggestions else ("once", "session"),
            rule_suggestions=rule_suggestions)
        await self._deliver([Out(PermissionRequested, {"request": req, "run": run})])

        def resolved(answer: object) -> Out:
            decision = answer if isinstance(answer, PermissionDecision) \
                else PermissionDecision(allow=False, message=WITHDRAWN)
            return Out(PermissionResolved, {"request_id": request_id, "decision": decision, "run": run})

        # Scope "session" is kept here, never written to claude's settings (ch 02 §3.2).
        key = json.dumps([tool, args], sort_keys=True, default=str)
        if key in self._session_allowed:
            decision = PermissionDecision(allow=True, scope="session")
            await self._deliver([resolved(decision)])
        else:
            decision = await self._ask_host(request_id, lambda: self._host.decide_permission(req),
                                            resolved=resolved, valid=lambda a: isinstance(a, PermissionDecision),
                                            fallback=PermissionDecision(allow=False, message=HOST_ERROR))
            if decision.allow and decision.scope == "session":
                self._session_allowed.add(key)
        if decision.allow:
            return {"behavior": "allow", "updatedInput": dict(decision.updated_input or args)}
        if call_id:
            self._wire.denied.add(call_id)
        return {"behavior": "deny", "message": decision.message or DEFAULT_DENY, "interrupt": decision.stop_turn}

    async def _ask_host(self, request_id: str, call: Callable[[], Awaitable[Any]], *,
                        resolved: Callable[[object], Out], valid: Callable[[Any], bool], fallback: Any) -> Any:
        """Wait for the host, as long as it takes (V4: claude holds the call
        open). A host that raises or answers something malformed gets
        ``fallback``, a deny: fail closed. A request claude withdraws is
        recorded as resolved, then the wait is cancelled."""
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
            log.exception("claude session %s: the host failed to answer %s", self.id, request_id)
            answer = fallback
        else:
            if not valid(answer):
                log.error("claude session %s: the host answered %s with a %s", self.id, request_id,
                          type(answer).__name__)
                answer = fallback
        finally:
            if self.state == "waiting_host" and not self._handlers.keys() - {request_id}:
                self.state = "running" if self._wire.current_turn else "idle"
        await self._deliver([resolved(answer)])
        return answer

    async def _question(self, request_id: str, run: str | None, args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("questions") if isinstance(args.get("questions"), list) else []
        questions = tuple({
            "id": str(q.get("question") or n), "header": q.get("header"), "question": q.get("question"),
            "options": [{"label": o.get("label"), "description": o.get("description")}
                        for o in q.get("options") or [] if isinstance(o, dict)],
            "multi": bool(q.get("multiSelect")), "secret": False,
        } for n, q in enumerate(raw) if isinstance(q, dict))
        question = Question(request_id=request_id, run=run, questions=questions, run_title=self._wire.run_title(run))
        await self._deliver([Out(QuestionAsked, {"question": question, "run": run})])
        failed = object()
        answer = await self._ask_host(
            request_id, lambda: self._host.answer_question(question),
            resolved=lambda a: Out(QuestionAnswered, {"request_id": request_id, "run": run,
                                                      "answer": dict(a) if isinstance(a, Mapping) else {}}),
            valid=lambda a: isinstance(a, Mapping), fallback=failed)
        if not isinstance(answer, Mapping):
            return {"behavior": "deny", "message": HOST_ERROR, "interrupt": False}
        answers = {key: ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
                   for key, value in dict(answer).items()}
        return {"behavior": "allow", "updatedInput": {**args, "answers": answers}}

    # ── the operations core calls ────────────────────────────────────────

    def _require_live(self) -> None:
        if self.state in ("closing", "closed", "failed") or self._finished.is_set():
            raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")

    async def send(self, msg: UserInput) -> str:
        """Start a turn, or add to the running one: claude steers the running
        turn with a message sent mid-turn (V2), so it returns that turn's id."""
        self._require_live()
        self._guard.check()
        content = await asyncio.to_thread(_content_blocks, msg)
        async with self._attach_lock:
            await self._settle()
            await self._check_login()
            async with self._emit_lock:
                if self._ending or self._handle is None:
                    raise RuntimeFailure("process_died", f"session {self.id} has ended; open it again to resume")
                if self._wire.current_turn is None:
                    turn_id, outs = self._wire.start_turn(msg.origin)
                    self._turn_idle.clear()
                    for out in outs:
                        await self._queue.put(self._stamp(out, None))
                else:
                    turn_id = self._wire.current_turn
            await self._write({"type": "user", "message": {"role": "user", "content": content},
                               "parent_tool_use_id": None})
        if self.state == "idle":
            self.state = "running"
        return turn_id

    async def _settle(self) -> None:
        """With ``_attach_lock`` held: wait for a restart in flight, or run
        the pending effort restart if no turn is running."""
        task = self._restart_task
        if task is None and self._pending_effort is not None and self._wire.current_turn is None:
            task = self._restart_task = asyncio.create_task(self._restart(self._pending_effort),
                                                            name=f"claude-restart:{self.id}")
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():            # close() stopped it
                raise RuntimeFailure("process_died", f"session {self.id} was closed while it restarted") from None
            raise
        finally:
            if task.done() and self._restart_task is task:
                self._restart_task = None

    async def interrupt(self, grace_s: float = 10) -> None:
        """claude's own interrupt (V1): the turn ends, the session stays. It
        also stops every background sub-agent (VF2). If the turn hasn't ended
        after ``grace_s``, the process group is terminated."""
        if self._handle is None or self._finished.is_set() or self.state == "closing":
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_s
        with contextlib.suppress(RuntimeFailure):
            await self._control({"subtype": "interrupt"}, timeout=max(grace_s, 0.01))
        try:
            await asyncio.wait_for(self._turn_idle.wait(), max(deadline - loop.time(), 0))
            return
        except asyncio.TimeoutError:
            pass
        await self._deliver([Out(Notice, {"code": "interrupt_escalated",
                                          "message": f"claude did not stop within {grace_s:g} s; "
                                                     "its process group was terminated"})])
        self._end_reason = self._end_reason or "interrupted"
        handle = self._handle
        if handle is not None:
            await handle.terminate(KILL_GRACE_S)
            await self._await_end(handle)

    async def update(self, *, model: str | None = None, effort: Effort | None = None,
                     permission_mode: PermissionMode | None = None) -> SupportReport:
        self._require_live()
        items: list[SupportItem] = []
        if model is not None:
            items.append(await self._live_change("update.model", "set_model", {"model": model}))
        if permission_mode is not None:
            if permission_mode == "bypass" and self._spec.permissions.mode != "bypass":
                items.append(SupportItem(feature="update.permission_mode", support="refused",
                                         mechanism="set_permission_mode", enforced_by="none",
                                         note="bypass only for a session whose spec asked for it"))
            elif self._spec.orchestration == "coordinator" and permission_mode in command.COORDINATOR_REFUSED:
                items.append(SupportItem(feature="update.permission_mode", support="refused",
                                         mechanism="set_permission_mode", enforced_by="none",
                                         note=f"{permission_mode} would let a coordinator's workers act unasked"))
            else:
                mode = command.claude_mode(permission_mode, coordinator=self._spec.orchestration == "coordinator")
                items.append(await self._live_change("update.permission_mode", "set_permission_mode",
                                                     {"mode": mode}))
        if effort is not None:
            items.append(await self._change_effort(effort))
        return SupportReport(items=tuple(items))

    async def _change_effort(self, effort: Effort) -> SupportItem:
        if not self._spec.persist:
            return SupportItem(feature="update.effort", support="refused", mechanism="restart + --resume",
                               enforced_by="none", note="a session without a transcript can't be resumed")
        self._pending_effort = effort
        if self._wire.current_turn is None:
            try:
                async with self._attach_lock:
                    await self._settle()
            except RuntimeFailure as exc:
                return SupportItem(feature="update.effort", support="refused", mechanism="restart + --resume",
                                   enforced_by="none", note=f"the restart failed and the session ended: {exc.message}")
        return SupportItem(feature="update.effort", support="emulated", mechanism="restart + --resume",
                           enforced_by="host", note="claude takes effort only at launch: the session restarts "
                                                    "with it between turns")

    async def _live_change(self, feature: str, subtype: str, fields: dict[str, Any]) -> SupportItem:
        try:
            await self._control({"subtype": subtype, **fields})
        except RuntimeFailure as exc:
            return SupportItem(feature=feature, support="refused", mechanism=subtype, enforced_by="none",
                               note=exc.message)
        if subtype == "set_model":
            self._spec = replace(self._spec, model=fields["model"])
        return SupportItem(feature=feature, support="native", mechanism=subtype, enforced_by="cli")

    async def _restart(self, effort: Effort) -> None:
        """Close this process and resume the same native session with the new
        effort, between turns. A failed restart ends the session."""
        old, reader = self._handle, self._reader
        self._pending_effort = None
        self._spec = replace(self._spec, effort=effort, fork=False, fork_at=None)
        self._retiring, self._retiring_reader, self._handle = old, reader, None
        for handler in list(self._handlers.values()):
            handler.cancel()                # a background run's held check: its process is going
        if old is not None:
            await old.terminate(KILL_GRACE_S)
        if reader is not None:
            # asyncio.wait, not a suppressed await: a close() that cancels this
            # restart must stop it here, before it attaches a new process.
            await asyncio.wait({reader})
        self._retiring = self._retiring_reader = None
        # Background runs and their calls died with the old process.
        await self._deliver([*self._wire.cancel_all_tools(), *self._wire.end_live_runs("claude was restarted")])
        try:
            notices = await self._attach(self.native_id)
        except RuntimeFailure as exc:
            self._end_reason = "process_died"
            self._failure = Out(Error, {"code": exc.code, "message": f"restarting claude failed: {exc.message}",
                                        "fatal": True})
            if self._handle is not None and self._reader is not None:
                await self._handle.terminate(KILL_GRACE_S)
                await asyncio.shield(self._reader)      # the reader reports the end
            else:
                await self._ended(None, None)
            raise
        await self._deliver([Out(Notice, {"code": "restarted", "message": f"claude restarted with effort "
                                                                          f"{effort} (resumed {self.native_id})"}),
                             *notices])

    async def stop_run(self, run_id: str) -> None:
        """``stop_task``; claude answers success even for an unknown id, so the
        stop is confirmed only by the ``RunFinished`` that follows (V14a)."""
        self._require_live()
        await self._control({"subtype": "stop_task", "task_id": run_id})

    async def send_to_run(self, run_id: str, msg: UserInput) -> None:
        raise RuntimeFailure("unsupported", "messaging a sub-agent is not available for claude_code "
                                            "(send_task_message is a stub in this CLI)")

    async def close(self, grace_s: float = 5) -> None:
        """Terminate the process group and wait until everything is released.
        Idempotent; safe while a permission is held (the wait is cancelled)."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(grace_s), name=f"claude-close:{self.id}")
        await asyncio.shield(self._closing)

    async def _close(self, grace_s: float) -> None:
        if self._finished.is_set():
            return
        self._end_reason = self._end_reason or "closed"
        self.state = "closing"
        for handler in list(self._handlers.values()):
            handler.cancel()
        if self._restart_task is not None:
            # A restart in flight never attaches a new process now; one held up
            # (its old reader blocked on a consumer that stopped reading) is
            # given up on after READER_DRAIN_S, like any reader.
            await _cancel_and_wait(self._restart_task, READER_DRAIN_S)
        if self._retiring_reader is not None:
            await _cancel_and_wait(self._retiring_reader, READER_DRAIN_S)
        for handle in (self._retiring, self._handle):
            if handle is not None:
                await handle.terminate(grace_s)
        if self._handle is not None:
            await self._await_end(self._handle)
        if not self._finished.is_set():     # stopped between two processes: no reader reports the end
            await self._ended(None, None)

    async def _await_end(self, handle: ProcessHandle) -> None:
        """Let the reader report the end of a terminated process. A reader
        held up by a consumer that stopped taking events is cancelled, and
        the end is reported here instead (into the final events, which never
        wait for the consumer)."""
        reader = self._reader
        if reader is None or self._finished.is_set():
            return
        done, _ = await asyncio.wait({reader}, timeout=READER_DRAIN_S)
        if done:
            return
        await _cancel_and_wait(reader, READER_DRAIN_S)
        if not self._finished.is_set():
            await self._ended(await handle.wait(), None)

    async def _check_login(self) -> None:
        """At a turn boundary: has the person connected or disconnected a
        claude login since this process started? Reported once per change as
        a ``Notice`` with code ``credentials_changed``; the engine recycles the
        session at its next boundary (§0.79)."""
        if self._fingerprint is None:
            return
        now = await asyncio.to_thread(login_fingerprint, self._claude_home)
        if now != self._fingerprint:
            self._fingerprint = now
            await self._deliver([Out(Notice, {"code": "credentials_changed",
                                              "message": "the claude login changed since this process started; "
                                                         "recycle the session to use it"})])


async def _cancel_and_wait(task: asyncio.Task, timeout: float) -> None:
    """Cancel a task this session owns and wait at most ``timeout`` for it to
    end. Never swallows the caller's own cancellation (unlike a suppressed
    ``await task``): ``asyncio.wait`` leaves the task's outcome in the task."""
    if not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=timeout)


def _content_blocks(msg: UserInput) -> list[dict[str, Any]]:
    """The user message's content: XO's hidden context first, as its own
    marked block, then the person's text, then images. Blocking (reads the
    images): run it in a thread."""
    blocks: list[dict[str, Any]] = []
    if msg.context:
        blocks.append({"type": "text", "text": f"{CONTEXT_HEADER}\n{msg.context}"})
    blocks.append({"type": "text", "text": msg.text})
    for path in msg.images:
        media_type = mimetypes.guess_type(path)[0]
        if media_type not in IMAGE_TYPES:
            raise RuntimeFailure("unsupported", f"claude takes PNG, JPEG, GIF or WebP images, not {path}")
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise RuntimeFailure("spec_refused", f"the image {path} can't be read: {exc.strerror}") from exc
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                   "data": base64.b64encode(data).decode("ascii")}})
    return blocks
