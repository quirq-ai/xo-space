"""Capability ``oneshot_prompt``: one tool-free agy completion (storm
ch 01 §4.7, ch 04 §10, ch 10 §8).

agy has no switch that removes its tools, so XO's hook refuses every tool
call ("answer from what you know") but ``finish``, the tool agy hands a
structured answer back with; the run is ``agy`` stream-json with
one message, in an empty temp folder, with the prompt on stdin, never argv.
The login still comes from agy's own token file. Reported ``emulated``
(core uses only a runtime whose ``check()`` reports native isolation, so
it never picks agy for these jobs).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.adapters.antigravity.runtime import argv as a
from services.cowork_agent.adapters.antigravity.runtime import install
from services.cowork_agent.adapters.antigravity.runtime.core import find_binary
from services.cowork_agent.adapters.antigravity.runtime.env import session_env
from services.cowork_agent.adapters.antigravity.runtime.hooks import HookServer
from services.cowork_agent.adapters.antigravity.runtime.wire import usage_of
from services.cowork_agent.adapters.contract import (
    Launcher,
    OneshotRequest,
    OneshotResult,
    RuntimeFailure,
    SessionSpec,
    SupportItem,
    SupportReport,
)

KILL_GRACE_S = 2.0
NO_TOOLS = "This task has no tools: answer from what you know, in your reply."
#: agy's tool for a structured answer (``--json-schema``): the one call a oneshot makes.
FINISH = "finish"


class _NoTools:
    async def on_hook(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if event != "PreToolUse":
            return {}
        call = payload.get("toolCall") if isinstance(payload.get("toolCall"), Mapping) else {}
        if call.get("name") == FINISH:
            return {"decision": "auto_approve"}       # how agy hands back a structured answer
        return {"decision": "deny", "reason": NO_TOOLS}

    def relay_parent(self) -> int | None:
        return None                               # no MCP server in a oneshot

    def on_mcp_hello(self, server: str) -> bool:
        return False

    async def on_mcp(self, server: str, message: Mapping[str, Any]) -> dict[str, Any] | None:
        return None


class AgyOneshot:
    def check(self) -> SupportReport:
        return SupportReport(items=(
            SupportItem(feature="isolation", support="emulated", mechanism="an empty temp folder; the person's "
                        "own agy setup still loads", enforced_by="cli"),
            SupportItem(feature="tools", support="emulated", mechanism="XO's hook refuses every tool call",
                        enforced_by="hook")))

    async def complete(self, req: OneshotRequest, launcher: Launcher) -> OneshotResult:
        await asyncio.to_thread(install.ensure, ())
        with tempfile.TemporaryDirectory(prefix="xo-agy-oneshot-") as folder:
            server = HookServer(Path(folder) / "run", _NoTools(), label="agy oneshot")
            await server.start()
            try:
                return await self._complete(req, launcher, folder, server.path)
            finally:
                await server.close()

    async def _complete(self, req: OneshotRequest, launcher: Launcher, folder: str, socket: str) -> OneshotResult:
        work = Path(folder) / "work"
        work.mkdir()
        schema = None
        if req.output_schema is not None:
            schema = str(Path(folder) / "schema.json")
            Path(schema).write_text(json.dumps(req.output_schema), encoding="utf-8")
        spec = SessionSpec(cwd=str(work), model=req.model, effort=req.effort)
        argv = a.argv(find_binary(), spec, mode="ask", resume=None, model=req.model, effort=req.effort,
                      schema_file=schema)
        env = await asyncio.to_thread(session_env, spec_env={}, socket=socket, label="agy oneshot")
        try:
            handle = await launcher.spawn(argv, cwd=str(work), env=env, label="agy oneshot")
        except OSError as exc:
            raise RuntimeFailure("cli_missing", f"the agy CLI can't be started ({argv[0]}: {exc.strerror or exc})") \
                from exc
        try:
            await handle.write(a.user_line(req.prompt))
            await handle.close_stdin()
            found = await asyncio.wait_for(_result(handle), req.timeout_s)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"agy didn't answer within {req.timeout_s:g} s", retryable=True) from exc
        finally:
            await handle.terminate(KILL_GRACE_S)
        if found is None:
            raise RuntimeFailure("process_died", "agy ended without an answer", retryable=True)
        if str(found.get("status")).upper() != "SUCCESS":
            raise RuntimeFailure("protocol", f"agy failed: {found.get('error') or found.get('status')}",
                                 retryable=True)
        text = found.get("response") if isinstance(found.get("response"), str) else None
        return OneshotResult(text=text.strip() if text else None, structured=found.get("structured_output"),
                             usage=usage_of(found.get("usage")))


async def _result(handle: Any) -> dict[str, Any] | None:
    found = None
    async for raw in handle.lines():
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("event") == "result" and isinstance(msg.get("result"), dict):
            found = msg["result"]
    await handle.wait()
    return found


def oneshot() -> AgyOneshot:
    return AgyOneshot()
