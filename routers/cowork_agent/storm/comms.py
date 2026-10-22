"""``xo_comms``, the MCP server storm's sessions talk through (design ch 12
§2.2; ch 17 §2.9). Not for UIs, hidden from OpenAPI:

  POST   /mcp/space-comms   JSON-RPC 2.0: initialize, tools/list, tools/call, ping
  GET    /mcp/space-comms   405: no server stream (every answer comes on the POST)
  DELETE /mcp/space-comms   200: a client ending its MCP session

Streamable HTTP with JSON answers only, written by hand so it doesn't depend
on an MCP package's version. Loopback callers only (the CLIs run on this
machine), and never a browser page (a request with an ``Origin`` is refused).
The token travels in ``Authorization: Bearer``, never in the path: unknown
or retired → 401, an earlier epoch's → 409 ``epoch_stale``. The token decides
the caller's role, and the role its tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from routers.browser_guard import is_loopback_peer
from services.cowork_agent.storm import chats
from services.cowork_agent.storm.comms import calls, tools
from services.cowork_agent.storm.comms.handlers import HANDLERS
from services.errors import ServiceError

log = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "1.0"
#: JSON-RPC's own error codes, and the one this server adds for a refused token.
INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, TOKEN_REFUSED = -32600, -32601, -32700, -32001


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message, **({"data": data} if data is not None else {})}
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _tool_result(rid: Any, text: str, *, error: bool) -> dict[str, Any]:
    return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": error})


async def _answer(service: chats.Chats, caller: calls.Caller, message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
    rid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if not isinstance(params, dict):
        return _error(rid, INVALID_REQUEST, "params must be an object")
    if "id" not in message:
        return None                     # a notification (notifications/initialized): no answer
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(rid, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": tools.SERVER_NAME, "version": SERVER_VERSION},
                         "instructions": f"XO Space's tools for a {caller.role}. In a workspace or project chat, "
                                         "text outside these tools reaches no one."})
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": tools.tools_for(caller.role)})
    if method == "tools/call":
        try:
            result = await calls.call(service, caller, params.get("name"), params.get("arguments") or {}, HANDLERS)
        except calls.CommsError as exc:
            return _tool_result(rid, exc.message, error=True)
        return _tool_result(rid, json.dumps(result, ensure_ascii=False), error=False)
    return _error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")


@router.post(tools.PATH, include_in_schema=False)
async def space_comms(request: Request) -> Response:
    if not is_loopback_peer(request) or request.headers.get("origin") is not None:
        return JSONResponse({"error": "loopback_only"}, status_code=403)
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return JSONResponse(_error(None, TOKEN_REFUSED, "Send the session's comms token as a Bearer token."),
                            status_code=401)
    try:
        service = chats.current()
        caller = await calls.resolve(service.engine, token.strip())
    except ServiceError as exc:
        return JSONResponse(_error(None, TOKEN_REFUSED, exc.message, {"code": exc.code}), status_code=exc.status)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400)
    messages = body if isinstance(body, list) else [body]
    try:
        answers = [a for a in [await _answer(service, caller, m) for m in messages] if a is not None]
    except calls.TokenRefused as exc:      # a newer process attached while the call ran
        return JSONResponse(_error(None, TOKEN_REFUSED, exc.message, {"code": exc.code}), status_code=exc.status)
    if not answers:
        return Response(status_code=202)
    return JSONResponse(answers if isinstance(body, list) else answers[0])


@router.get(tools.PATH, include_in_schema=False)
async def space_comms_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete(tools.PATH, include_in_schema=False)
async def space_comms_end() -> Response:
    return Response(status_code=200)
