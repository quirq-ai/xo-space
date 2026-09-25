"""Gateway history regressions with no host files or network access."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from services.cowork_agent.adapters.grokbot import session_seats, sessions
from services.cowork_agent.adapters.grokbot.gateway import GrokbotGatewayError
from services.cowork_agent.engine.sessions_io import load_all_sessions
from tests.test_grokbot_adapter import _clear_grokbot_env


def row(seq, text="hello"):
    if seq % 2:
        return {"kind": "message", "role": "user", "content": text,
                "seq": seq, "timestampMs": 1_700_000_000_000 + seq}
    return {"kind": "send-message", "message": {"type": "text", "content": text},
            "seq": seq, "timestampMs": 1_700_000_000_000 + seq}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patch = mock.patch.dict(os.environ, {
            **_clear_grokbot_env(), "AGENT_NAME": "grokbot",
            "QUIRQ_STATE_ROOT": str(self.root / "state"),
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
            "SAND_DATA_ROOT": str(self.root / "absent"),
            "SAND_GATEWAY_TOKEN": "private-token",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []
        self.clients = []
        self.reply = lambda name, body: [row(1), row(2)]
        real_client = httpx.Client

        def factory(**kwargs):
            client = real_client(transport=httpx.MockTransport(self.handle), **kwargs)
            self.clients.append(client)
            return client

        patch = mock.patch("httpx.Client", side_effect=factory)
        patch.start()
        self.addCleanup(patch.stop)
        session_seats.remember_seat("space", "seat", question="first prompt")

    def handle(self, request):
        name = request.url.path.removeprefix("/api/")
        body = json.loads(request.content)
        self.calls.append((name, body))
        self.assertEqual(request.headers["Authorization"], "Bearer private-token")
        self.assertEqual(body["id"], "seat")
        self.assertLessEqual(request.extensions["timeout"]["read"], 5)
        response = self.reply(name, body)
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    def test_reopen_pages_older_history_and_deduplicates_overlapping_windows(self):
        def reply(name, body):
            if name == "getAgentTranscript":
                return [row(7), row(8)]
            self.assertEqual(name, "getAgentTranscriptPage")
            self.assertEqual(body["limit"], 200)
            before = body["beforeSeq"]
            if before == 7:
                return {"entries": [row(5), row(6), row(7)], "nextBeforeSeq": 5}
            if before == 5:
                return {"entries": [row(3), row(4)], "nextBeforeSeq": 3}
            self.assertEqual(before, 3)
            return {"entries": [row(1), row(2)]}

        self.reply = reply
        messages = sessions.get_messages("space")
        self.assertEqual([m["id"] for m in messages], [f"space:{i}" for i in range(1, 9)])
        self.assertEqual([m["data"]["role"] for m in messages], ["user", "assistant"] * 4)
        self.assertTrue(all(m["session_id"] == "space" for m in messages))
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].is_closed)
        self.assertFalse((self.root / "absent").exists())

    def test_full_transcript_needs_one_call_and_keeps_text_and_timestamps(self):
        messages = sessions.get_messages("space")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(messages[1]["parts"][0]["data"], {"type": "text", "text": "hello"})
        self.assertEqual(messages[0]["time_created"], "2023-11-14T22:13:20.001000+00:00")

    def test_listing_uses_index_without_any_gateway_calls(self):
        first = session_seats.indexed_sessions()["space"]
        session_seats.remember_seat("space", "seat", question="followup")
        session_seats.remember_seat("second", "seat-2", question="another chat")
        rows = {r["id"]: r for r in load_all_sessions()}
        self.assertEqual(set(rows), {"space", "second"})
        self.assertEqual(rows["space"]["title"], "first prompt")
        self.assertEqual(session_seats.indexed_sessions()["space"]["createdAt"], first["createdAt"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.clients, [])

    def test_unindexed_seat_is_not_imported(self):
        self.assertFalse(sessions.owns_session("unknown"))
        self.assertEqual(sessions.get_messages("unknown"), [])
        self.assertEqual(self.calls, [])

    def test_wrapped_gateway_entries_keep_sequence_and_only_render_text(self):
        self.reply = lambda *_: {"entries": [
            {"seq": 1, "entry": row(1)},
            {"seq": 2, "entry": row(2)},
            {"seq": 3, "kind": "notice", "content": "internal"},
            {"seq": 4, "kind": "send-message", "message": {"type": "image", "content": "image"}},
        ]}
        messages = sessions.get_messages("space")
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(p["data"]["type"] == "text" for m in messages for p in m["parts"]))

    def test_unavailable_history_fails_instead_of_looking_empty_and_redacts_token(self):
        self.reply = lambda *_: httpx.Response(503, text="private-token unavailable")
        with self.assertRaisesRegex(GrokbotGatewayError, "HTTP 503") as caught:
            sessions.get_messages("space")
        self.assertNotIn("private-token", str(caught.exception))
        self.assertTrue(self.clients[0].is_closed)

    def test_missing_token_fails_without_a_request(self):
        with mock.patch.dict(os.environ, {"SAND_GATEWAY_TOKEN": ""}):
            with self.assertRaisesRegex(GrokbotGatewayError, "SAND_GATEWAY_TOKEN"):
                sessions.get_messages("space")
        self.assertEqual(self.calls, [])
        self.assertTrue(self.clients[0].is_closed)

    def test_pagination_errors_are_not_silent_truncation(self):
        for page in [httpx.Response(404), {"entries": [row(5)], "nextBeforeSeq": 5}]:
            with self.subTest(page=page):
                self.reply = lambda name, body: [row(5), row(6)] if name == "getAgentTranscript" else page
                with self.assertRaises(GrokbotGatewayError):
                    sessions.get_messages("space")
        self.assertTrue(all(c.is_closed for c in self.clients))

    def test_missing_sequence_is_an_explicit_compatibility_error(self):
        entry = row(1)
        del entry["seq"]
        self.reply = lambda *_: [entry]
        with self.assertRaisesRegex(GrokbotGatewayError, "missing seq"):
            sessions.get_messages("space")

    def test_empty_host_history_is_empty(self):
        self.reply = lambda *_: []
        self.assertEqual(sessions.get_messages("space"), [])
