"""One agy session over the Agent Client Protocol: ``agy acp``, one
long-lived process per session, JSON-RPC over its stdio (agy 1.5.15+).

The same session as ``AgySession`` in everything XO decides: XO's plugin
hook still answers every tool call of the session and of its sub-agents
(``on_hook``), adds the role's prompt and the turn's hidden context, and the
MCP relay still carries XO's servers. ACP is the transport, and it gives
XO what stream-json can't:

* turns: each message is one ``session/prompt`` request, and its answer is
  the turn's end (``stopReason``, the turn's usage with its sub-agents' and
  background work's), so no turn is matched to a send by order;
* tool calls by agy's own id (the id XO's hook sees), with their diffs and
  output; the model's thinking as it streams; agy's plan;
* interrupt is ``session/cancel``; model, effort (agy's ``thought_level``)
  and mode change with ``session/set_*``: the process and the record carry
  on, nothing restarts;
* agy's own permission asks arrive as ``session/request_permission`` and are
  decided by XO like any other ask.

Auto: XO's ``auto`` runs agy in its own Auto mode, and XO's hook, once its
own checks have passed (protected paths, ``~/.gemini``, a sub-agent's comms,
a coordinator's read-only policy), leaves the call to agy (``"ask"``): agy's
Auto approves what it judges safe and asks XO about the rest, which XO's
policy decides. agy's Auto runs commands in its terminal sandbox; where that
sandbox can't start (a container that doesn't allow it: agy logs ``sbox:
mount / private: permission denied``), every command would fail once and
then ask, so from the first such failure on the runtime runs agy in its
default mode and XO's trusted auto decides, as on stream-json (said once,
in a Notice).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections import deque
from dataclasses import replace
from functools import partial
from typing import Any, Mapping

from services.cowork_agent.adapters.antigravity.runtime import argv as a
from services.cowork_agent.adapters.antigravity.runtime.account import is_quota
from services.cowork_agent.adapters.antigravity.runtime.acp_wire import AcpWire
from services.cowork_agent.adapters.antigravity.runtime.env import session_env
from services.cowork_agent.adapters.antigravity.runtime.install import agy_server_name
from services.cowork_agent.adapters.antigravity.runtime.session import (
    CONTEXTS_KEPT,
    KILL_GRACE_S,
    NOT_STARTED,
    START_TIMEOUT_S,
    AgySession,
)
from services.cowork_agent.adapters.antigravity.runtime.tools import describe
from services.cowork_agent.adapters.antigravity.runtime.wire import Out
from services.cowork_agent.adapters.contract import (
    Effort,
    Notice,
    PermissionMode,
    ProcessHandle,
    RateLimit,
    RunStarted,
    RuntimeFailure,
    SupportItem,
    SupportReport,
    TurnFinished,
    UserInput,
)

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "xo_space", "title": "XO Space", "version": "1"}     # as codex's; never another client's name
#: XO's efforts as agy's ``thought_level`` (low, medium, high).
THOUGHT_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
#: XO's modes as agy's: plan is agy's plan mode, auto its Auto (where its
#: sandbox runs), everything else its default mode with XO's hook deciding.
AGY_PLAN, AGY_AUTO, AGY_DEFAULT = "plan", "auto", "default"
#: JSON-RPC: the method isn't served; agy's "not found" for an unknown session.
METHOD_NOT_FOUND = -32601
NOT_FOUND_CODES = (-32002, -32004)
DEFER = {"decision": "ask"}
SANDBOX_NOTE = ("agy's terminal sandbox can't start on this machine, so agy's own Auto mode would ask before "
                "every command. XO's auto decides instead, as before: commands and edits run, XO's own checks "
                "still apply.")


class AcpError(Exception):
    """A JSON-RPC error agy answered with."""

    def __init__(self, code: object, message: str) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, int) else None
        self.message = message


class SandboxState:
    """Whether agy's terminal sandbox can start on this machine: unknown
    (assumed yes) until a command says it can't. One per runtime, so every
    session on the server learns it once."""

    def __init__(self) -> None:
        self.broken = False


class AcpSession(AgySession):
    """``contract.Session`` on ``agy acp``."""

    def __init__(self, *, sandbox: SandboxState | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        spec = self._spec
        self._wire = AcpWire(cwd=spec.cwd, runs_detail=spec.runs_detail, thinking=spec.thinking != "off",
                             servers=tuple(agy_server_name(m.name) for m in spec.mcp_servers))
        self._sandbox = sandbox or SandboxState()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._requests: set[asyncio.Task] = set()       # agy's own requests being answered
        self._prompts: deque[tuple[str, str]] = deque()  # (turn id, text) not yet sent to agy
        self._prompter: asyncio.Task | None = None
        self._loading = False                            # session/load is replaying the conversation
        self._settings_lock = asyncio.Lock()
        self._agy: dict[str, str | None] = {"mode": None, "model": None, "effort": None}
        self._levels: tuple[str, ...] | None = None      # the thought levels agy offers for its model
        self._said: set[str] = set()

    # ── start ────────────────────────────────────────────────────────────

    async def _attach(self) -> None:
        await super()._attach()
        self._thinking_tail = None                       # ACP streams the thinking itself

    async def _launch(self, resume: str | None) -> None:
        """``agy acp`` on the conversation (``resume``: ``session/load``,
        else ``session/new``), with the session's model, effort and mode."""
        spec = self._spec
        if self._prepare is not None:
            await self._prepare()
        env = await asyncio.to_thread(session_env, spec_env=spec.env, socket=self._server.path if self._server
                                      else None, label=f"agy session {self.id}")
        self._planned = None
        self._sent_in_process = 0
        self._agy = {"mode": None, "model": None, "effort": None}
        try:
            handle = await self._host.launcher.spawn(a.acp_argv(self._binary, spec), cwd=spec.cwd, env=env,
                                                     label=f"storm agy {self.id}")
        except OSError as exc:
            if not await asyncio.to_thread(os.path.isdir, spec.cwd):
                raise self._missing_cwd() from None
            raise RuntimeFailure("cli_missing", f"the agy CLI can't be started ({self._binary}: "
                                                f"{exc.strerror or exc}); install it, or point AGY_CLI_PATH at an "
                                                "executable agy") from exc
        self._handle = handle
        self._process_done = asyncio.Event()
        self._stderr.clear()
        self._relays_up.clear()
        self._reader = asyncio.create_task(self._read(handle), name=f"agy-reader:{self.id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(handle), name=f"agy-stderr:{self.id}")
        try:
            self._check_open()
            init = await self._start_call("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": CLIENT_INFO})
            if not isinstance(init, Mapping) or init.get("protocolVersion") != PROTOCOL_VERSION:
                raise RuntimeFailure("cli_too_old", "agy doesn't speak ACP version 1; update agy (`agy update`)")
            if resume:
                self._loading = True
                try:
                    opened = await self._start_call("session/load", {"sessionId": resume, "cwd": spec.cwd,
                                                                     "mcpServers": []})
                except AcpError as exc:
                    if exc.code in NOT_FOUND_CODES or "not found" in exc.message.lower():
                        raise RuntimeFailure("session_not_found", f"agy has no conversation {resume}; XO can "
                                                                  "replace it only if no input was ever submitted"
                                             ) from exc
                    raise
                finally:
                    self._loading = False
                conversation = resume
            else:
                opened = await self._start_call("session/new", {"cwd": spec.cwd, "mcpServers": []})
                conversation = opened.get("sessionId") if isinstance(opened, Mapping) else None
                if not isinstance(conversation, str) or not conversation:
                    raise RuntimeFailure("protocol", "agy started without a session id" + self._stderr_tail())
            self._check_open()
            self.native_id = conversation
            self._wire.conversation = conversation
            self._read_state(opened if isinstance(opened, Mapping) else {})
            if not self.model and self._agy["model"]:
                self.model = self._agy["model"]
            await self._sync_settings()
        except AcpError as exc:
            await self._abandon(handle)
            raise RuntimeFailure("protocol", f"agy refused to start the session: {exc.message}"
                                 + self._stderr_tail()) from exc
        except BaseException:
            await self._abandon(handle)
            raise

    def _check_open(self) -> None:
        if self._closing is not None or self._finished.is_set():
            raise RuntimeFailure("process_died", f"session {self.id} was closed while agy started")

    async def _start_call(self, method: str, params: Mapping[str, Any]) -> Any:
        try:
            return await self._call(method, params, timeout=START_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"agy didn't answer {method} within {START_TIMEOUT_S:g} s"
                                 + self._stderr_tail(), retryable=True) from exc

    async def _abandon(self, handle: ProcessHandle) -> None:
        self._planned = "start_failed"
        await handle.terminate(KILL_GRACE_S)
        await self._await_reader()

    def _read_state(self, opened: Mapping[str, Any]) -> None:
        """agy's mode, model and thought levels, from ``session/new`` or
        ``session/load``."""
        modes = opened.get("modes") if isinstance(opened.get("modes"), Mapping) else {}
        if isinstance(modes.get("currentModeId"), str):
            self._agy["mode"] = modes["currentModeId"]
        models = opened.get("models") if isinstance(opened.get("models"), Mapping) else {}
        if isinstance(models.get("currentModelId"), str):
            self._agy["model"] = models["currentModelId"]
        self._read_options(opened.get("configOptions"))

    def _read_options(self, options: object) -> None:
        for option in options if isinstance(options, list) else ():
            if isinstance(option, Mapping) and option.get("id") == "thought_level":
                values = option.get("options") if isinstance(option.get("options"), list) else []
                self._levels = tuple(str(v.get("value")) for v in values if isinstance(v, Mapping))
                return
        if isinstance(options, list):
            self._levels = ()                            # this model takes no thought level

    # ── JSON-RPC ─────────────────────────────────────────────────────────

    async def _send_line(self, message: Mapping[str, Any]) -> None:
        await self._write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())

    async def _call(self, method: str, params: Mapping[str, Any], *, timeout: float | None = None) -> Any:
        rid = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send_line({"id": rid, "method": method, "params": dict(params)})
            if timeout is None:
                return await future
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            self._pending.pop(rid, None)

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send_line({"method": method, "params": dict(params)})

    async def _respond(self, rid: object, *, result: Any = None, error: Mapping[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"id": rid}
        if error is not None:
            body["error"] = dict(error)
        else:
            body["result"] = result
        with contextlib.suppress(RuntimeFailure):
            await self._send_line(body)

    async def _dispatch(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            await self._deliver(self._wire.unknown("a line that is not JSON"))
            return
        if not isinstance(msg, dict):
            await self._deliver(self._wire.unknown("a line that is not a JSON object"))
            return
        method = msg.get("method")
        if method is None:
            future = self._pending.get(msg.get("id")) if isinstance(msg.get("id"), int) else None
            if future is None or future.done():
                return
            error = msg.get("error")
            if isinstance(error, Mapping):
                future.set_exception(AcpError(error.get("code"), str(error.get("message") or "error")))
            else:
                future.set_result(msg.get("result"))
            return
        if "id" in msg:
            self._track(asyncio.ensure_future(self._agent_request(msg)))
            return
        if method != "session/update":
            await self._deliver(self._wire.unknown(f"notification {method!r}"))
            return
        params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
        update = params.get("update") if isinstance(params.get("update"), Mapping) else {}
        self._note_state(update)
        if self._loading or (self.native_id and params.get("sessionId") not in (None, self.native_id)):
            return                                       # a load's replay: history XO has already
        await self._deliver(self._wire.update(update), raw=msg)
        if self._wire.sandbox_failed and not self._sandbox.broken:
            self._sandbox.broken = True
            self._track(asyncio.ensure_future(self._leave_agy_auto()))

    def _note_state(self, update: Mapping[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update" and isinstance(update.get("currentModeId"), str):
            self._agy["mode"] = update["currentModeId"]
        elif kind == "config_option_update":
            self._read_options(update.get("configOptions"))

    def _track(self, task: asyncio.Task) -> None:
        self._requests.add(task)
        task.add_done_callback(self._requests.discard)

    async def _leave_agy_auto(self) -> None:
        await self._deliver([Out(Notice, {"code": "agy_sandbox_unavailable", "message": SANDBOX_NOTE})])
        with contextlib.suppress(AcpError, RuntimeFailure, asyncio.TimeoutError):
            await self._sync_settings()

    # ── agy's requests ───────────────────────────────────────────────────

    async def _agent_request(self, msg: Mapping[str, Any]) -> None:
        rid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
        if method != "session/request_permission":
            await self._respond(rid, error={"code": METHOD_NOT_FOUND, "message": f"XO doesn't serve {method}"})
            return
        try:
            outcome = await self._permission(rid, params)
        except asyncio.CancelledError:
            await asyncio.shield(self._respond(rid, result={"outcome": {"outcome": "cancelled"}}))
            raise
        except Exception:  # noqa: BLE001 - a bug here refuses the action, never allows it
            log.exception("agy session %s: answering agy's permission request failed", self.id)
            outcome = _choose(params.get("options"), allow=False)
        await self._respond(rid, result={"outcome": outcome})

    async def _permission(self, rid: object, params: Mapping[str, Any]) -> dict[str, Any]:
        """agy asks before an action (its Auto mode wouldn't decide it): XO
        decides it like any other ask, recorded the same way."""
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
        call_id = tool_call.get("toolCallId") if isinstance(tool_call.get("toolCallId"), str) else f"acp-{rid}"
        name = str(tool_call.get("name") or "tool")
        args = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), Mapping) else \
            tool_call.get("args") if isinstance(tool_call.get("args"), Mapping) else {}
        call = await asyncio.to_thread(partial(describe, servers=self._wire.servers), name, args, self._spec.cwd)
        answer = await self._ask(call_id, None, call, args)
        return _choose(params.get("options"), allow=answer.get("decision") in ("auto_approve", "allow"))

    # ── XO's hook ────────────────────────────────────────────────────────

    def _hook_call_id(self, payload: Mapping[str, Any], call_obj: Mapping[str, Any], conv: str) -> str:
        """agy's own tool call id, as ACP names the call; a sub-agent's calls
        keep the transcript's ``<conversation>:<step>``."""
        if self._root(payload) and isinstance(call_obj.get("id"), str) and call_obj["id"]:
            return call_obj["id"]
        return super()._hook_call_id(payload, call_obj, conv)

    def _agy_auto(self) -> bool:
        return self._mode == "auto" and not self._coordinator and not self._sandbox.broken

    def _trusted_answer(self) -> dict[str, Any]:
        """XO's checks passed: in agy's Auto, agy decides the rest."""
        return dict(DEFER) if self._agy_auto() else super()._trusted_answer()

    async def on_hook(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        conv = payload.get("conversationId")
        if isinstance(conv, str) and conv and not self._root(payload):
            await self._ensure_run(conv)
        if event == "Stop":
            # the prompt's answer ends a prompted turn; a turn agy started on its own ends with its loop
            if self._root(payload) and payload.get("fullyIdle") is True and self._prompter is None \
                    and self._wire.current is not None:
                await self._turn_done(self._wire.end_injected())
            return {}
        return await super().on_hook(event, payload)

    async def _ensure_run(self, conv: str) -> None:
        outs = self._wire.subagent_started(conv)
        if not outs:
            return
        await self._deliver(outs)
        for out in outs:
            if out.cls is RunStarted and out.fields["run"] not in self._followers:
                run = out.fields["run"]
                self._followers[run] = asyncio.create_task(self._follow(run), name=f"agy-run:{self.id}:{run}")

    # ── turns ────────────────────────────────────────────────────────────

    async def send(self, msg: UserInput) -> str:
        """Queue one message: agy runs it as soon as the turn before it
        ends (one ``session/prompt`` at a time)."""
        self._require_live()
        self._guard.check(self.model)
        async with self._send_lock:
            await self._check_login()
            await self._withdraw_plan(end_input=False)
            self._restart = False                        # settings change over ACP: nothing restarts
            if self._handle is not None and self._planned is not None:
                await self._process_done.wait()          # a stop is under way: the next process takes this
            if self._handle is None:
                await self._process_done.wait()
                await self._launch(self.native_id)
            text = msg.text
            if msg.images:
                text += "".join(f"\n\n[An image is attached at {path}. Read it with your file tool.]"
                                for path in msg.images)
            notes, self._notes = self._notes, []
            context = "\n\n".join([*notes, *([msg.context] if msg.context else [])]) or None
            if context:
                if len(self._contexts) > CONTEXTS_KEPT:
                    self._contexts.clear()
                self._contexts.setdefault(text, deque()).append(context)
            turn_id = self._wire.expect_turn(msg.origin)
            self._input_open = True
            self._prompts.append((turn_id, text))
            if self._prompter is None or self._prompter.done():
                self._prompter = asyncio.create_task(self._prompt_loop(), name=f"agy-prompt:{self.id}")
            self._sent_in_process += 1
            if self._sent_in_process == 1:
                self._watch_comms()
            if self.state == "idle":
                self.state = "running"
            return turn_id

    async def _prompt_loop(self) -> None:
        try:
            while self._prompts and self._handle is not None and not self._ending:
                turn_id, text = self._prompts.popleft()
                try:
                    await self._sync_settings()
                except (AcpError, asyncio.TimeoutError) as exc:
                    await self._deliver([Out(Notice, {"code": "settings_not_applied", "message":
                                                      f"agy didn't take the session's settings: {exc}"})])
                if turn_id not in self._wire.pending_turns():
                    continue                            # an interrupt ended it meanwhile
                if self._wire.current is not None:      # agy's own turn, still open: it ends here
                    await self._deliver(self._wire.end_injected())
                await self._deliver(self._wire.begin_turn())
                result, error = None, None
                try:
                    result = await self._call("session/prompt", {"sessionId": self.native_id,
                                                                 "prompt": [{"type": "text", "text": text}]})
                except AcpError as exc:
                    error = exc.message
                    if is_quota({"code": exc.code, "message": exc.message}):
                        await self._deliver([Out(RateLimit, {"status": "limited", "window": None})])
                except RuntimeFailure:
                    return                              # the process ended: its end closes the turn
                await self._turn_done(self._wire.prompt_done(result if isinstance(result, Mapping) else None,
                                                             error))
        finally:
            if self._prompter is asyncio.current_task():
                self._prompter = None

    async def _turn_done(self, outs: list[Out]) -> None:
        """A turn's end: agy's answer means everything it started for the
        turn (sub-agents, background work) is done, unless more is queued."""
        if not self._prompts:
            outs = outs + [o for run in self._wire.live_runs() for o in self._wire.finish_run(run, "completed", None)]
        await self._deliver(outs)
        finished = next((o for o in reversed(outs) if o.cls is TurnFinished), None)
        if finished is None:
            return
        self._loop_idle = not self._prompts
        self._stop_seen = True
        await self._turn_ended(finished)

    async def interrupt(self, grace_s: float = 10) -> None:
        """``session/cancel``: agy ends the turn (its open tool calls closed,
        sub-agents and background work stopped) and stays up; messages not
        yet sent never start."""
        if self._finished.is_set() or self.state == "closing":
            return
        busy = self._wire.current is not None or self._prompts or self._wire.live_runs()
        if self._handle is None or not busy:
            await self._withdraw_plan(end_input=True)
            return
        self._prompts.clear()
        await self._deliver(self._wire.never_started(NOT_STARTED))
        for task in list(self._requests):
            task.cancel()
        with contextlib.suppress(RuntimeFailure):
            await self._notify("session/cancel", {"sessionId": self.native_id})
        prompter = self._prompter
        if prompter is not None and not prompter.done():
            done, _ = await asyncio.wait({prompter}, timeout=grace_s)
            if not done:
                await self._deliver([Out(Notice, {"code": "interrupt_escalated",
                                                  "message": f"agy did not stop within {grace_s:g} s; its process "
                                                             "group was terminated"})])
                await self._stop_process("interrupted", grace_s=0)
                return
        elif self._wire.current is not None:
            await self._deliver(self._wire.close_turn("interrupted", None))
        await self._deliver(self._wire.end_live_runs("stopped by an interrupt"))
        await self._idle_if_done(force=True)

    async def _stop_process(self, why: str, *, grace_s: float, sigint: bool = False) -> None:
        handle = self._handle
        if handle is None:
            return
        self._planned = why
        with contextlib.suppress(Exception):
            await handle.close_stdin()
        try:
            await asyncio.wait_for(self._process_done.wait(), grace_s or KILL_GRACE_S)
        except asyncio.TimeoutError:
            await handle.terminate(KILL_GRACE_S)
            await self._await_reader()

    async def _process_ended(self, handle: ProcessHandle, code: int | None, failure: Out | None) -> None:
        if handle is self._handle:
            gone = RuntimeFailure("process_died", f"agy exited (code {code})" + self._stderr_tail(), retryable=True)
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(gone)
            self._pending.clear()
            for task in list(self._requests):
                task.cancel()
            self._prompts.clear()
            self._agy = {"mode": None, "model": None, "effort": None}
        await super()._process_ended(handle, code, failure)

    async def _close(self, grace_s: float) -> None:
        for task in [*self._requests, *([self._prompter] if self._prompter else [])]:
            task.cancel()
        await super()._close(grace_s)

    # ── settings ─────────────────────────────────────────────────────────

    def _wanted_mode(self) -> str:
        if self._mode == "plan":
            return AGY_PLAN
        return AGY_AUTO if self._agy_auto() else AGY_DEFAULT

    async def _sync_settings(self) -> None:
        """agy's mode, model and thought level as the session's are now."""
        async with self._settings_lock:
            if self._handle is None or not self.native_id:
                return
            sid = self.native_id
            mode = self._wanted_mode()
            if mode != self._agy["mode"]:
                await self._call("session/set_mode", {"sessionId": sid, "modeId": mode}, timeout=START_TIMEOUT_S)
                self._agy["mode"] = mode
            if self.model and self.model != self._agy["model"]:
                await self._call("session/set_model", {"sessionId": sid, "modelId": self.model},
                                 timeout=START_TIMEOUT_S)
                self._agy["model"], self._agy["effort"] = self.model, None
            effort = self._effort
            if effort and effort != self._agy["effort"]:
                level = THOUGHT_LEVELS.get(effort)
                if level and (self._levels is None or level in self._levels):
                    await self._call("session/set_config_option", {"sessionId": sid, "configId": "thought_level",
                                                                    "value": level}, timeout=START_TIMEOUT_S)
                elif f"effort:{self.model}" not in self._said:
                    self._said.add(f"effort:{self.model}")
                    await self._deliver([Out(Notice, {"code": "effort_unsupported", "message":
                                                      f"agy offers no thinking level for {self.model}; the effort "
                                                      f"{effort} isn't applied"})])
                self._agy["effort"] = effort

    async def update(self, *, model: str | None = None, effort: Effort | None = None,
                     permission_mode: PermissionMode | None = None) -> SupportReport:
        """Model, effort and mode change over ACP, at once: agy's process and
        its conversation carry on."""
        self._require_live()
        items: list[SupportItem] = []
        if model is not None and model != self.model:
            self.model = model
            self._spec = replace(self._spec, model=model)
            items.append(_item("update.model", "session/set_model"))
        if effort is not None and effort != self._effort:
            self._effort = effort
            items.append(_item("update.effort", "session/set_config_option thought_level"))
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
                self._mode = permission_mode
                items.append(SupportItem(feature="update.permission_mode", support="native",
                                         mechanism="XO's hook and session/set_mode", enforced_by="hook"))
        if items and self._handle is not None:
            try:
                await self._sync_settings()
            except (AcpError, RuntimeFailure, asyncio.TimeoutError) as exc:
                await self._deliver([Out(Notice, {"code": "settings_deferred", "message":
                                                  f"agy didn't take the change yet ({exc}); it is tried again "
                                                  "before the next message"})])
        return SupportReport(items=tuple(items))


def _choose(options: object, *, allow: bool) -> dict[str, Any]:
    """The option agy offered for XO's answer: once, never always (a
    session-wide yes stays in XO, as on stream-json)."""
    kinds = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    offered = [o for o in options if isinstance(o, Mapping)] if isinstance(options, list) else []
    for kind in kinds:
        for option in offered:
            if option.get("kind") == kind and isinstance(option.get("optionId"), str):
                return {"outcome": "selected", "optionId": option["optionId"]}
    return {"outcome": "cancelled"}


def _item(feature: str, mechanism: str) -> SupportItem:
    return SupportItem(feature=feature, support="native", mechanism=mechanism, enforced_by="cli")


__all__ = ["AcpSession", "SandboxState", "AcpError"]
