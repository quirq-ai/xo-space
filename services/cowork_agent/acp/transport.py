from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from services.cowork_agent.adapters.contract import ProcessHandle, RuntimeFailure


class AcpResponseError(RuntimeFailure):
    def __init__(self, error: Mapping[str, Any]):
        self.rpc_code = error.get("code")
        super().__init__("protocol", str(error.get("message") or "ACP request failed"))


class AcpConnection:
    def __init__(self, process: ProcessHandle, *,
                 on_update: Callable[[str, dict], None],
                 on_request: Callable[[str, dict], Awaitable[dict]]):
        self.process = process
        self._update = on_update
        self._request = on_request
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()
        self._failure: RuntimeFailure | None = None
        self._closed = False
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        async for _ in self.process.stderr():
            pass

    async def _write(self, message: dict):
        data = json.dumps({"jsonrpc": "2.0", **message}, allow_nan=False).encode() + b"\n"
        async with self._write_lock:
            if self._failure:
                raise self._failure
            await self.process.write(data)

    async def notify(self, method: str, params: dict):
        await self._write({"method": method, "params": params})

    async def call(self, method: str, params: dict, *, timeout: float | None = 15) -> dict:
        key = next(self._ids)
        result = asyncio.get_running_loop().create_future()
        self._pending[key] = result
        try:
            async with asyncio.timeout(timeout):
                await self._write({"id": key, "method": method, "params": params})
                return await result
        finally:
            self._pending.pop(key, None)
            if result.done() and not result.cancelled():
                result.exception()
            else:
                result.cancel()

    async def _answer(self, key, method, params):
        try:
            reply = {"result": await self._request(method, params)}
        except NotImplementedError:
            reply = {"error": {"code": -32601, "message": "Method unavailable"}}
        except Exception:
            reply = {"error": {"code": -32603, "message": "Host request failed"}}
        await self._write({"id": key, **reply})

    def _answered(self, task: asyncio.Task):
        self._handlers.discard(task)
        if not task.cancelled():
            task.exception()

    async def _read(self):
        try:
            async for line in self.process.lines():
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeError):
                    self._update("invalid_message", {})
                    continue
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    raise ValueError("invalid ACP envelope")
                key = msg.get("id")
                if "method" in msg:
                    if key is None:
                        self._update(msg["method"], msg.get("params", {}))
                    else:
                        task = asyncio.create_task(self._answer(key, msg["method"], msg.get("params", {})))
                        self._handlers.add(task)
                        task.add_done_callback(self._answered)
                elif (pending := self._pending.get(key)) is not None and not pending.done():
                    if "error" in msg:
                        pending.set_exception(AcpResponseError(msg["error"]))
                    elif isinstance(msg.get("result"), dict):
                        pending.set_result(msg["result"])
                    else:
                        pending.set_exception(RuntimeFailure("protocol", "Malformed ACP response"))
        except Exception:
            self._failure = RuntimeFailure("protocol", "ACP stream failed")
        finally:
            self._failure = self._failure or RuntimeFailure("protocol", "ACP stream closed")
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(self._failure)
            for task in self._handlers:
                task.cancel()

    async def close(self, grace_s: float = 5):
        if self._closed:
            return
        self._closed = True
        try:
            await self.process.terminate(grace_s)
        finally:
            tasks = [self._reader, self._stderr, *self._handlers]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
