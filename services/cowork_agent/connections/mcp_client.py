"""A minimal streamable-HTTP JSON-RPC client: ``tools/list``, one ``tools/call``,
and :func:`execute_tool`, which runs a toolkit tool directly or through
Composio's tool-router executor when the session only lists meta tools.

Four requests per call against the entry ``build_mcp_server_entry`` hands
out (``{"type": "http", "url", "headers"}``): ``initialize`` (the
``Mcp-Session-Id`` response header, read case-insensitively, is echoed
on everything after), the ``notifications/initialized`` notification
(its body is never parsed; a 202/204 is the normal answer), ``tools/call``
and a best-effort ``DELETE`` to close the session.

Answers may be plain JSON (a dict, or a batch list) or ``text/event-stream``
with multi-line ``data:`` fields, ``event:``/``id:`` lines, comments and
CRLF endings; both go through the same extractor, which picks the message
whose id matches and falls back to the last parsable one.

``entry["headers"]`` carries the upstream credential. Nothing here logs
the entry, a header or a body: log lines carry the tool name, the HTTP
status and the body length only, and every :class:`McpError` snippet is
scrubbed of header values before it is built. Transport failures are
re-raised as :class:`McpError` with the exception type only, since an
httpx message can include the URL.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "xo-space-connections", "version": "1"}
_SNIPPET_MAX = 200
_TOOL_ERROR_MAX = 500
_INITIALIZE_ID, _CALL_ID = 1, 2

#: Tests set this to an ``httpx.MockTransport`` so no socket is ever opened.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class McpError(RuntimeError):
    """The upstream refused, failed, or answered with a JSON-RPC or tool error."""


def _client(timeout_s: float) -> httpx.AsyncClient:
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=30.0, pool=10.0)
    return httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT)


# ── Message extraction ───────────────────────────────────────────────────────


def _scrub(text: str, secrets: dict) -> str:
    """Replace every header value (and its last whitespace-separated token,
    for ``Bearer <token>`` shapes) that shows up in ``text``."""
    for value in (secrets or {}).values():
        if not isinstance(value, str):
            continue
        for candidate in {value, value.split()[-1] if value.split() else ""}:
            if len(candidate) >= 8 and candidate in text:
                text = text.replace(candidate, "[redacted]")
    return text


def _snippet(resp: httpx.Response, secrets: dict) -> str:
    return _scrub(resp.text, secrets)[:_SNIPPET_MAX]


def _parse_sse(text: str) -> list[Any]:
    """Events are blank-line separated; the ``data:`` lines of one event
    are joined with newlines (one optional leading space stripped). Events
    that do not parse as JSON are ignored."""
    messages: list[Any] = []
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data = [line[6:] if line.startswith("data: ") else line[5:]
                for line in block.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            messages.append(json.loads("\n".join(data)))
        except ValueError:
            continue
    return messages


def _messages(resp: httpx.Response) -> list[dict]:
    """Every JSON-RPC message in the response, SSE or plain JSON, batch
    lists flattened."""
    content_type = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        parsed = _parse_sse(resp.text)
    else:
        try:
            parsed = [json.loads(resp.text)] if resp.text.strip() else []
        except ValueError:
            parsed = []
    out: list[dict] = []
    for message in parsed:
        if isinstance(message, dict):
            out.append(message)
        elif isinstance(message, list):
            out.extend(m for m in message if isinstance(m, dict))
    return out


def _pick(messages: list[dict], wanted_id: int) -> Optional[dict]:
    for message in messages:
        if "id" in message and str(message.get("id")) == str(wanted_id):
            return message
    return messages[-1] if messages else None


def _joined_text(result: dict) -> str:
    parts = [item["text"] for item in result.get("content") or []
             if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
    return "\n".join(parts)


# ── Sessions and calls ───────────────────────────────────────────────────────

_LIST_ID = 3
#: Composio's tool-router session lists only meta tools; toolkit tools run
#: through this one. :func:`execute_tool` falls back to it when a slug is
#: not exposed directly.
EXECUTOR_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"


async def _post(client: httpx.AsyncClient, url: str, headers: dict, body: dict, stage: str) -> httpx.Response:
    try:
        return await client.post(url, headers=headers, content=json.dumps(body).encode("utf-8"))
    except httpx.HTTPError as exc:
        raise McpError(f"{stage}: {type(exc).__name__} while calling the MCP upstream") from None


def _prepare(entry: dict) -> tuple[str, dict, dict]:
    url = entry.get("url") if isinstance(entry, dict) else None
    if not isinstance(url, str) or not url:
        raise McpError("MCP entry has no url")
    secrets = dict(entry.get("headers") or {})
    headers = dict(secrets)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    return url, headers, secrets


async def _handshake(client: httpx.AsyncClient, url: str, headers: dict, secrets: dict) -> Optional[str]:
    """``initialize`` plus the ``notifications/initialized`` notification.
    Returns the session id (already added to ``headers``) when one was issued."""
    resp = await _post(client, url, headers, {
        "jsonrpc": "2.0", "id": _INITIALIZE_ID, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
    }, "initialize")
    if resp.status_code // 100 != 2:
        raise McpError(f"initialize failed: HTTP {resp.status_code} {_snippet(resp, secrets)}")
    session_id = resp.headers.get("mcp-session-id") or None
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    logger.debug("mcp: initialize -> HTTP %s (%d bytes)", resp.status_code, len(resp.content))

    resp = await _post(client, url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       "notifications/initialized")
    if resp.status_code >= 400:
        # Some servers answer notifications with an error status and still serve
        # the calls that follow; those are the real test of the session.
        logger.debug("mcp: notifications/initialized -> HTTP %s", resp.status_code)
    return session_id


async def _close(client: httpx.AsyncClient, url: str, headers: dict, session_id: Optional[str]) -> None:
    """Best effort, Exception only: a CancelledError from the poller shutting
    down must still propagate."""
    if not session_id:
        return
    try:
        await client.delete(url, headers=headers)
    except Exception as exc:
        logger.debug("mcp: session close failed: %s", type(exc).__name__)


def _rpc_result(resp: httpx.Response, secrets: dict, wanted_id: int, what: str) -> dict:
    if resp.status_code >= 400:
        raise McpError(f"{what} failed: HTTP {resp.status_code} {_snippet(resp, secrets)}")
    message = _pick(_messages(resp), wanted_id)
    if message is None:
        raise McpError(f"{what}: no JSON-RPC message in the response")
    if message.get("error") is not None:
        error = message["error"]
        text = error.get("message") if isinstance(error, dict) else None
        raise McpError(_scrub(str(text or error), secrets)[:_TOOL_ERROR_MAX])
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError(f"{what}: the response carries no result")
    return result


async def call_tool(entry: dict, name: str, arguments: dict, *, timeout_s: float = 60.0,
                    raise_on_tool_error: bool = True) -> dict:
    """Run one tool and return its ``result`` dict. Raises :class:`McpError`
    on any HTTP, JSON-RPC, transport or tool (``isError``) failure; with
    ``raise_on_tool_error`` false an ``isError`` result is returned as is
    (the executor's answer carries per-tool errors inside its text)."""
    url, headers, secrets = _prepare(entry)
    session_id: Optional[str] = None
    async with _client(timeout_s) as client:
        try:
            session_id = await _handshake(client, url, headers, secrets)
            resp = await _post(client, url, headers, {
                "jsonrpc": "2.0", "id": _CALL_ID, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, "tools/call")
            logger.debug("mcp: tools/call %s -> HTTP %s (%d bytes)", name, resp.status_code, len(resp.content))
            result = _rpc_result(resp, secrets, _CALL_ID, f"tools/call {name}")
            if result.get("isError") and raise_on_tool_error:
                raise McpError(_scrub(_joined_text(result) or "tool reported an error", secrets)[:_TOOL_ERROR_MAX])
            return result
        finally:
            await _close(client, url, headers, session_id)


async def list_tools(entry: dict, *, timeout_s: float = 30.0) -> list[str]:
    """The tool names this session exposes (``tools/list``)."""
    url, headers, secrets = _prepare(entry)
    session_id: Optional[str] = None
    async with _client(timeout_s) as client:
        try:
            session_id = await _handshake(client, url, headers, secrets)
            resp = await _post(client, url, headers,
                               {"jsonrpc": "2.0", "id": _LIST_ID, "method": "tools/list", "params": {}}, "tools/list")
            logger.debug("mcp: tools/list -> HTTP %s (%d bytes)", resp.status_code, len(resp.content))
            tools = _rpc_result(resp, secrets, _LIST_ID, "tools/list").get("tools")
            if not isinstance(tools, list):
                return []
            return [t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
        finally:
            await _close(client, url, headers, session_id)


def _text_result(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _unwrap_executor(payload: Any, slug: str) -> dict:
    """The executor answers ``{data: {results: [one per requested tool]}}``
    where a result is ``{tool_slug, index, response: {successful, data}}`` or
    ``{tool_slug, index, error}``. Reduce the first result to the toolkit
    envelope ``{successful, data, error}``; a per-tool error is raised."""
    data = payload.get("data") if isinstance(payload, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    first = next((r for r in results if isinstance(r, dict)), None) if isinstance(results, list) else None
    if first is None:
        if isinstance(payload, dict) and payload.get("successful") is False:
            raise McpError(str(payload.get("error") or "executor reported failure")[:_TOOL_ERROR_MAX])
        raise McpError(f"{EXECUTOR_TOOL} returned no result for {slug}")
    if first.get("error"):
        raise McpError(str(first["error"])[:_TOOL_ERROR_MAX])
    response = first.get("response")
    if isinstance(response, dict) and ("successful" in response or "data" in response):
        return {"successful": response.get("successful", True), "data": response.get("data", {}),
                "error": response.get("error")}
    return {"successful": True, "data": response if response is not None else {}, "error": None}


async def execute_tool(entry: dict, slug: str, arguments: dict, *, tool_names: Optional[list[str]] = None,
                       timeout_s: float = 60.0) -> dict:
    """Run toolkit tool ``slug`` and return an MCP-shaped result whose text
    content is the toolkit envelope ``{"successful", "data", "error"}``.

    A plain MCP server lists toolkit tools by slug, so they are called
    directly. Composio's tool-router session lists only meta tools and runs
    toolkit tools through :data:`EXECUTOR_TOOL`; its answer is unwrapped so
    every caller sees one shape. ``tool_names`` is the ``tools/list`` answer
    when the caller already has it (one listing per poll, not per collector).
    """
    names = tool_names if tool_names is not None else await list_tools(entry, timeout_s=min(timeout_s, 30.0))
    if slug in names:
        return await call_tool(entry, slug, arguments, timeout_s=timeout_s)
    if EXECUTOR_TOOL not in names:
        raise McpError(f"{slug} is not exposed by this session and {EXECUTOR_TOOL} is unavailable")
    wrapper = {"tools": [{"tool_slug": slug, "arguments": arguments}], "sync_response_to_workbench": False}
    result = await call_tool(entry, EXECUTOR_TOOL, wrapper, timeout_s=timeout_s, raise_on_tool_error=False)
    return _text_result(_unwrap_executor(tool_result_json(result), slug))


def tool_result_json(result: dict) -> Any:
    """The payload inside a result: the first ``text`` content item that
    parses as JSON; ``{"text": <joined text>}`` when none parses; with no
    text content at all, ``structuredContent`` or ``{}``."""
    content = result.get("content") if isinstance(result, dict) else None
    texts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            continue
        texts.append(item["text"])
        try:
            return json.loads(item["text"])
        except ValueError:
            continue
    if texts:
        return {"text": "\n".join(texts)}
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    return structured or {}
