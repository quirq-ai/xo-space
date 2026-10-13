"""``xo_comms``: the MCP server each projects-chat agent session talks through.

  POST /mcp/space-comms/{token}     JSON-RPC 2.0 (initialize, tools/list, tools/call, ping)

A small hand-written MCP endpoint (streamable HTTP, JSON responses, no server
stream), so it does not depend on the ``mcp`` package's major version. The
token in the path is the session's per-turn comms token
(``store.issue_token``); it decides who is calling and therefore which tools
exist. Only loopback callers are served: the agent CLI runs on this machine.
Tool semantics live in ``services/projects_chat/comms.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from services.projects_chat import workspace as _workspace_tools  # noqa: F401  (registers the workspace tools)
from services.projects_chat import comms, store

log = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _handle(caller: comms.Caller, message: Any) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    rid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if rid is None:  # a notification (e.g. notifications/initialized): no answer
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(rid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": comms.SERVER_NAME, "version": "0.1.0"},
            "instructions": f"Tools for a {caller.role} in XO Space's projects chat. Text outside these tools "
                            "reaches no one.",
        })
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": comms.tools_for(caller.role)})
    if method == "tools/call":
        name = params.get("name")
        try:
            result = comms.call(caller, name, params.get("arguments") or {})
        except comms.CommsError as exc:
            return _ok(rid, {"content": [{"type": "text", "text": exc.message}], "isError": True})
        except Exception:  # noqa: BLE001 - the agent gets a message, the log gets the trace
            log.exception("xo_comms: %s failed for %s", name, caller.session_id)
            return _ok(rid, {"content": [{"type": "text", "text": "The server could not run that tool."}],
                             "isError": True})
        return _ok(rid, {"content": [{"type": "text", "text": comms.result_text(result)}], "isError": False})
    return _error(rid, -32601, f"Method not found: {method}")


@router.post("/mcp/space-comms/{token}", include_in_schema=False)
async def space_comms(token: str, request: Request):
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK:
        return JSONResponse({"error": "loopback_only"}, status_code=403)
    found = store.resolve_token(token)
    if found is None:
        return JSONResponse(_error(None, -32001, "Unknown or expired session token"), status_code=401)
    project, session = found
    caller = comms.Caller(project=project, session=session)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
    from starlette.concurrency import run_in_threadpool

    if isinstance(body, list):
        answers = [a for a in [await run_in_threadpool(_handle, caller, m) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await run_in_threadpool(_handle, caller, body)
    return JSONResponse(answer) if answer is not None else Response(status_code=202)


@router.get("/mcp/space-comms/{token}", include_in_schema=False)
async def space_comms_stream(token: str):
    # No server-initiated stream: every answer comes back on the POST.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp/space-comms/{token}", include_in_schema=False)
async def space_comms_end(token: str):
    return Response(status_code=200)
