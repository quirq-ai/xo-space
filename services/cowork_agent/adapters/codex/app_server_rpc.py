"""
One JSON-RPC call to the codex app-server daemon over its control socket.

The CLI exposes start / pair / stop but not the read-only remote-control
methods (connection status, paired devices, whether a pairing code was
claimed), so ``remote_control.py`` asks the daemon directly, the way the CLI
itself does (codex-rs ``app-server-daemon/src/client.rs``): a WebSocket over
the Unix socket at ``ws://localhost/``, an ``initialize`` request that opts
into the experimental API, an ``initialized`` notification, then the call.
Messages carry no ``jsonrpc`` field. The ``remoteControl/*`` methods are
marked experimental upstream, so callers treat every failure as "unknown".
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT_SECONDS = 5.0
_INITIALIZE_ID = 1
_CALL_ID = 2
_CLIENT_INFO = {"name": "xo_space", "title": "XO Space", "version": "1"}


class AppServerRpcError(Exception):
    """The daemon could not be reached, or answered the call with an error."""


async def call(
    socket_path: Path,
    method: str,
    params: Optional[dict[str, Any]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """``method``'s result object. Raises ``AppServerRpcError`` on any failure."""
    try:
        return await asyncio.wait_for(_call(socket_path, method, params), timeout=timeout)
    except asyncio.TimeoutError:
        raise AppServerRpcError(f"{method} did not answer within {timeout:g}s") from None
    except AppServerRpcError:
        raise
    except Exception as exc:  # connect/handshake/socket failures of any kind
        raise AppServerRpcError(f"{method} failed: {exc}") from exc


async def _call(socket_path: Path, method: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
    # Imported here so a missing install degrades status, not the server's boot.
    from websockets.asyncio.client import unix_connect

    async with unix_connect(str(socket_path), uri="ws://localhost/") as ws:
        await ws.send(json.dumps({
            "id": _INITIALIZE_ID,
            "method": "initialize",
            "params": {"clientInfo": _CLIENT_INFO, "capabilities": {"experimentalApi": True}},
        }))
        await _read_result(ws, _INITIALIZE_ID, "initialize")
        await ws.send(json.dumps({"method": "initialized"}))
        request: dict[str, Any] = {"id": _CALL_ID, "method": method}
        if params is not None:
            request["params"] = params
        await ws.send(json.dumps(request))
        return await _read_result(ws, _CALL_ID, method)


async def _read_result(ws: Any, request_id: int, method: str) -> dict[str, Any]:
    """Skip notifications until the response to ``request_id`` arrives."""
    async for frame in ws:
        if not isinstance(frame, str):
            continue
        try:
            message = json.loads(frame)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            error = message.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else None
            raise AppServerRpcError(f"{method} failed: {detail or 'unknown error'}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}
    raise AppServerRpcError(f"the daemon closed the socket before answering {method}")


__all__ = ["AppServerRpcError", "DEFAULT_TIMEOUT_SECONDS", "call"]
