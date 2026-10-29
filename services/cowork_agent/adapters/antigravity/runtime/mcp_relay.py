"""The HTTP side of XO's agy MCP relay (storm ch 04 §2; ch 12 §2.1).

agy talks to each of a session's MCP servers through the plugin's stdio
relay (``plugin/xo_agy_mcp.py``), which hands every JSON-RPC message to the
session's socket. Here the runtime posts it to the real server (Streamable
HTTP: a JSON answer, or an event stream holding it), with the server's
bearer token from the spec's env. The token stays in XO's process: agy, its
env and its shell never see it.

A server the session doesn't mount answers ``initialize`` and lists no tools,
so agy shows nothing for it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import httpx

from services.cowork_agent.adapters.contract import McpServer

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 600.0
CONNECT_TIMEOUT_S = 10.0
SESSION_HEADER = "mcp-session-id"


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def unmounted(name: str, msg: Mapping[str, Any]) -> dict[str, Any] | None:
    """The answer for a name this session doesn't mount."""
    rid = msg.get("id")
    if rid is None:
        return None
    method = msg.get("method")
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
            "serverInfo": {"name": name, "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": []}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return _error(rid, -32601, f"{name} isn't mounted in this session")


def _from_stream(text: str, rid: Any) -> dict[str, Any] | None:
    """The JSON-RPC answer to ``rid`` in an event stream."""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == rid and ("result" in msg or "error" in msg):
            return msg
    return None


class Upstream:
    """One MCP server of the session, over HTTP."""

    def __init__(self, server: McpServer, client: httpx.AsyncClient) -> None:
        self.server = server
        self._client = client
        self._session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        token = self.server.env.get(self.server.bearer_env) if self.server.bearer_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def send(self, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        rid = msg.get("id")
        if self.server.url is None:
            return unmounted(self.server.name, msg)
        try:
            response = await self._client.post(self.server.url, json=msg, headers=self._headers(),
                                               timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        except httpx.HTTPError as exc:
            log.info("agy relay: %s didn't answer %s (%s)", self.server.name, msg.get("method"), type(exc).__name__)
            return None if rid is None else _error(rid, -32603, f"{self.server.name} didn't answer "
                                                                f"({type(exc).__name__}); try again")
        if response.headers.get(SESSION_HEADER):
            self._session_id = response.headers[SESSION_HEADER]
        if rid is None:
            return None
        kind = response.headers.get("content-type", "")
        try:
            if "text/event-stream" in kind:
                found = _from_stream(response.text, rid)
            else:
                found = response.json() if response.content else None
        except ValueError:
            found = None
        if isinstance(found, list):
            found = next((m for m in found if isinstance(m, dict) and m.get("id") == rid), None)
        if isinstance(found, dict) and ("result" in found or "error" in found):
            return {**found, "id": rid}
        if isinstance(found, dict) and isinstance(found.get("error"), dict):
            return _error(rid, int(found["error"].get("code") or -32603), str(found["error"].get("message")))
        return _error(rid, -32603, f"{self.server.name} answered HTTP {response.status_code} without a result")
