"""Grok Bot regressions through the real dispatcher, SSE and session routes.

Only the host HTTP transport is faked. Transcript fixtures follow the
reviewer's live-host nonce/requestId and send-message observations.
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


def entry(role: str, text: str, *, nonce="nonce", request_id="request", seq=1) -> dict:
    common = {"requestId": request_id, "seq": seq, "timestampMs": 1_700_000_000_000 + seq}
    if role == "user":
        return {**common, "kind": "message", "role": role, "content": text, "clientNonce": nonce}
    return {**common, "kind": "send-message", "message": {"type": "text", "content": text}}


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
        self.acceptance = {"outcome": "not-found"}
        self.on_poll = None
        self.polls = 0
        self.last_prompt = None
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
        real_sync_client = httpx.Client
        sync_patch = mock.patch("httpx.Client", side_effect=lambda **kwargs: real_sync_client(
            transport=httpx.MockTransport(self.handle), **kwargs,
        ))
        sync_patch.start()
        self.addCleanup(sync_patch.stop)

    def turn_entries(self, body):
        start = len(self.entries[body["agentId"]]) + 1
        return [
            entry("user", body["prompt"], nonce=body["clientNonce"], request_id=f"req-{body['clientNonce']}", seq=start),
            entry("assistant", "PONG", request_id=f"req-{body['clientNonce']}", seq=start + 1),
        ]

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
            self.last_prompt = body
            if self.auto_reply:
                self.entries[body["agentId"]].extend(self.turn_entries(body))
            self.sent.set()
            return httpx.Response(200, json={"accepted": True})
        if name == "listAgents":
            self.polls += 1
            if self.on_poll:
                self.on_poll(self.polls)
            return httpx.Response(200, json=[
                {"id": seat, "isRunning": self.busy} for seat in self.entries
            ])
        if name in {"getAsyncTasks", "getSubagents"}:
            return httpx.Response(200, json=[])
        if name == "promptAcceptanceStatus":
            if not self.acceptance_available:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json=self.acceptance)
        if name == "getAgentTranscript":
            return httpx.Response(200, json=self.entries.get(body["id"], []))
        if name == "interruptAgentRun":
            self.interrupted.set()
            return httpx.Response(200 if self.interrupt_available else 404, json={"hadActiveRun": True})
        if name == "deleteAgent":
            del self.entries[body["id"]]
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected command: {name}")

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
        self.assertIsNone(find_session_file("space-uuid"))
        self.assertFalse((self.root / "sand" / "agent-transcripts").exists())

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
                self.entries["seat-1"].append(self.turn_entries(self.last_prompt)[0])
            else:
                # Identical answer text is fine when it is a NEW entry.
                self.entries["seat-1"].append(self.turn_entries(self.last_prompt)[1])

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

    async def test_not_found_through_idle_running_reply_then_idle(self):
        self.auto_reply = False
        delays = []

        def advance(poll):
            # Live probe: initially idle; reply lands before the run ends.
            self.busy = poll in (2, 3)
            if poll == 2:
                self.entries["seat-1"] = self.turn_entries(self.last_prompt)[:1]
            if poll == 3:
                self.entries["seat-1"].append(self.turn_entries(self.last_prompt)[1])

        async def sleep(delay):
            delays.append(delay)

        self.on_poll = advance
        with mock.patch("services.cowork_agent.adapters.grokbot.oneshot.asyncio.sleep", side_effect=sleep):
            result = await AgentDispatcher("grokbot").ask("ping", "space-uuid", is_new_session=True)
        self.assertIn("PONG", str(result))
        self.assertEqual(self.polls, 4)
        self.assertEqual(delays, [1.0, 1.5, 2.25])
        self.assertFalse(self.interrupted.is_set())

    async def test_nonce_is_retained_when_prompt_leaves_running_window(self):
        def advance(poll):
            self.busy = poll == 1
            if poll == 2:
                self.entries["seat-1"] = self.entries["seat-1"][1:]

        self.on_poll = advance
        with mock.patch("services.cowork_agent.adapters.grokbot.oneshot.asyncio.sleep", new_callable=mock.AsyncMock):
            result = await run_turn("ping", "space-uuid", is_new_session=True)
        self.assertEqual(result["message"], "PONG")
        self.assertEqual(self.polls, 2)

    async def test_pending_acceptance_does_not_block_but_rejected_fails(self):
        self.acceptance = {"outcome": "found", "record": {"status": "pending"}}
        self.assertEqual((await run_turn("ping"))["message"], "PONG")
        self.acceptance["record"]["status"] = "rejected"
        with self.assertRaisesRegex(GrokbotGatewayError, "rejected"):
            await run_turn("ping")


class TranscriptTests(unittest.TestCase):
    def test_nonce_matching_ignores_repeated_prompts_and_other_requests(self):
        from services.cowork_agent.adapters.grokbot.transcript import current_turn_reply
        entries = [
            entry("user", "same", nonce="old", request_id="old"),
            entry("assistant", "stale", request_id="old"),
            entry("user", "same"),
            entry("assistant", "first part"),
            entry("user", "same", nonce="other", request_id="other"),
            entry("assistant", "unrelated", request_id="other"),
            entry("assistant", "second part"),
        ]
        self.assertEqual(current_turn_reply(entries, "nonce"), ("request", "first part\n\nsecond part"))
        self.assertEqual(current_turn_reply(entries[:2], "nonce"), (None, ""))
        self.assertEqual(current_turn_reply(entries[2:], "nonce"), ("request", "first part\n\nsecond part"))

    def test_mixed_transcript_ignores_unrelated_entries_without_correlation_keys(self):
        from services.cowork_agent.adapters.grokbot.transcript import current_turn_reply
        old_prompt = {"kind": "message", "role": "user", "content": "ping"}
        old_reply = {"kind": "send-message", "message": {"type": "text", "content": "unrelated"}}
        entries = [old_prompt, old_reply, entry("user", "ping"), entry("assistant", "PONG")]
        self.assertEqual(current_turn_reply(entries, "nonce"), ("request", "PONG"))
        self.assertEqual(current_turn_reply(entries[:2], "nonce"), (None, ""))
        self.assertEqual(current_turn_reply([old_reply], "nonce", "request"), ("request", ""))

    def test_current_nonce_requires_a_valid_request_id(self):
        from services.cowork_agent.adapters.grokbot.transcript import current_turn_reply
        for request_id in (None, "", 123):
            with self.subTest(request_id=request_id):
                prompt = entry("user", "ping", request_id=request_id)
                if request_id is None:
                    del prompt["requestId"]
                entries = [prompt, entry("assistant", "PONG")]
                with self.assertRaisesRegex(GrokbotGatewayError, "missing requestId"):
                    current_turn_reply(entries, "nonce")

    def test_history_keeps_repeated_prompts_and_stable_ids(self):
        records = [
            entry("user", "same", seq=1), entry("assistant", "reply", seq=2),
            entry("user", "same", seq=3), entry("assistant", "reply", seq=4),
        ]
        messages = sessions._convert_messages("space", records)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages, sessions._convert_messages("space", records))
        self.assertEqual(len({row["id"] for row in messages}), 4)


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self):
        gateway = mock.Mock()
        gateway.list_agents = mock.AsyncMock(return_value=[{"id": "seat"}])
        gateway.get_async_tasks = mock.AsyncMock(return_value=[])
        gateway.get_subagents = mock.AsyncMock(return_value=[])
        gateway.prompt_acceptance_status = mock.AsyncMock(return_value={"outcome": "not-found"})
        gateway.get_agent_transcript = mock.AsyncMock(return_value=[
            entry("user", "ping"), entry("assistant", "DONE", seq=2),
        ])
        return gateway

    async def test_reply_needs_idle_roster_tasks_and_subagents(self):
        from services.cowork_agent.adapters.grokbot.oneshot import wait_for_idle
        for signal in ("isRunning", "isComposingMessage", "tasks", "subagents"):
            with self.subTest(signal=signal):
                gateway = self.gateway()
                if signal == "tasks":
                    gateway.get_async_tasks.side_effect = [[{"id": "task"}], []]
                elif signal == "subagents":
                    gateway.get_subagents.side_effect = [[{"status": "running"}], [{"status": "completed"}]]
                else:
                    gateway.list_agents.side_effect = [[{"id": "seat", signal: True}], [{"id": "seat"}]]
                with mock.patch("services.cowork_agent.adapters.grokbot.oneshot.asyncio.sleep", new_callable=mock.AsyncMock) as sleep:
                    result = await wait_for_idle(gateway, "seat", client_nonce="nonce")
                self.assertEqual(result, ("idle", "DONE"))
                self.assertEqual(gateway.list_agents.await_count, 2)
                sleep.assert_awaited_once()

    async def test_reply_observed_on_deadline_tick_is_returned(self):
        from services.cowork_agent.adapters.grokbot.oneshot import wait_for_idle
        gateway = self.gateway()
        now = 0.0
        clock = mock.Mock()
        clock.time.side_effect = lambda: now

        async def transcript(_):
            nonlocal now
            now = 1.0
            return [entry("user", "ping"), entry("assistant", "DONE", seq=2)]

        gateway.get_agent_transcript.side_effect = transcript
        with mock.patch("services.cowork_agent.adapters.grokbot.oneshot.asyncio.get_running_loop", return_value=clock):
            result = await wait_for_idle(gateway, "seat", client_nonce="nonce", timeout_s=1)
        self.assertEqual(result, ("idle", "DONE"))


if __name__ == "__main__":
    unittest.main()
