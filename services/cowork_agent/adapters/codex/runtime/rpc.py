"""codex's app-server JSON-RPC, as one session's process speaks it (storm
ch 03 §1).

``codex app-server --listen stdio://`` exchanges one JSON object per line,
without the ``jsonrpc`` field. Three kinds arrive:

* a **response** to a request this side sent: ``id`` and ``result`` or
  ``error``, no ``method``;
* a **notification**: ``method`` and ``params``, no ``id``;
* a **server request**: ``method``, ``params`` and an ``id`` of the server's
  own numbering (it starts at 0, like ours could: the two never mix, because
  a response never carries a ``method``), answered by ``id``.

``Rpc`` keeps the requests this side is waiting on and frames what it
writes; the session reads the process and hands each line to ``classify``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from services.cowork_agent.adapters.contract import RuntimeFailure

#: JSON-RPC's "method not found", for a server request this client doesn't serve.
METHOD_NOT_FOUND = -32601
#: What the client says of itself in ``initialize``.
CLIENT_INFO = {"name": "xo_space", "title": "XO Space", "version": "1"}


@dataclass(frozen=True)
class Line:
    """One line from the server, classified."""

    kind: str                           # "response" | "notification" | "request" | "unknown"
    msg: dict[str, Any]

    @property
    def method(self) -> str:
        method = self.msg.get("method")
        return method if isinstance(method, str) else ""

    @property
    def params(self) -> dict[str, Any]:
        params = self.msg.get("params")
        return params if isinstance(params, dict) else {}


def classify(raw: bytes | str) -> Line:
    try:
        msg = json.loads(raw)
    except ValueError:
        return Line("unknown", {"line": raw[:200] if isinstance(raw, str) else raw[:200].decode(errors="replace")})
    if not isinstance(msg, dict):
        return Line("unknown", {"line": str(msg)[:200]})
    has_method = isinstance(msg.get("method"), str)
    if "id" in msg and not has_method and ("result" in msg or "error" in msg):
        return Line("response", msg)
    if has_method and "id" in msg:
        return Line("request", msg)
    if has_method:
        return Line("notification", msg)
    return Line("unknown", msg)


class RpcError(RuntimeFailure):
    """The server answered a request with an error."""

    def __init__(self, method: str, error: object) -> None:
        detail = error.get("message") if isinstance(error, dict) else None
        self.rpc_code = error.get("code") if isinstance(error, dict) else None
        super().__init__("protocol", f"codex refused {method}: {detail or error}")


class Rpc:
    """The requests in flight and the writes, for one connection."""

    def __init__(self, write: Callable[[bytes], Awaitable[None]]) -> None:
        self._write = write
        self._next_id = 1
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}

    async def send(self, obj: dict[str, Any]) -> None:
        await self._write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Send a request and wait for its result. Raises ``RpcError`` for an
        error answer, ``RuntimeFailure("timeout")`` past ``timeout``, and
        ``RuntimeFailure("process_died")`` when the connection ends first."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            msg = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"codex didn't answer {method} within {timeout:g} s",
                                 retryable=True) from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in msg:
            raise RpcError(method, msg["error"])
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.send({"method": method, **({"params": params} if params is not None else {})})

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self.send({"id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self.send({"id": request_id, "error": {"code": code, "message": message}})

    def resolve(self, msg: dict[str, Any]) -> bool:
        """Hand a response to the request waiting for it; False when nothing
        waits (a late answer to a request that timed out)."""
        entry = self._pending.get(msg.get("id")) if isinstance(msg.get("id"), int) else None
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(msg)
        return True

    def fail_all(self, failure: RuntimeFailure) -> None:
        """The connection ended: every waiting request fails with ``failure``."""
        for _method, future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()


def initialize_params() -> dict[str, Any]:
    return {"clientInfo": dict(CLIENT_INFO), "capabilities": {"experimentalApi": True}}


def version_of(user_agent: object) -> str | None:
    """The CLI version in ``initialize``'s ``userAgent``
    (``xo_space/0.162.1 (Ubuntu …) …``)."""
    if not isinstance(user_agent, str) or "/" not in user_agent:
        return None
    version = user_agent.split("/", 1)[1].split(" ", 1)[0]
    return version or None
