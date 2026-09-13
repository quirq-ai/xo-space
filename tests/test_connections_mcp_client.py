"""The streamable-HTTP JSON-RPC client behind the connections poller.

Every test routes the AsyncClient through an ``httpx.MockTransport`` (via
``mcp_client._TRANSPORT``), so no socket is ever opened. The scripted
upstream records every request so the session header, the request shapes
and the closing DELETE can be asserted."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Optional
from unittest.mock import patch

import httpx

from services.connections import mcp_client
from services.connections.mcp_client import McpError

TOKEN = "sk-composio-secret-token-1234567890"
API_KEY = "apikey-abcdef1234567890"
URL = "https://mcp.example.test/mcp"
ENTRY = {"type": "http", "url": URL, "headers": {"Authorization": f"Bearer {TOKEN}", "x-api-key": API_KEY}}
RESULT = {"content": [{"type": "text", "text": json.dumps({"successful": True, "data": {"messages": [{"id": "m1"}]}})}]}
CALL_OK = {"jsonrpc": "2.0", "id": 2, "result": RESULT}
INIT_OK = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Upstream:
    """A scripted MCP server. ``call`` is the tools/call answer: a JSON body,
    or ``("sse", text)`` for an event stream. ``delete_exc`` is raised on the
    closing DELETE."""

    def __init__(self, *, session_id: Optional[str] = "sess-1", call=CALL_OK, call_status: int = 200,
                 init_status: int = 200, init_body=INIT_OK, notify_status: int = 202, delete_exc=None,
                 init_exc=None) -> None:
        self.session_id, self.call, self.call_status = session_id, call, call_status
        self.init_status, self.init_body, self.notify_status = init_status, init_body, notify_status
        self.delete_exc, self.init_exc = delete_exc, init_exc
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            if self.delete_exc is not None:
                raise self.delete_exc
            return httpx.Response(204)
        message = json.loads(request.content) if request.content else {}
        method = message.get("method")
        if method == "initialize":
            if self.init_exc is not None:
                raise self.init_exc
            headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            body = self.init_body
            if isinstance(body, str):
                return httpx.Response(self.init_status, content=body.encode(), headers=headers)
            return httpx.Response(self.init_status, json=body, headers=headers)
        if method == "notifications/initialized":
            return httpx.Response(self.notify_status)
        if method == "tools/call":
            if isinstance(self.call, tuple) and self.call[0] == "sse":
                return httpx.Response(self.call_status, content=self.call[1].encode("utf-8"),
                                      headers={"Content-Type": "text/event-stream; charset=utf-8"})
            if isinstance(self.call, str):
                return httpx.Response(self.call_status, content=self.call.encode("utf-8"),
                                      headers={"Content-Type": "application/json"})
            return httpx.Response(self.call_status, json=self.call)
        return httpx.Response(400, json={"error": "unexpected"})

    def bodies(self) -> list:
        return [json.loads(r.content) if r.content else None for r in self.requests]


class _Base(unittest.TestCase):
    def use(self, upstream: Upstream) -> Upstream:
        self._transport = patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(upstream.handler))
        self._transport.start()
        self.addCleanup(self._transport.stop)
        return upstream

    def call(self, name: str = "GMAIL_FETCH_EMAILS", arguments: Optional[dict] = None, **kw) -> dict:
        return run(mcp_client.call_tool(ENTRY, name, arguments if arguments is not None else {"q": 1}, **kw))


class CallToolTests(_Base):
    def test_happy_path_json_with_session_header_echoed_everywhere(self) -> None:
        up = self.use(Upstream())
        result = self.call(arguments={"query": "is:unread", "max_results": 20})
        self.assertEqual(result, RESULT)
        self.assertEqual([r.method for r in up.requests], ["POST", "POST", "POST", "DELETE"])
        init, notify, call, delete = up.requests
        self.assertEqual(json.loads(init.content), {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "xo-space-connections", "version": "1"}},
        })
        self.assertEqual(init.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(init.headers["x-api-key"], API_KEY)
        self.assertEqual(init.headers["Content-Type"], "application/json")
        self.assertIn("text/event-stream", init.headers["Accept"])
        self.assertIn("application/json", init.headers["Accept"])
        self.assertNotIn("mcp-session-id", init.headers)
        self.assertEqual(json.loads(notify.content), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(json.loads(call.content), {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "GMAIL_FETCH_EMAILS", "arguments": {"query": "is:unread", "max_results": 20}},
        })
        for req in (notify, call, delete):
            self.assertEqual(req.headers["mcp-session-id"], "sess-1")
            self.assertEqual(req.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(str(delete.url), URL)

    def test_without_a_session_id_there_is_no_header_and_no_delete(self) -> None:
        up = self.use(Upstream(session_id=None))
        self.assertEqual(self.call(), RESULT)
        self.assertEqual([r.method for r in up.requests], ["POST", "POST", "POST"])
        self.assertTrue(all("mcp-session-id" not in r.headers for r in up.requests))

    def test_notification_status_and_sse_initialize_are_tolerated(self) -> None:
        sse_init = "event: message\r\ndata: " + json.dumps(INIT_OK) + "\r\n\r\n"
        for notify_status in (200, 202, 204, 405):
            with self.subTest(notify_status=notify_status):
                up = Upstream(notify_status=notify_status, init_body=sse_init)
                with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler)):
                    self.assertEqual(self.call(), RESULT)

    def test_sse_response_with_notifications_comments_and_multi_line_data(self) -> None:
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
        pretty = json.dumps(CALL_OK, indent=2)
        stream = (
            ": keep-alive comment\r\n\r\n"
            "event: message\r\nid: 7\r\ndata: " + json.dumps(progress) + "\r\n\r\n"
            "data: not json at all\r\n\r\n"
            "event: message\r\n" + "".join("data: " + line + "\r\n" for line in pretty.split("\n")) + "\r\n"
            "data:{\"jsonrpc\":\"2.0\",\"id\":99,\"result\":{\"content\":[]}}\n\n"
        )
        self.use(Upstream(call=("sse", stream)))
        self.assertEqual(self.call(), RESULT)

    def test_sse_picks_id_two_as_a_string_and_falls_back_to_the_last_message(self) -> None:
        as_string = dict(CALL_OK, id="2")
        stream = "data: " + json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"content": []}}) + "\n\n" \
                 "data: " + json.dumps(as_string) + "\n\n"
        self.use(Upstream(call=("sse", stream)))
        self.assertEqual(self.call(), RESULT)
        other = {"jsonrpc": "2.0", "id": 9, "result": {"content": [{"type": "text", "text": "last"}]}}
        stream = "data: junk\n\ndata: " + json.dumps(other) + "\n\n"
        with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(Upstream(call=("sse", stream)).handler)):
            self.assertEqual(self.call(), other["result"])

    def test_batch_list_response_picks_id_two(self) -> None:
        batch = [{"jsonrpc": "2.0", "id": 1, "result": {}}, CALL_OK, {"jsonrpc": "2.0", "id": 3, "error": {"message": "x"}}]
        self.use(Upstream(call=batch))
        self.assertEqual(self.call(), RESULT)

    def test_json_rpc_error(self) -> None:
        self.use(Upstream(call={"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "bad arguments"}}))
        with self.assertRaises(McpError) as ctx:
            self.call()
        self.assertEqual(str(ctx.exception), "bad arguments")
        up = Upstream(call={"jsonrpc": "2.0", "id": 2, "error": {"code": -32000}})
        with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler)):
            with self.assertRaises(McpError) as ctx:
                self.call()
        self.assertIn("-32000", str(ctx.exception))
        self.assertEqual(up.requests[-1].method, "DELETE", "the session is closed after an error too")

    def test_http_500_carries_status_and_a_scrubbed_snippet(self) -> None:
        echo = "upstream exploded; auth=" + f"Bearer {TOKEN}" + " key=" + API_KEY + " " + "z" * 500
        up = self.use(Upstream(call=echo, call_status=500))
        with self.assertRaises(McpError) as ctx:
            self.call()
        text = str(ctx.exception)
        self.assertIn("HTTP 500", text)
        self.assertIn("upstream exploded", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(API_KEY, text)
        self.assertLess(len(text), 300)
        self.assertEqual(up.requests[-1].method, "DELETE")

    def test_http_400_with_an_sse_body(self) -> None:
        self.use(Upstream(call=("sse", "data: {\"error\": \"nope\"}\n\n"), call_status=400))
        with self.assertRaises(McpError) as ctx:
            self.call()
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_is_error_result_raises_with_the_joined_text(self) -> None:
        result = {"isError": True, "content": [{"type": "text", "text": "first"}, {"type": "image"},
                                               {"type": "text", "text": "second " + "x" * 600}]}
        self.use(Upstream(call={"jsonrpc": "2.0", "id": 2, "result": result}))
        with self.assertRaises(McpError) as ctx:
            self.call()
        self.assertTrue(str(ctx.exception).startswith("first\nsecond "))
        self.assertLessEqual(len(str(ctx.exception)), 500)
        with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(
                Upstream(call={"jsonrpc": "2.0", "id": 2, "result": {"isError": True}}).handler)):
            with self.assertRaises(McpError) as ctx:
                self.call()
        self.assertEqual(str(ctx.exception), "tool reported an error")

    def test_missing_message_or_result(self) -> None:
        for body, fragment in (("", "no JSON-RPC message"), ("not json", "no JSON-RPC message"),
                               (json.dumps({"jsonrpc": "2.0", "id": 2}), "carries no result"),
                               (json.dumps({"jsonrpc": "2.0", "id": 2, "result": "text"}), "carries no result")):
            with self.subTest(body=body):
                with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(Upstream(call=body).handler)):
                    with self.assertRaises(McpError) as ctx:
                        self.call()
                self.assertIn(fragment, str(ctx.exception))

    def test_initialize_must_be_2xx(self) -> None:
        for status in (302, 401, 500):
            with self.subTest(status=status):
                up = Upstream(init_status=status, init_body="denied " + TOKEN)
                with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler)):
                    with self.assertRaises(McpError) as ctx:
                        self.call()
                self.assertTrue(str(ctx.exception).startswith(f"initialize failed: HTTP {status}"))
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertEqual(len(up.requests), 1, "nothing after a failed initialize, no DELETE either")

    def test_transport_errors_become_mcp_errors_without_the_url(self) -> None:
        up = self.use(Upstream(init_exc=httpx.ConnectError("boom https://mcp.example.test/mcp?token=" + TOKEN)))
        with self.assertRaises(McpError) as ctx:
            self.call()
        self.assertIn("ConnectError", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("example.test", str(ctx.exception))
        self.assertEqual(len(up.requests), 1)

    def test_delete_failure_is_swallowed(self) -> None:
        up = self.use(Upstream(delete_exc=httpx.ConnectError("closed")))
        self.assertEqual(self.call(), RESULT)
        self.assertEqual(up.requests[-1].method, "DELETE")

    def test_entry_without_a_url(self) -> None:
        for entry in ({}, {"url": ""}, {"url": None}, None):
            with self.subTest(entry=entry):
                with self.assertRaises(McpError):
                    run(mcp_client.call_tool(entry, "X", {}))

    def test_logs_never_carry_headers_or_bodies(self) -> None:
        up = self.use(Upstream(call="secret body " + TOKEN, call_status=500))
        with self.assertLogs("services.connections.mcp_client", level="DEBUG") as logs:
            with self.assertRaises(McpError):
                self.call()
        joined = "\n".join(logs.output)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn(API_KEY, joined)
        self.assertNotIn("secret body", joined)
        self.assertIn("GMAIL_FETCH_EMAILS", joined)
        self.assertEqual(up.requests[-1].method, "DELETE")

    def test_cancellation_propagates_and_still_closes_the_session(self) -> None:
        up = Upstream()
        sync_handler = up.handler

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
                await asyncio.sleep(30)
            return sync_handler(request)

        loop = asyncio.new_event_loop()
        try:
            with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(handler)):
                task = loop.create_task(mcp_client.call_tool(ENTRY, "X", {}))
                loop.call_later(0.05, task.cancel)
                with self.assertRaises(asyncio.CancelledError):
                    loop.run_until_complete(task)
        finally:
            loop.close()
        self.assertEqual([r.method for r in up.requests], ["POST", "POST", "DELETE"])


class ToolResultJsonTests(unittest.TestCase):
    def test_first_parsable_text_wins(self) -> None:
        result = {"content": [{"type": "text", "text": "not json"}, {"type": "image", "data": "x"},
                              {"type": "text", "text": "{\"a\": 1}"}, {"type": "text", "text": "[2]"}]}
        self.assertEqual(mcp_client.tool_result_json(result), {"a": 1})

    def test_no_parsable_text_returns_the_joined_text(self) -> None:
        result = {"content": [{"type": "text", "text": "one"}, {"text": "no type"}, {"type": "text", "text": 5},
                              {"type": "text", "text": "two"}, "junk"]}
        self.assertEqual(mcp_client.tool_result_json(result), {"text": "one\ntwo"})

    def test_structured_content_and_empty(self) -> None:
        self.assertEqual(mcp_client.tool_result_json({"structuredContent": {"k": 1}}), {"k": 1})
        self.assertEqual(mcp_client.tool_result_json({"content": [], "structuredContent": {"k": 1}}), {"k": 1})
        self.assertEqual(mcp_client.tool_result_json({}), {})
        self.assertEqual(mcp_client.tool_result_json({"content": "junk"}), {})
        self.assertEqual(mcp_client.tool_result_json(None), {})
        self.assertEqual(mcp_client.tool_result_json({"content": [{"type": "text", "text": "\"s\""}]}), "s")


class ErrorTextTests(unittest.TestCase):
    def test_shapes(self) -> None:
        et = mcp_client.error_text
        self.assertEqual(et(None), "")
        self.assertEqual(et("plain text"), "plain text")
        self.assertEqual(et({"message": "boom"}), "boom")
        self.assertEqual(et({"error": {"code": 403, "message": "Quota exceeded"}}), "Quota exceeded")
        self.assertEqual(et('{"error": {"message": "nested in a string"}}'), "nested in a string")
        self.assertEqual(et("{not json"), "{not json")
        self.assertEqual(et({"code": 500}), '{"code": 500}')
        self.assertEqual(et(42), "42")


if __name__ == "__main__":
    unittest.main()


class RouterUpstream(Upstream):
    """An :class:`Upstream` that also answers ``tools/list`` with ``tools``,
    the way Composio's tool-router session lists only its meta tools."""

    def __init__(self, *, tools: list[str], **kw) -> None:
        super().__init__(**kw)
        self.tools = tools

    def handler(self, request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content) if request.content else {}
        if message.get("method") == "tools/list":
            self.requests.append(request)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"),
                                             "result": {"tools": [{"name": n, "inputSchema": {}} for n in self.tools]}})
        return super().handler(request)


def router_call(results: list[dict], *, is_error: bool = False) -> dict:
    """A ``tools/call`` answer from COMPOSIO_MULTI_EXECUTE_TOOL."""
    payload = {"data": {"results": results, "total_count": len(results),
                        "success_count": sum(1 for r in results if "error" not in r),
                        "error_count": sum(1 for r in results if "error" in r)},
               "successful": not is_error, "error": None}
    return {"jsonrpc": "2.0", "id": 2,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}}


class ExecuteToolTests(_Base):
    EXECUTOR = mcp_client.EXECUTOR_TOOL

    def _calls(self, up: Upstream, method: str) -> list[dict]:
        return [b for b in up.bodies() if isinstance(b, dict) and b.get("method") == method]

    def test_list_tools_returns_the_names_and_closes_the_session(self) -> None:
        up = self.use(RouterUpstream(tools=[self.EXECUTOR, "COMPOSIO_SEARCH_TOOLS"]))
        self.assertEqual(run(mcp_client.list_tools(ENTRY)), [self.EXECUTOR, "COMPOSIO_SEARCH_TOOLS"])
        self.assertEqual(len(self._calls(up, "tools/list")), 1)
        self.assertEqual(up.requests[-1].method, "DELETE")
        self.assertEqual(up.requests[-1].headers.get("mcp-session-id"), "sess-1")

    def test_direct_call_when_the_slug_is_listed(self) -> None:
        up = self.use(RouterUpstream(tools=["GMAIL_FETCH_EMAILS"]))
        result = run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {"q": 1}))
        self.assertEqual(result["content"], CALL_OK["result"]["content"])
        call = self._calls(up, "tools/call")[0]["params"]
        self.assertEqual(call["name"], "GMAIL_FETCH_EMAILS")
        self.assertEqual(call["arguments"], {"q": 1})

    def test_executor_path_unwraps_the_router_envelope(self) -> None:
        answer = router_call([{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                               "response": {"successful": True, "data": {"messages": [{"messageId": "m1"}]}}}])
        up = self.use(RouterUpstream(tools=[self.EXECUTOR], call=answer))
        result = run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {"query": "is:unread"}))
        self.assertEqual(mcp_client.tool_result_json(result),
                         {"successful": True, "data": {"messages": [{"messageId": "m1"}]}, "error": None})
        call = self._calls(up, "tools/call")[0]["params"]
        self.assertEqual(call["name"], self.EXECUTOR)
        self.assertEqual(call["arguments"]["tools"],
                         [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {"query": "is:unread"}}])
        self.assertIs(call["arguments"]["sync_response_to_workbench"], False)

    def test_executor_per_tool_error_raises_with_that_message(self) -> None:
        answer = router_call([{"tool_slug": "NOTION_SEARCH_NOTION_PAGE", "index": 0,
                               "error": "[Session Restriction] Toolkit 'notion' is not allowed for this session."}],
                             is_error=True)
        self.use(RouterUpstream(tools=[self.EXECUTOR], call=answer))
        with self.assertRaises(McpError) as raised:
            run(mcp_client.execute_tool(ENTRY, "NOTION_SEARCH_NOTION_PAGE", {}, tool_names=[self.EXECUTOR]))
        self.assertIn("Session Restriction", str(raised.exception))

    def test_executor_per_tool_error_object_reads_as_its_message(self) -> None:
        """Google answers through Composio as ``{"error": {"code": 403,
        "message": ...}}``; the message, not the dict's repr, is the error."""
        answer = router_call([{"tool_slug": "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", "index": 0,
                               "error": {"error": {"code": 403, "message": "Quota exceeded for quota metric 'Queries'"}}}],
                             is_error=True)
        self.use(RouterUpstream(tools=[self.EXECUTOR], call=answer))
        with self.assertRaises(McpError) as raised:
            run(mcp_client.execute_tool(ENTRY, "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {}, tool_names=[self.EXECUTOR]))
        self.assertEqual(str(raised.exception), "Quota exceeded for quota metric 'Queries'")

    def test_executor_without_a_result_or_with_a_failed_response(self) -> None:
        empty = router_call([])
        self.use(RouterUpstream(tools=[self.EXECUTOR], call=empty))
        with self.assertRaises(McpError):
            run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {}, tool_names=[self.EXECUTOR]))
        failed = router_call([{"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0,
                               "response": {"successful": False, "data": {}, "error": "scope missing"}}])
        with patch.object(mcp_client, "_TRANSPORT",
                          httpx.MockTransport(RouterUpstream(tools=[self.EXECUTOR], call=failed).handler)):
            result = run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {}, tool_names=[self.EXECUTOR]))
        self.assertEqual(mcp_client.tool_result_json(result)["successful"], False)

    def test_neither_direct_nor_executor_is_an_error_before_any_call(self) -> None:
        with self.assertRaises(McpError) as raised:
            run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {}, tool_names=["COMPOSIO_SEARCH_TOOLS"]))
        self.assertIn("GMAIL_FETCH_EMAILS", str(raised.exception))

    def test_given_tool_names_skip_the_listing(self) -> None:
        up = self.use(RouterUpstream(tools=[]))
        run(mcp_client.execute_tool(ENTRY, "GMAIL_FETCH_EMAILS", {}, tool_names=["GMAIL_FETCH_EMAILS"]))
        self.assertEqual(self._calls(up, "tools/list"), [])

    def test_raise_on_tool_error_false_hands_back_the_result(self) -> None:
        answer = {"jsonrpc": "2.0", "id": 2,
                  "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}}
        self.use(Upstream(call=answer))
        result = run(mcp_client.call_tool(ENTRY, "X", {}, raise_on_tool_error=False))
        self.assertTrue(result["isError"])


class EchoUpstream(RouterUpstream):
    """A :class:`RouterUpstream` whose ``tools/call`` answer carries the
    request's own id, the way a real server does, so a call later in a
    session (id 3, 4, ...) is matched by id rather than by the fallback."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content) if request.content else {}
        if message.get("method") == "tools/call":
            self.requests.append(request)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"), "result": RESULT})
        return super().handler(request)


def methods(up: Upstream) -> list[str]:
    return [r.method if r.method == "DELETE" else json.loads(r.content).get("method") for r in up.requests]


class McpSessionTests(_Base):
    """One handshake, many requests: the session keeps the client and the
    session id, and every request carries its own JSON-RPC id."""

    def test_one_handshake_serves_a_listing_and_many_calls(self) -> None:
        up = self.use(EchoUpstream(tools=["A", "B"]))

        async def scenario():
            async with mcp_client.McpSession(ENTRY, timeout_s=5.0) as session:
                names = await session.list_tools()
                first = await session.call_tool("A", {"n": 1})
                second = await session.call_tool("B", {"n": 2})
                third = await session.execute_tool("A", {"n": 3}, tool_names=names)
            return names, first, second, third

        names, first, second, third = run(scenario())
        self.assertEqual(names, ["A", "B"])
        self.assertEqual((first, second, third), (RESULT, RESULT, RESULT))
        self.assertEqual(methods(up), ["initialize", "notifications/initialized", "tools/list",
                                       "tools/call", "tools/call", "tools/call", "DELETE"])
        ids = [b.get("id") for b in up.bodies() if isinstance(b, dict) and "id" in b]
        self.assertEqual(ids, [1, 2, 3, 4, 5], "distinct ids, counted up within the session")
        self.assertNotIn("mcp-session-id", up.requests[0].headers)
        for req in up.requests[1:]:
            self.assertEqual(req.headers["mcp-session-id"], "sess-1")
            self.assertEqual(req.headers["Authorization"], f"Bearer {TOKEN}")
        calls = [b["params"]["arguments"] for b in up.bodies() if isinstance(b, dict) and b.get("method") == "tools/call"]
        self.assertEqual(calls, [{"n": 1}, {"n": 2}, {"n": 3}])

    def test_execute_tool_lists_on_the_same_session_when_names_are_not_given(self) -> None:
        up = self.use(EchoUpstream(tools=["A"]))

        async def scenario():
            async with mcp_client.McpSession(ENTRY) as session:
                return await session.execute_tool("A", {})

        self.assertEqual(run(scenario()), RESULT)
        self.assertEqual(methods(up), ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"])

    def test_close_is_idempotent_and_a_closed_or_unopened_session_refuses_requests(self) -> None:
        up = self.use(Upstream())

        async def scenario():
            session = mcp_client.McpSession(ENTRY)
            with self.assertRaises(McpError) as before:
                await session.list_tools()
            await session.open()
            await session.close()
            await session.close()
            with self.assertRaises(McpError) as after:
                await session.call_tool("X", {})
            return before.exception, after.exception

        before, after = run(scenario())
        self.assertEqual((before.stage, before.status, after.stage, after.status), ("session", None, "session", None))
        self.assertEqual(methods(up), ["initialize", "notifications/initialized", "DELETE"], "one DELETE")

    def test_failed_handshake_closes_the_client_and_sends_no_delete(self) -> None:
        up = self.use(Upstream(init_status=500, init_body="down"))
        session = mcp_client.McpSession(ENTRY)
        with self.assertRaises(McpError) as ctx:
            run(session.open())
        self.assertEqual((ctx.exception.stage, ctx.exception.status), ("initialize", 500))
        run(session.close())
        with self.assertRaises(McpError) as ctx:
            run(session.call_tool("X", {}))
        self.assertEqual(ctx.exception.stage, "session", "the client is gone after a failed handshake")
        self.assertEqual(methods(up), ["initialize"])

    def test_the_one_shot_wrappers_open_and_close_a_session_each(self) -> None:
        up = self.use(EchoUpstream(tools=["A"]))
        run(mcp_client.list_tools(ENTRY))
        run(mcp_client.call_tool(ENTRY, "A", {}))
        run(mcp_client.execute_tool(ENTRY, "A", {}, tool_names=["A"]))
        self.assertEqual(methods(up), ["initialize", "notifications/initialized", "tools/list", "DELETE",
                                       "initialize", "notifications/initialized", "tools/call", "DELETE",
                                       "initialize", "notifications/initialized", "tools/call", "DELETE"])

    def test_a_session_opens_once(self) -> None:
        """A second open, on the open session or after close, is refused with
        stage ``session``: the live client is neither leaked nor replaced, no
        second handshake goes out, and the session keeps working."""
        up = self.use(EchoUpstream(tools=["A"]))

        async def scenario():
            session = mcp_client.McpSession(ENTRY)
            await session.open()
            client = session._client
            with self.assertRaises(McpError) as again:
                await session.open()
            self.assertIs(session._client, client, "the live client is kept")
            result = await session.call_tool("A", {})
            await session.close()
            with self.assertRaises(McpError) as after_close:
                await session.open()
            return again.exception, result, after_close.exception

        again, result, after_close = run(scenario())
        self.assertEqual((again.stage, again.status), ("session", None))
        self.assertEqual((after_close.stage, after_close.status), ("session", None))
        self.assertEqual(result, RESULT)
        self.assertEqual(methods(up), ["initialize", "notifications/initialized", "tools/call", "DELETE"],
                         "one handshake, one DELETE")


class McpErrorStageTests(_Base):
    """Every raise site stamps ``stage`` and ``status``; the poller reads
    those (a dead Composio session is ``initialize`` + 404), never the text."""

    def _raised(self, coro) -> McpError:
        with self.assertRaises(McpError) as ctx:
            run(coro)
        return ctx.exception

    def test_initialize_carries_its_http_status(self) -> None:
        for status in (302, 404, 500):
            with self.subTest(status=status):
                up = Upstream(init_status=status, init_body="gone")
                with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler)):
                    exc = self._raised(mcp_client.call_tool(ENTRY, "X", {}))
                self.assertEqual((exc.stage, exc.status), ("initialize", status))
                self.assertTrue(str(exc).startswith(f"initialize failed: HTTP {status}"))

    def test_transport_failures_carry_the_stage_and_no_status(self) -> None:
        self.use(Upstream(init_exc=httpx.ConnectError("boom")))
        exc = self._raised(mcp_client.call_tool(ENTRY, "X", {}))
        self.assertEqual((exc.stage, exc.status), ("initialize", None))
        plain = Upstream()

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content).get("method") == "notifications/initialized":
                raise httpx.ReadTimeout("slow")
            return plain.handler(request)

        with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(handler)):
            exc = self._raised(mcp_client.call_tool(ENTRY, "X", {}))
        self.assertEqual((exc.stage, exc.status), ("notifications/initialized", None))
        self.assertEqual(str(exc), "notifications/initialized: ReadTimeout while calling the MCP upstream")

    def test_tools_call_http_json_rpc_and_tool_errors(self) -> None:
        cases = [
            (Upstream(call="down", call_status=502), 502),
            (Upstream(call={"jsonrpc": "2.0", "id": 2, "error": {"message": "bad"}}), 200),
            (Upstream(call=""), 200),
            (Upstream(call={"jsonrpc": "2.0", "id": 2, "result": "text"}), 200),
            (Upstream(call={"jsonrpc": "2.0", "id": 2, "result": {"isError": True}}), 200),
        ]
        for up, status in cases:
            with self.subTest(body=up.call, status=status):
                with patch.object(mcp_client, "_TRANSPORT", httpx.MockTransport(up.handler)):
                    exc = self._raised(mcp_client.call_tool(ENTRY, "X", {}))
                self.assertEqual((exc.stage, exc.status), ("tools/call", status))

    def test_tools_list_failure(self) -> None:
        class Down(RouterUpstream):
            def handler(self, request: httpx.Request) -> httpx.Response:
                message = json.loads(request.content) if request.content else {}
                if message.get("method") == "tools/list":
                    self.requests.append(request)
                    return httpx.Response(503, content=b"busy")
                return super().handler(request)

        self.use(Down(tools=[]))
        exc = self._raised(mcp_client.list_tools(ENTRY))
        self.assertEqual((exc.stage, exc.status), ("tools/list", 503))
        self.assertTrue(str(exc).startswith("tools/list failed: HTTP 503"))

    def test_entry_and_executor_decisions(self) -> None:
        exc = self._raised(mcp_client.call_tool({}, "X", {}))
        self.assertEqual((exc.stage, exc.status, str(exc)), ("entry", None, "MCP entry has no url"))
        exc = self._raised(mcp_client.execute_tool(ENTRY, "X", {}, tool_names=["OTHER"]))
        self.assertEqual((exc.stage, exc.status), ("execute", None))
        answer = router_call([{"tool_slug": "X", "index": 0, "error": "restricted"}], is_error=True)
        self.use(RouterUpstream(tools=[mcp_client.EXECUTOR_TOOL], call=answer))
        exc = self._raised(mcp_client.execute_tool(ENTRY, "X", {}, tool_names=[mcp_client.EXECUTOR_TOOL]))
        self.assertEqual((exc.stage, exc.status, str(exc)), ("execute", None, "restricted"))
        empty = router_call([])
        with patch.object(mcp_client, "_TRANSPORT",
                          httpx.MockTransport(RouterUpstream(tools=[mcp_client.EXECUTOR_TOOL], call=empty).handler)):
            exc = self._raised(mcp_client.execute_tool(ENTRY, "X", {}, tool_names=[mcp_client.EXECUTOR_TOOL]))
        self.assertEqual((exc.stage, exc.status), ("execute", None))

    def test_a_plain_error_has_neither(self) -> None:
        exc = McpError("just text")
        self.assertEqual((exc.stage, exc.status, str(exc)), (None, None, "just text"))
        self.assertIsInstance(exc, RuntimeError)
