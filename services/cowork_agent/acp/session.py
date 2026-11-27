from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .transport import AcpConnection
from services.cowork_agent.adapters import contract as c

#: Tool names many CLIs share, and the kind XO reviews each as. A CLI's own
#: names come from its runtime (``AcpRuntime.tool_kinds``) and win over these.
TOOL_KINDS = {"bash": "command", "shell": "command", "run_terminal_cmd": "command",
              "read": "read", "read_file": "read", "readfile": "read", "grep": "search",
              "glob": "list", "edit": "edit_file", "write": "create_file", "delete": "delete_file",
              "webfetch": "web_fetch", "websearch": "web_search"}
#: ACP's own tool-call kinds.
ACP_KINDS = {"execute": "command", "edit": "edit_file", "delete": "delete_file", "read": "read",
             "search": "search", "fetch": "web_fetch"}
#: Where a tool's input names the file or folder it works on.
PATH_KEYS = ("file_path", "path", "target_file", "target_directory")
#: The plain-language line for a tool call whose CLI gave none, by kind; ``{}`` is its target.
TITLES = {"command": "Run `{}`", "read": "Read `{}`", "edit_file": "Edit `{}`", "create_file": "Create `{}`",
          "delete_file": "Delete `{}`", "list": "List `{}`", "search": "Search for `{}`", "web_fetch": "Fetch {}",
          "web_search": "Search the web for `{}`"}
#: The longest target a title shows.
TITLE_TARGET_MAX = 120
#: What an open request is answered when the turn is stopped: ACP's "cancelled".
CANCELLED = {"outcome": {"outcome": "cancelled"}}


class _Plain:
    """The runtime hooks of a CLI that adds nothing to ACP (``AcpRuntime`` has the same)."""

    tool_kinds: dict = {}
    steer = None

    def requests(self):
        return {}

    def tool_name(self, call):
        return call.get("name")


def _target(kind, args):
    """What a tool call works on, as its card shows it: its command, path, URL or query."""
    if not isinstance(args, dict):
        return None
    if kind == "command":
        value = args.get("command")
    elif kind in ("web_fetch", "web_search"):
        value = args.get("url") or args.get("query")
    elif kind == "search":
        value = args.get("pattern") or args.get("query")
    else:
        value = next((args[key] for key in PATH_KEYS if isinstance(args.get(key), str)), None)
    if not isinstance(value, str) or not value.strip():
        return None
    line = " ".join(value.split())
    return line if len(line) <= TITLE_TARGET_MAX else line[:TITLE_TARGET_MAX - 1] + "…"


def usage_of(usage):
    """A turn's ``TokenUsage`` from an ACP prompt result's usage, or None.
    Both spellings CLIs use for cache writes and reasoning are read."""
    if not isinstance(usage, dict) or not ("inputTokens" in usage or "outputTokens" in usage):
        return None
    def count(*keys):
        value = next((usage[key] for key in keys if isinstance(usage.get(key), int)), 0)
        return max(0, value)
    cached = count("cachedReadTokens")
    written = count("cachedWriteTokens", "cacheCreationTokens")
    return c.TokenUsage(input=max(0, count("inputTokens") - cached - written), output=count("outputTokens"),
                        cache_read=cached, cache_write=written, reasoning=count("thoughtTokens", "reasoningTokens"))


def mcp_servers(spec: c.SessionSpec) -> list[dict]:
    servers = []
    for server in spec.mcp_servers:
        if server.transport == "stdio":
            servers.append(dict(name=server.name, command=server.command[0], args=list(server.command[1:]),
                                env=[dict(name=k, value=v) for k, v in server.env.items()]))
        else:
            headers = []
            if server.bearer_env:
                token = server.env.get(server.bearer_env)
                if not token:
                    raise c.RuntimeFailure("spec_refused", f"Missing credential for MCP server {server.name}")
                headers.append(dict(name="Authorization", value=f"Bearer {token}"))
            servers.append(dict(type="http", name=server.name, url=server.url, headers=headers))
    return servers


class AcpSession:
    def __init__(self, session_id, spec, host, report, process, *, guard=None, dialect=None):
        self.id = session_id
        self.spec = spec
        self.host = host
        self.report = report
        self._guard = guard
        #: The CLI's own additions to ACP: its tool names, extra requests and steering (its ``AcpRuntime``).
        self._dialect = dialect or _Plain()
        self._extra = dict(self._dialect.requests())
        self.state = "starting"
        self.native_id = ""
        self._seq = 0
        self._events = asyncio.Queue()
        self._task = None
        self._turn = None
        self._text = []
        self._denied = set()
        self._open_tools = set()
        self._thinking = False
        self._reviews = set()
        #: Each tool call of the turn as its ``tool_call`` updates gave it, for the permission requests
        #: that name it without its input.
        self._calls = {}
        self._closed = asyncio.Event()
        self._native_prompt = False
        self._rpc = AcpConnection(process, on_update=self._update, on_request=self._request)
        self._watcher = asyncio.create_task(self._watch(process))

    async def _watch(self, process):
        code = await process.wait()
        if self.state not in ("closing", "closed"):
            if self._task:
                await asyncio.gather(self._task, return_exceptions=True)
            if self.state not in ("closing", "closed"):
                self.state = "failed"
                if self._guard:
                    await self._guard.close()
                self._emit(c.SessionClosed, reason="ACP process exited", exit_code=code)

    def _emit(self, cls, **payload):
        self._seq += 1
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._events.put_nowait(cls(session=self.id, seq=self._seq, ts=now, native_id=self.native_id or None,
                                    turn_id=self._turn, **payload))

    async def events(self):
        while True:
            item = await self._events.get()
            yield item
            if isinstance(item, c.SessionClosed):
                break

    async def start(self, auth_method, mode, version, meta, *, identity):
        init = await self._rpc.call("initialize", dict(protocolVersion=1, clientCapabilities={},
                                                        clientInfo=dict(name="xo_space", version="1")))
        if init.get("protocolVersion") != 1:
            raise c.RuntimeFailure("protocol", "Unsupported ACP protocol version")
        if not any(method.get("id") == identity for method in init.get("authMethods", ())):
            raise c.RuntimeFailure("cli_too_old", "CLI did not advertise the expected ACP login method")
        params = dict(cwd=self.spec.cwd, mcpServers=mcp_servers(self.spec))
        if meta:
            params["_meta"] = meta
            self._native_prompt = "rules" in meta or "systemPromptOverride" in meta
        if self.spec.resume:
            if not init.get("agentCapabilities", {}).get("loadSession"):
                raise c.RuntimeFailure("unsupported", "ACP session loading unavailable")
            params["sessionId"] = self.spec.resume
        if auth_method:
            await self._rpc.call("authenticate", {"methodId": auth_method})
        result = await self._rpc.call("session/load" if self.spec.resume else "session/new", params, timeout=60)
        self.native_id = self.spec.resume or result.get("sessionId")
        if not isinstance(self.native_id, str) or not self.native_id:
            raise c.RuntimeFailure("protocol", "ACP returned no session ID")
        if mode:
            await self._rpc.call("session/set_mode", dict(sessionId=self.native_id, modeId=mode))
        self.state = "idle"
        current = (result.get("models") or {}).get("currentModelId") if isinstance(result, dict) else None
        self._emit(c.SessionStarted, model=self.spec.model or (current if isinstance(current, str) else None),
                   cwd=self.spec.cwd, cli_version=version)

    async def _request(self, method, params):
        if method != "session/request_permission":
            handler = self._extra.get(method)
            if handler is None:
                raise NotImplementedError(method)
            return await handler(self, params)
        if self.state not in ("running", "waiting_host") or params.get("sessionId") != self.native_id:
            return CANCELLED
        given = params.get("toolCall") or {}
        # A CLI may name the call and leave out its input (cursor-agent gives no rawInput): what its own
        # tool_call said fills what the request leaves out, so the review sees the real command or path.
        call = {**self._calls.get(given.get("toolCallId"), {}), **{k: v for k, v in given.items() if v}}
        req = self._permission(call.get("name") or call.get("title") or "tool", call.get("toolCallId"),
                               call.get("rawInput") or {}, self._tool_kind(call))
        decision = await self._decide(req)
        if decision is None:
            return CANCELLED
        kind = "allow_once" if decision.allow and not decision.updated_input and not decision.stop_turn else "reject_once"
        for option in params.get("options", []):
            if option.get("kind") == kind:
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return CANCELLED

    def _kind_of(self, tool, kind="other"):
        """The kind XO reviews a tool as: its CLI's own name for it first, then the common names, then
        what the CLI said (``kind``)."""
        name = tool.lower()
        return self._dialect.tool_kinds.get(name) or TOOL_KINDS.get(name) or kind

    def _permission(self, tool, key, args, kind="other"):
        kind = self._kind_of(tool, kind)
        mcp = None
        qualified = tool.removeprefix("mcp__")
        for server in self.spec.mcp_servers:
            if qualified.startswith(server.name + "__"):
                mcp = (server.name, qualified[len(server.name)+2:])
                kind = "mcp"
                break
        path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), None)
        if kind in ("edit_file", "create_file", "delete_file", "read") and not isinstance(path, str):
            kind = "other"
        return c.PermissionRequest(request_id=str(uuid4()), run=None, call_id=key, tool=tool, kind=kind,
                                   cwd=args.get("cwd") or args.get("working_directory") or self.spec.cwd,
                                   summary=tool, input=args, paths=(path,) if isinstance(path, str) else (),
                                   command=args.get("command") if kind == "command" else None,
                                   url=args.get("url") if kind == "web_fetch" else None, mcp=mcp)

    async def tool_hook(self, event):
        if self.state not in ("running", "waiting_host") or not isinstance(event, dict):
            return False
        args = event.get("tool_input") or event.get("toolInput") or {}
        name = event.get("tool_name") or event.get("toolName")
        key = event.get("tool_use_id") or event.get("toolUseId")
        if not isinstance(name, str) or not isinstance(args, dict):
            return False
        req = self._permission(name, key, args)
        if req.kind == "ask":
            return True              # its question tool: the question itself reaches the person (``ask``)
        decision = await self._decide(req)
        return decision is not None and decision.allow and not decision.updated_input and not decision.stop_turn

    async def _hold(self, ask):
        """The host's answer to ``ask`` (a coroutine), as long as it takes; None when the turn was stopped
        first, so the CLI is answered "cancelled" rather than left waiting (ACP ``session/cancel``)."""
        turn = self._turn
        held = asyncio.ensure_future(ask)
        self._reviews.add(held)
        self.state = "waiting_host"
        try:
            answer = await held
        except asyncio.CancelledError:
            if not held.cancelled() or asyncio.current_task().cancelling():
                raise
            return None                                   # the turn was stopped: its item is withdrawn
        finally:
            self._reviews.discard(held)
            if self.state == "waiting_host" and not self._reviews:
                self.state = "running"
        if turn != self._turn or self.state in ("closing", "closed", "failed"):
            return None
        return answer

    async def _decide(self, req):
        """The host's decision on ``req``; None when the turn ended first."""
        self._emit(c.PermissionRequested, request=req)
        decision = await self._hold(self.host.decide_permission(req))
        if decision is None:
            return None
        self._emit(c.PermissionResolved, request_id=req.request_id, decision=decision)
        if not decision.allow and req.call_id:
            self._denied.add(req.call_id)
        return decision

    async def call(self, method, params, *, timeout=15):
        """A request to the CLI, for its runtime's own methods (``AcpRuntime.steer``); raises
        ``RuntimeFailure`` when it fails or the CLI refuses it."""
        return await self._rpc.call(method, params, timeout=timeout)

    async def ask(self, questions, *, call_id=None):
        """Put a CLI's questions to the person (the contract's ``Question``
        entries: id, header, question, options, multi, secret) and return
        their answer, a mapping of question id to the chosen label(s) or
        typed text; None when the turn ended first or nothing came back. For
        a runtime's own question request (``AcpRuntime.requests``)."""
        if self.state not in ("running", "waiting_host") or not questions:
            return None
        question = c.Question(request_id=f"acp-{call_id or uuid4()}", run=None, questions=tuple(questions))
        self._emit(c.QuestionAsked, question=question)
        answer = await self._hold(self.host.answer_question(question))
        if not isinstance(answer, Mapping):
            return None
        self._emit(c.QuestionAnswered, request_id=question.request_id, answer=dict(answer))
        return answer

    def _update(self, method, params):
        if method != "session/update" or not self._turn or params.get("sessionId") != self.native_id:
            return
        data = params.get("update", {})
        kind = data.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            delta = data.get("content", {}).get("text")
            if isinstance(delta, str) and delta:
                if kind == "agent_message_chunk":
                    self._text.append(delta)
                    self._emit(c.TextDelta, block_id=self._turn, delta=delta)
                elif self.spec.thinking != "off":
                    self._thinking = True
                    self._emit(c.ThinkingDelta, block_id=self._turn + "-thinking", delta=delta)
        elif kind in ("tool_call", "tool_call_update") and data.get("toolCallId"):
            key = data["toolCallId"]
            known = self._calls.setdefault(key, {"toolCallId": key})
            machine = self._dialect.tool_name(data) or data.get("name") if kind == "tool_call" else None
            if kind == "tool_call":
                known["name"] = machine or data.get("title")
            known.update({k: data[k] for k in ("title", "kind", "rawInput") if data.get(k)})
            if kind == "tool_call":
                self._started(key, known, machine)
            for block in data.get("content") or ():
                content = block.get("content") if isinstance(block, dict) else None
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    self._emit(c.ToolOutputDelta, call_id=key, delta=content["text"])
            if data.get("status") in ("completed", "failed"):
                self._open_tools.discard(key)
                self._calls.pop(key, None)
                status = "denied" if key in self._denied else ("ok" if data["status"] == "completed" else "error")
                self._emit(c.ToolFinished, call_id=key, status=status)
        elif kind == "plan":
            states = {"completed": "done", "in_progress": "doing"}
            items = tuple(c.PlanItem(text=e["content"], state=states.get(e.get("status"), "todo"))
                          for e in data.get("entries", []) if isinstance(e.get("content"), str))
            self._emit(c.PlanUpdated, items=items)
        elif kind == "usage_update":
            self._emit(c.Usage, usage=c.TokenUsage(context_tokens=data.get("used"), context_window=data.get("size")))
        elif kind not in ("available_commands_update", "config_option_update", "current_mode_update",
                          "session_info_update", "user_message_chunk"):
            self._emit(c.Notice, code="unknown_wire", message=f"Unrecognized ACP update: {kind}")

    def _started(self, key, call, machine):
        """The tool card: the CLI's own title, or one made from the call's kind and target when its title
        is only the tool's machine name (``machine``), as some CLIs send it before their own title."""
        name = call.get("name") or "tool"
        args = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
        kind = self._kind_of(name, self._tool_kind(call))
        target = _target(kind, args)
        title = call.get("title")
        if (not title or title == machine) and target and kind in TITLES:
            title = TITLES[kind].format(target)
        self._open_tools.add(key)
        self._emit(c.ToolStarted, call_id=key, tool=name, title=title or "Tool call", kind=kind, target=target,
                   input=args)

    @staticmethod
    def _tool_kind(call):
        return ACP_KINDS.get(call.get("kind"), "other")

    async def send(self, msg):
        if self.state in ("running", "waiting_host") and self._dialect.steer and not msg.images:
            # A message sent mid-turn joins the running turn when the CLI takes one (``steer``).
            text = "\n\n".join(part for part in (msg.context, msg.text) if part)
            if await self._dialect.steer(self, text):
                return self._turn
            # Not taken: the message waits for this turn's end (the engine admits it then), never lost.
            raise c.RuntimeFailure("protocol", "The CLI didn't take the message mid-turn; it waits for the turn's end",
                                   retryable=True)
        if self.state != "idle":
            raise c.RuntimeFailure("protocol", "Session is busy or closed")
        prompt = []
        if self.spec.system_prompt and not self._native_prompt:
            prompt.append(dict(type="text", text=self.spec.system_prompt.text))
        if msg.context:
            prompt.append(dict(type="text", text=msg.context))
        prompt.append(dict(type="text", text=msg.text))
        for path in msg.images:
            content = await asyncio.to_thread(Path(path).read_bytes)
            prompt.append(dict(type="image", mimeType=mimetypes.guess_type(path)[0] or "application/octet-stream",
                               data=base64.b64encode(content).decode("ascii")))
        self.state = "running"
        self._turn = str(uuid4())
        self._text = []
        self._thinking = False
        self._denied.clear()
        self._calls.clear()
        self._emit(c.TurnStarted, origin=msg.origin)
        self._task = asyncio.create_task(self._prompt(prompt))
        return self._turn

    async def _prompt(self, prompt):
        try:
            result = await self._rpc.call("session/prompt", dict(sessionId=self.native_id, prompt=prompt), timeout=None)
            reason = result.get("stopReason")
            if not reason:
                raise c.RuntimeFailure("protocol", "Missing ACP stop reason")
            text = "".join(self._text)
            if text:
                self._emit(c.TextDone, block_id=self._turn, text=text)
            if self._thinking:
                self._emit(c.ThinkingDone, block_id=self._turn + "-thinking", duration_ms=None)
            counted = usage_of(result.get("usage") or (result.get("_meta") or {}).get("usage"))
            if counted is not None:
                self._emit(c.Usage, usage=counted)
            self._emit(c.TurnFinished, status="interrupted" if reason == "cancelled" else "completed",
                       text=text, usage=counted)
        except Exception as exc:
            self._emit(c.TurnFinished, status="failed", error=str(exc))
        finally:
            for task in self._reviews:
                task.cancel()
            for key in self._open_tools:
                self._emit(c.ToolFinished, call_id=key, status="cancelled")
            self._open_tools.clear()
            self._emit(c.SessionIdle)
            self._turn = None
            if self.state in ("running", "waiting_host"):
                self.state = "idle"

    async def interrupt(self, grace_s=10):
        if self.state in ("running", "waiting_host"):
            await self._rpc.notify("session/cancel", {"sessionId": self.native_id})
            # ACP: after session/cancel the client answers every open request "cancelled"; a CLI that
            # waits for that answer would otherwise hold the turn open until the grace ran out.
            for held in list(self._reviews):
                held.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), grace_s)
            except TimeoutError:
                await self.close()

    async def close(self, grace_s=5):
        if self.state == "closing":
            await self._closed.wait()
            return
        if self.state == "closed":
            return
        was_failed = self.state == "failed"
        self.state = "closing"
        try:
            await self._rpc.close(grace_s)
            if self._task:
                await asyncio.gather(self._task, return_exceptions=True)
            await self._watcher
        finally:
            if self._guard:
                await self._guard.close()
            self.state = "closed"
            self._closed.set()
            if not was_failed:
                self._emit(c.SessionClosed, reason="closed")

    async def update(self, **changes):
        raise c.RuntimeFailure("unsupported", "Start a new session to change these settings")

    async def stop_run(self, run_id):
        raise c.RuntimeFailure("unsupported", "Individual run control unavailable")

    async def send_to_run(self, run_id, msg):
        raise c.RuntimeFailure("unsupported", "Individual run input unavailable")
