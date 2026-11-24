from __future__ import annotations

import asyncio
import base64
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .transport import AcpConnection
from services.cowork_agent.adapters import contract as c


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
    def __init__(self, session_id, spec, host, report, process, *, guard=None):
        self.id = session_id
        self.spec = spec
        self.host = host
        self.report = report
        self._guard = guard
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
        self._emit(c.SessionStarted, model=self.spec.model, cwd=self.spec.cwd, cli_version=version)

    async def _request(self, method, params):
        if method != "session/request_permission":
            raise NotImplementedError(method)
        cancelled = {"outcome": {"outcome": "cancelled"}}
        if self.state not in ("running", "waiting_host") or params.get("sessionId") != self.native_id:
            return cancelled
        call = params.get("toolCall") or {}
        req = self._permission(call.get("name") or call.get("title") or "tool", call.get("toolCallId"),
                               call.get("rawInput") or {}, self._tool_kind(call))
        decision = await self._decide(req)
        kind = "allow_once" if decision.allow and not decision.updated_input and not decision.stop_turn else "reject_once"
        for option in params.get("options", []):
            if option.get("kind") == kind:
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return cancelled

    def _permission(self, tool, key, args, kind="other"):
        names = {"bash": "command", "shell": "command", "run_terminal_cmd": "command",
                 "read": "read", "read_file": "read", "readfile": "read", "grep": "search",
                 "glob": "list", "edit": "edit_file", "write": "create_file", "delete": "delete_file",
                 "webfetch": "web_fetch", "websearch": "web_search"}
        kind = names.get(tool.lower(), kind)
        mcp = None
        qualified = tool.removeprefix("mcp__")
        for server in self.spec.mcp_servers:
            if qualified.startswith(server.name + "__"):
                mcp = (server.name, qualified[len(server.name)+2:])
                kind = "mcp"
                break
        path = args.get("file_path") or args.get("path") or args.get("target_file")
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
        decision = await self._decide(req)
        return decision.allow and not decision.updated_input and not decision.stop_turn

    async def _decide(self, req):
        turn = self._turn
        task = asyncio.current_task()
        self._reviews.add(task)
        self._emit(c.PermissionRequested, request=req)
        self.state = "waiting_host"
        try:
            decision = await self.host.decide_permission(req)
        finally:
            self._reviews.discard(task)
            if self.state == "waiting_host" and not self._reviews:
                self.state = "running"
        if turn != self._turn or self.state in ("closing", "closed", "failed"):
            return c.PermissionDecision(allow=False, message="Turn ended")
        self._emit(c.PermissionResolved, request_id=req.request_id, decision=decision)
        if not decision.allow and req.call_id:
            self._denied.add(req.call_id)
        return decision

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
            if kind == "tool_call":
                title = data.get("title") or "Tool call"
                self._open_tools.add(key)
                self._emit(c.ToolStarted, call_id=key, tool=data.get("name") or title, title=title,
                           kind=self._tool_kind(data), target=None, input=data.get("rawInput") or {})
            for block in data.get("content") or ():
                content = block.get("content") if isinstance(block, dict) else None
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    self._emit(c.ToolOutputDelta, call_id=key, delta=content["text"])
            if data.get("status") in ("completed", "failed"):
                self._open_tools.discard(key)
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

    @staticmethod
    def _tool_kind(call):
        return {"execute": "command", "edit": "edit_file", "delete": "delete_file",
                "read": "read", "search": "search", "fetch": "web_fetch"}.get(call.get("kind"), "other")

    async def send(self, msg):
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
            usage = result.get("usage") or (result.get("_meta") or {}).get("usage")
            counted = None
            if isinstance(usage, dict) and ("inputTokens" in usage or "outputTokens" in usage):
                cached = usage.get("cachedReadTokens", 0)
                written = usage.get("cachedWriteTokens", 0)
                counted = c.TokenUsage(input=max(0, usage.get("inputTokens", 0) - cached - written),
                                       output=usage.get("outputTokens", 0), cache_read=cached, cache_write=written,
                                       reasoning=usage.get("thoughtTokens", 0))
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
