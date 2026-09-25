"""Grok Bot regressions through the real dispatcher, SSE and session routes.

Only the host HTTP transport is faked. Transcript fixtures use the public
SDK's gateway and legacy JSONL envelopes; no live host transcript is bundled.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from services.cowork_agent.adapters.grokbot import session_seats, sessions
from services.cowork_agent.adapters.grokbot.gateway import GrokbotGatewayError
from services.cowork_agent.adapters.grokbot.oneshot import run_turn
from services.cowork_agent.engine.dispatcher import AgentDispatcher
from services.cowork_agent.engine.sessions_io import find_session_backend, find_session_file
from tests.test_grokbot_adapter import _clear_grokbot_env


def entry(role: str, text: str) -> dict:
    return {"kind": "message", "role": role, "content": text}


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = mock.patch.dict(os.environ, {
            **_clear_grokbot_env(),
            "AGENT_NAME": "grokbot",
            "QUIRQ_STATE_ROOT": str(self.root / "state"),
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
            "SAND_DATA_ROOT": str(self.root / "sand"),
            "SAND_GATEWAY_TOKEN": "test-token",
            "GROKBOT_GATEWAY_URL": "http://127.0.0.1:1340",
        })
        env.start()
        self.addCleanup(env.stop)
        self.entries = {}
        self.calls = []
        self.clients = []
        self.busy = False
        self.auto_reply = True
        self.acceptance_available = True
        self.interrupt_available = True
        self.sent = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.before_send = None
        self.real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(self.handle))
            client = self.real_client(*args, **kwargs)
            self.clients.append(client)
            return client

        patch = mock.patch("httpx.AsyncClient", side_effect=factory)
        patch.start()
        self.addCleanup(patch.stop)

    def handle(self, request):
        name = request.url.path.removeprefix("/api/")
        body = json.loads(request.content)
        self.calls.append((name, body))
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        if name == "createAgent":
            seat = f"seat-{len(self.entries) + 1}"
            self.entries[seat] = []
            return httpx.Response(200, json={"agent": {"id": seat}})
        if name == "sendPrompt":
            if self.before_send:
                self.before_send(body)
            if self.auto_reply:
                self.entries[body["agentId"]].extend([
                    entry("user", body["prompt"]), entry("assistant", "PONG"),
                ])
                self.write_disk(body["agentId"])
            self.sent.set()
            return httpx.Response(200, json={"accepted": True})
        if name == "listAgents":
            return httpx.Response(200, json=[
                {"id": seat, "isRunning": self.busy} for seat in self.entries
            ])
        if name in {"getAsyncTasks", "getSubagents"}:
            return httpx.Response(200, json=[])
        if name == "promptAcceptanceStatus":
            if not self.acceptance_available:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"outcome": "found", "record": {"status": "accepted"}})
        if name == "getAgentTranscript":
            return httpx.Response(200, json=self.entries[body["id"]])
        if name == "interruptAgentRun":
            self.interrupted.set()
            return httpx.Response(200 if self.interrupt_available else 404, json={"hadActiveRun": True})
        if name == "deleteAgent":
            del self.entries[body["id"]]
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected command: {name}")

    def write_disk(self, seat):
        path = self.root / "sand" / "agent-transcripts" / seat / f"{seat}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps({
            "role": row["role"], "message": {"content": row["content"]},
        }) for row in self.entries[seat]), encoding="utf-8")

    async def sse(self, sid, question, is_new):
        from routers.cowork_agent.chat import _dispatcher_sse
        return "".join([event async for event in _dispatcher_sse({
            "agent_name": "grokbot", "question": question,
            "our_session_id": sid, "is_new_session": is_new,
        })])

    async def test_sse_new_chat_and_followup_reopen_under_space_id(self):
        from routers.cowork_agent.sessions import router

        def check_index(body):
            self.assertEqual(find_session_backend("space-uuid"), "grokbot")
            self.assertEqual(session_seats.lookup_seat("space-uuid"), body["agentId"])

        self.before_send = check_index
        first = await self.sse("space-uuid", "ping", True)
        self.assertIn("event: session-created", first)
        self.assertIn('"text": "PONG"', first)
        self.assertIn('"session_id": "space-uuid"', first)
        self.assertNotIn("event: agent-error", first)
        self.assertTrue(sessions.owns_session("space-uuid"))
        self.assertEqual(find_session_file("space-uuid"), sessions.resolve_native_file({}, "space-uuid"))

        # A new dispatcher/HTTP client must recover identity from disk.
        second = await self.sse("space-uuid", "ping again", False)
        self.assertIn('"text": "PONG"', second)
        self.assertNotIn("event: agent-error", second)
        self.assertEqual(sum(name == "createAgent" for name, _ in self.calls), 1)
        self.assertEqual(len(self.clients), 2)  # One pooled client per turn.
        self.assertTrue(all(client.is_closed for client in self.clients))

        app = FastAPI()
        app.include_router(router)
        async with self.real_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rows = (await client.get("/api/sessions")).json()
            self.assertEqual([row["id"] for row in rows], ["space-uuid"])
            self.assertEqual(rows[0]["title"], "ping")
            messages = (await client.get("/api/messages/space-uuid")).json()
        self.assertEqual(messages["total"], 4)
        self.assertTrue(all(row["session_id"] == "space-uuid" for row in messages["messages"]))

    async def test_missing_token_reaches_sse_as_agent_error(self):
        with mock.patch.dict(os.environ, {"SAND_GATEWAY_TOKEN": ""}):
            output = await self.sse("missing-token", "ping", True)
        self.assertIn("event: agent-error", output)
        self.assertIn("SAND_GATEWAY_TOKEN", output)
        self.assertIn("event: done", output)
        self.assertEqual(self.calls, [])

    async def test_index_survives_without_mounted_transcripts(self):
        from services.cowork_agent.engine.sessions_io import load_all_sessions
        session_seats.remember_seat("unmounted", "seat-1")
        self.assertEqual(find_session_backend("unmounted"), "grokbot")
        self.assertTrue(sessions.owns_session("unmounted"))
        self.assertEqual([row["id"] for row in load_all_sessions()], ["unmounted"])
        self.assertEqual(sessions.get_messages("unmounted"), [])

    async def test_timeout_never_returns_previous_answer_even_with_a_reply_present(self):
        await AgentDispatcher("grokbot").ask("first", "space-uuid", is_new_session=True)
        self.busy = True
        with self.assertRaisesRegex(GrokbotGatewayError, "did not finish"):
            await run_turn("second", "space-uuid", timeout_s=0.01)
        self.assertTrue(self.interrupted.is_set())
        self.assertIn("seat-1", self.entries)  # Retain history for resuming.
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_idle_with_missing_acceptance_does_not_return_old_reply(self):
        await run_turn("first", "space-uuid", is_new_session=True)
        self.auto_reply = False
        self.acceptance_available = False
        with self.assertRaisesRegex(GrokbotGatewayError, "did not finish"):
            await run_turn("second", "space-uuid", timeout_s=0.01)
        self.assertTrue(self.interrupted.is_set())

    async def test_idle_waits_for_prompt_and_fresh_reply_with_poll_backoff(self):
        await run_turn("first", "space-uuid", is_new_session=True)
        self.auto_reply = False
        self.acceptance_available = False
        delays = []

        async def advance(delay):
            delays.append(delay)
            if len(delays) == 1:
                self.entries["seat-1"].append(entry("user", "second"))
            else:
                # Identical answer text is fine when it is a NEW entry.
                self.entries["seat-1"].append(entry("assistant", "PONG"))

        with mock.patch("services.cowork_agent.adapters.grokbot.oneshot.asyncio.sleep", side_effect=advance):
            result = await run_turn("second", "space-uuid")
        self.assertEqual(result["message"], "PONG")
        self.assertEqual(delays, [1.0, 1.5])

    async def test_cancellation_attempts_host_interrupt_and_closes_client(self):
        self.busy = True
        self.interrupt_available = False
        task = asyncio.create_task(run_turn("ping", "space-uuid", is_new_session=True))
        await self.sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.interrupted.is_set())
        self.assertEqual(session_seats.lookup_seat("space-uuid"), "seat-1")
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_standalone_one_shot_seat_is_deleted(self):
        result = await run_turn("ping")
        self.assertEqual(result["message"], "PONG")
        self.assertEqual(self.entries, {})
        self.assertIn(("deleteAgent", {"id": "seat-1"}), self.calls)

    async def test_standalone_one_shot_seat_is_deleted_on_timeout(self):
        self.busy = True
        with self.assertRaisesRegex(GrokbotGatewayError, "did not finish"):
            await run_turn("ping", timeout_s=0.01)
        self.assertTrue(self.interrupted.is_set())
        self.assertEqual(self.entries, {})

    async def test_cancelling_sse_consumer_interrupts_producer(self):
        from routers.cowork_agent.chat import _dispatcher_sse
        self.busy = True
        stream = _dispatcher_sse({
            "agent_name": "grokbot", "question": "ping",
            "our_session_id": "space-uuid", "is_new_session": True,
        })
        self.assertIn("session-created", await anext(stream))
        self.assertIn("model-loading", await anext(stream))
        reader = asyncio.create_task(anext(stream))
        await self.sent.wait()
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        await asyncio.wait_for(self.interrupted.wait(), timeout=1)

    async def test_distinct_new_chats_get_distinct_retained_seats(self):
        one, two = await asyncio.gather(
            run_turn("first", "space-one", is_new_session=True),
            run_turn("second", "space-two", is_new_session=True),
        )
        self.assertNotEqual(one["native_session_id"], two["native_session_id"])
        self.assertEqual(session_seats.lookup_seat("space-one"), one["native_session_id"])
        self.assertEqual(session_seats.lookup_seat("space-two"), two["native_session_id"])

    async def test_concurrent_chats_keep_separate_index_shards(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: session_seats.remember_seat(f"space-{n}", f"seat-{n}"), range(32)))
        self.assertEqual(len(session_seats.indexed_sessions()), 32)
        for n in range(32):
            self.assertEqual(session_seats.lookup_seat(f"space-{n}"), f"seat-{n}")

    async def test_legacy_mapping_is_migrated_on_resume(self):
        path = self.root / "state" / "grokbot" / "session-seats.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"seats": {"legacy": "seat-1"}}))
        self.entries["seat-1"] = []
        result = await run_turn("ping", "legacy")
        self.assertEqual(result["native_session_id"], "seat-1")
        self.assertEqual(find_session_backend("legacy"), "grokbot")
        self.assertNotIn("createAgent", [name for name, _ in self.calls])


class TranscriptTests(unittest.TestCase):
    def test_reply_fence_allows_user_envelope_tool_results(self):
        from services.cowork_agent.adapters.grokbot.oneshot import _current_turn_entries, last_assistant_text
        entries = [
            {"role": "user", "message": {"content": [{"type": "text", "text": "ping"}]}},
            {"role": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"entry": entry("assistant", "PONG")},
        ]
        self.assertEqual(last_assistant_text(_current_turn_entries([], entries, "ping")), "PONG")

    def test_user_tool_results_match_call_ids_even_out_of_order(self):
        records = [
            {"role": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "one", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "two", "name": "Read", "input": {}},
            ]}},
            {"role": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "unknown", "content": "ignore"},
                {"type": "tool_result", "tool_use_id": "two", "content": "second"},
                {"type": "tool_result", "tool_use_id": "one", "content": "first", "is_error": True},
            ]}},
        ]
        messages = sessions._convert_messages("space-uuid", records)
        self.assertEqual(len(messages), 1)
        states = [part["data"]["state"] for part in messages[0]["parts"]]
        self.assertEqual([state["output"] for state in states], ["first", "second"])
        self.assertEqual(states[0]["status"], "error")

    def test_reply_fence_rejects_rewritten_history_and_unrelated_answers(self):
        from services.cowork_agent.adapters.grokbot.oneshot import _current_turn_entries, last_assistant_text
        before = [entry("user", "first"), entry("assistant", "old")]
        with self.assertRaises(GrokbotGatewayError):
            _current_turn_entries(before, [entry("assistant", "unrelated")], "second")
        self.assertIsNone(_current_turn_entries(before, before + [entry("assistant", "late old reply")], "second"))
        current = _current_turn_entries(before, before + [
            entry("user", "second"), entry("user", "someone else's prompt"), entry("assistant", "unrelated"),
        ], "second")
        self.assertIsNone(last_assistant_text(current))


if __name__ == "__main__":
    unittest.main()
