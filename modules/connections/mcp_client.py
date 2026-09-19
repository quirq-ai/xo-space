"""A minimal streamable-HTTP JSON-RPC client. :class:`McpSession` holds one
MCP session: the handshake once, then any number of ``tools/list`` and
``tools/call`` over the same ``Mcp-Session-Id`` and httpx client, then a
best-effort ``DELETE``. :meth:`McpSession.execute_tool` runs a toolkit tool
directly or through Composio's tool-router executor when the session only
lists meta tools. The module-level :func:`call_tool`, :func:`list_tools` and
:func:`execute_tool` are one-shot wrappers: one session per call.

A session is built from the entry ``build_mcp_server_entry`` hands out
(``{"type": "http", "url", "headers"}``). Opening it is two requests:
``initialize`` (the ``Mcp-Session-Id`` response header, read
case-insensitively, is echoed on everything after) and the
``notifications/initialized`` notification (its body is never parsed; a
202/204 is the normal answer). Every request in a session carries its own
JSON-RPC id (a counter from 1), so the poller's one session per poll pays
for the handshake once, not once per collector.

Answers may be plain JSON (a dict, or a batch list) or ``text/event-stream``
with multi-line ``data:`` fields, ``event:``/``id:`` lines, comments and
CRLF endings; both go through the same extractor, which picks the message
whose id matches and falls back to the last parsable one.

``entry["headers"]`` carries the upstream credential. Nothing here logs
the entry, a header or a body: log lines carry the tool name, the HTTP
status and the body length only, and every :class:`McpError` snippet is
scrubbed of header values before it is built. Transport failures are
re-raised as :class:`McpError` with the exception type only, since an
httpx message can include the URL. Every :class:`McpError` also carries
the ``stage`` that failed and the HTTP ``status`` when there was one, so
callers branch on those rather than on the message text.
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

#: Tests set this to an ``httpx.MockTransport`` so no socket is ever opened.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class McpError(RuntimeError):
    """The upstream refused, failed, or answered with a JSON-RPC or tool error.

    ``stage`` names the step that failed: ``"entry"`` (no url),
    ``"session"`` (used before it was opened, or opened twice), ``"initialize"``,
    ``"notifications/initialized"``, ``"tools/list"``, ``"tools/call"`` or
    ``"execute"`` (:meth:`McpSession.execute_tool`'s own checks and the
    executor's per-tool errors). ``status`` is the HTTP status of the answer
    that produced the error, ``None`` when there was no answer (a transport
    failure, a bad entry, an executor payload). The message text is for
    people; code reads these two."""

    def __init__(self, message: str, *, stage: Optional[str] = None, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status


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


# ── Requests ─────────────────────────────────────────────────────────────────

#: Composio's tool-router session lists only meta tools; toolkit tools run
#: through this one. :meth:`McpSession.execute_tool` falls back to it when a
#: slug is not exposed directly.
EXECUTOR_TOOL = "COMPOSIO_MULTI_EXECUTE_TOOL"


async def _post(client: httpx.AsyncClient, url: str, headers: dict, body: dict, stage: str) -> httpx.Response:
    try:
        return await client.post(url, headers=headers, content=json.dumps(body).encode("utf-8"))
    except httpx.HTTPError as exc:
        raise McpError(f"{stage}: {type(exc).__name__} while calling the MCP upstream", stage=stage) from None


def _prepare(entry: dict) -> tuple[str, dict, dict]:
    url = entry.get("url") if isinstance(entry, dict) else None
    if not isinstance(url, str) or not url:
        raise McpError("MCP entry has no url", stage="entry")
    secrets = dict(entry.get("headers") or {})
    headers = dict(secrets)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    return url, headers, secrets


async def _handshake(client: httpx.AsyncClient, url: str, headers: dict, secrets: dict,
                     request_id: int) -> Optional[str]:
    """``initialize`` (with JSON-RPC id ``request_id``) plus the
    ``notifications/initialized`` notification. Returns the session id
    (already added to ``headers``) when one was issued."""
    resp = await _post(client, url, headers, {
        "jsonrpc": "2.0", "id": request_id, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
    }, "initialize")
    if resp.status_code // 100 != 2:
        raise McpError(f"initialize failed: HTTP {resp.status_code} {_snippet(resp, secrets)}",
                       stage="initialize", status=resp.status_code)
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


def _rpc_result(resp: httpx.Response, secrets: dict, wanted_id: int, what: str, stage: str) -> dict:
    """The ``result`` dict of the message answering ``wanted_id``. ``what``
    names the request in messages (``tools/call <name>``), ``stage`` is the
    :class:`McpError` stage (``tools/call``)."""
    status = resp.status_code
    if status >= 400:
        raise McpError(f"{what} failed: HTTP {status} {_snippet(resp, secrets)}", stage=stage, status=status)
    message = _pick(_messages(resp), wanted_id)
    if message is None:
        raise McpError(f"{what}: no JSON-RPC message in the response", stage=stage, status=status)
    if message.get("error") is not None:
        error = message["error"]
        text = error.get("message") if isinstance(error, dict) else None
        raise McpError(_scrub(str(text or error), secrets)[:_TOOL_ERROR_MAX], stage=stage, status=status)
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError(f"{what}: the response carries no result", stage=stage, status=status)
    return result


def _text_result(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def error_text(error: Any) -> str:
    """One readable line from a tool error, for ``last_error`` and the card:
    the ``message`` of an error object (Google nests it as
    ``error.message``), the same out of a JSON string holding one, or the
    value as text. Never the repr of a dict."""
    if error is None:
        return ""
    if isinstance(error, str):
        stripped = error.strip()
        if stripped.startswith("{"):
            try:
                return error_text(json.loads(stripped))
            except ValueError:
                return error
        return error
    if isinstance(error, dict):
        inner = error.get("error")
        if isinstance(inner, (dict, str)) and inner:
            return error_text(inner)
        for key in ("message", "detail", "description"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(error)[:_TOOL_ERROR_MAX]
    return str(error)


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
            raise McpError((error_text(payload.get("error")) or "executor reported failure")[:_TOOL_ERROR_MAX],
                           stage="execute")
        raise McpError(f"{EXECUTOR_TOOL} returned no result for {slug}", stage="execute")
    if first.get("error"):
        raise McpError(error_text(first["error"])[:_TOOL_ERROR_MAX], stage="execute")
    response = first.get("response")
    if isinstance(response, dict) and ("successful" in response or "data" in response or "data_preview" in response):
        data = response.get("data")
        truncated = False
        if not data and isinstance(response.get("data_preview"), dict):
            # A large answer: the executor keeps only a preview inline and parks
            # the full payload in a remote file the poller cannot read. The
            # preview is real data (the first items), so it is used and flagged.
            data, truncated = response["data_preview"], True
        out = {"successful": response.get("successful", True), "data": data if data is not None else {},
               "error": response.get("error")}
        if truncated:
            out["truncated"] = True
        return out
    return {"successful": True, "data": response if response is not None else {}, "error": None}


def _check_routable(slug: str, names: list[str]) -> None:
    """``slug`` must be listed, or the executor must be. Decided from the
    names alone, so a one-shot caller that hands them over is refused
    before any request is made."""
    if slug not in names and EXECUTOR_TOOL not in names:
        raise McpError(f"{slug} is not exposed by this session and {EXECUTOR_TOOL} is unavailable",
                       stage="execute")


# ── Sessions ─────────────────────────────────────────────────────────────────


class McpSession:
    """One MCP session over one httpx client. :meth:`open` (or ``async
    with``) creates the client and runs the handshake once; after that
    :meth:`list_tools`, :meth:`call_tool` and :meth:`execute_tool` share the
    ``Mcp-Session-Id`` and take JSON-RPC ids from a counter (``initialize``
    is 1). ``timeout_s`` is the client's read timeout for every request.
    :meth:`close` (``__aexit__``) sends the best-effort DELETE and closes
    the client; calling it twice, or after a failed handshake, is a no-op.
    A session opens once: a second :meth:`open`, on the open session or on
    one that was closed, raises ``stage="session"``. One request at a
    time: not for concurrent use."""

    def __init__(self, entry: dict, *, timeout_s: float = 60.0) -> None:
        self.url, self.headers, self.secrets = _prepare(entry)
        self.timeout_s = timeout_s
        self.session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._last_id = 0
        self._opened = False

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _live(self) -> httpx.AsyncClient:
        if self._client is None:
            raise McpError("MCP session is not open", stage="session")
        return self._client

    async def open(self) -> "McpSession":
        """Create the client and run the handshake. A handshake that fails
        closes the client again (no DELETE: no session id was issued). A
        second open is refused with ``stage="session"``: it would leak or
        replace the live client and reuse this session's ``Mcp-Session-Id``
        and id counter on a new handshake."""
        if self._opened:
            raise McpError("MCP session opens once; this one was opened already", stage="session")
        self._opened = True
        self._client = _client(self.timeout_s)
        try:
            self.session_id = await _handshake(self._client, self.url, self.headers, self.secrets,
                                               self._next_id())
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        """Best-effort DELETE of the session, then the client is closed.
        Exception only on the DELETE: a CancelledError still propagates,
        after the client is closed."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await _close(client, self.url, self.headers, self.session_id)
        finally:
            await client.aclose()

    async def __aenter__(self) -> "McpSession":
        return await self.open()

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def list_tools(self) -> list[str]:
        """The tool names this session exposes (``tools/list``)."""
        client = self._live()
        request_id = self._next_id()
        resp = await _post(client, self.url, self.headers,
                           {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}, "tools/list")
        logger.debug("mcp: tools/list -> HTTP %s (%d bytes)", resp.status_code, len(resp.content))
        tools = _rpc_result(resp, self.secrets, request_id, "tools/list", "tools/list").get("tools")
        if not isinstance(tools, list):
            return []
        return [t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]

    async def call_tool(self, name: str, arguments: dict, *, raise_on_tool_error: bool = True) -> dict:
        """Run one tool and return its ``result`` dict. Raises :class:`McpError`
        on any HTTP, JSON-RPC, transport or tool (``isError``) failure; with
        ``raise_on_tool_error`` false an ``isError`` result is returned as is
        (the executor's answer carries per-tool errors inside its text)."""
        client = self._live()
        request_id = self._next_id()
        resp = await _post(client, self.url, self.headers, {
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, "tools/call")
        logger.debug("mcp: tools/call %s -> HTTP %s (%d bytes)", name, resp.status_code, len(resp.content))
        result = _rpc_result(resp, self.secrets, request_id, f"tools/call {name}", "tools/call")
        if result.get("isError") and raise_on_tool_error:
            raise McpError(_scrub(_joined_text(result) or "tool reported an error", self.secrets)[:_TOOL_ERROR_MAX],
                           stage="tools/call", status=resp.status_code)
        return result

    async def execute_tool(self, slug: str, arguments: dict, *, tool_names: Optional[list[str]] = None) -> dict:
        """Run toolkit tool ``slug`` and return an MCP-shaped result whose text
        content is the toolkit envelope ``{"successful", "data", "error"}``.

        A plain MCP server lists toolkit tools by slug, so they are called
        directly. Composio's tool-router session lists only meta tools and runs
        toolkit tools through :data:`EXECUTOR_TOOL`; its answer is unwrapped so
        every caller sees one shape. ``tool_names`` is the ``tools/list`` answer
        when the caller already has it (one listing per poll, not per collector).
        """
        names = tool_names if tool_names is not None else await self.list_tools()
        _check_routable(slug, names)
        if slug in names:
            return await self.call_tool(slug, arguments)
        wrapper = {"tools": [{"tool_slug": slug, "arguments": arguments}], "sync_response_to_workbench": False}
        result = await self.call_tool(EXECUTOR_TOOL, wrapper, raise_on_tool_error=False)
        return _text_result(_unwrap_executor(tool_result_json(result), slug))


# ── One-shot wrappers ────────────────────────────────────────────────────────


async def call_tool(entry: dict, name: str, arguments: dict, *, timeout_s: float = 60.0,
                    raise_on_tool_error: bool = True) -> dict:
    """One session, one :meth:`McpSession.call_tool`, closed again."""
    async with McpSession(entry, timeout_s=timeout_s) as session:
        return await session.call_tool(name, arguments, raise_on_tool_error=raise_on_tool_error)


async def list_tools(entry: dict, *, timeout_s: float = 30.0) -> list[str]:
    """One session, one :meth:`McpSession.list_tools`, closed again."""
    async with McpSession(entry, timeout_s=timeout_s) as session:
        return await session.list_tools()


async def execute_tool(entry: dict, slug: str, arguments: dict, *, tool_names: Optional[list[str]] = None,
                       timeout_s: float = 60.0) -> dict:
    """One session, one :meth:`McpSession.execute_tool` (the listing, when
    ``tool_names`` is not given, shares that session), closed again. Given
    ``tool_names`` that route nowhere, the error comes before any request."""
    if tool_names is not None:
        _check_routable(slug, tool_names)
    async with McpSession(entry, timeout_s=timeout_s) as session:
        return await session.execute_tool(slug, arguments, tool_names=tool_names)


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
