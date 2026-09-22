"""Focused unit tests for the Grok Bot adapter (paths, seats, sessions, HTTP)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from services.cowork_agent.adapters.grokbot.adapter import Adapter, GrokbotAdapter
from services.cowork_agent.adapters.grokbot.gateway import (
    GrokbotGateway,
    GrokbotGatewayError,
    MISSING_TOKEN_HINT,
)
from services.cowork_agent.adapters.grokbot.oneshot import (
    last_assistant_text,
    resolve_target_agent,
    run_turn,
)
from services.cowork_agent.adapters.grokbot.paths import (
    DEFAULT_GATEWAY_URL,
    discover_gateway,
    normalize_gateway_url,
    redact_secret,
    resolve_sand_root,
    transcript_path,
)
from services.cowork_agent.adapters.grokbot import session_seats, sessions


def _clear_grokbot_env() -> dict[str, str]:
    keys = (
        "SAND_DATA_ROOT",
        "SAND_USER_DATA_DIR",
        "SAND_GATEWAY_TOKEN",
        "SAND_HOST_PORT",
        "SAND_GATEWAY_BIND_HOST",
        "GROKBOT_GATEWAY_URL",
        "SAND_GATEWAY_URL",
        "GROKBOT_DEFAULT_AGENT_ID",
    )
    return {key: "" for key in keys}


class PathDiscoveryTests(unittest.TestCase):
    def test_wildcard_binds_rewrite_to_loopback(self) -> None:
        self.assertEqual(
            normalize_gateway_url("http://0.0.0.0:1340/"),
            "http://127.0.0.1:1340",
        )
        self.assertEqual(
            normalize_gateway_url("http://[::]:1340"),
            "http://127.0.0.1:1340",
        )

    def test_env_url_wins_and_token_comes_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gateway.json").write_text(
                json.dumps({
                    "port": 9999,
                    "pid": 1,
                    "startedAt": 1,
                    "host": "0.0.0.0",
                    "token": "file-token",
                }),
                encoding="utf-8",
            )
            env = {
                **_clear_grokbot_env(),
                "SAND_DATA_ROOT": str(root),
                "GROKBOT_GATEWAY_URL": "http://0.0.0.0:1340",
                "SAND_GATEWAY_TOKEN": "env-token",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                discovered = discover_gateway()
            self.assertEqual(discovered.base_url, "http://127.0.0.1:1340")
            self.assertEqual(discovered.token, "env-token")
            self.assertTrue(discovered.has_token)
            public = json.dumps(discovered.public_dict())
            self.assertNotIn("env-token", public)
            self.assertNotIn("file-token", public)

    def test_token_falls_back_to_gateway_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gateway.json").write_text(
                json.dumps({
                    "port": 1340,
                    "pid": 7,
                    "startedAt": 1,
                    "token": "disk-token",
                }),
                encoding="utf-8",
            )
            env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": str(root)}
            with mock.patch.dict(os.environ, env, clear=False):
                discovered = discover_gateway()
            self.assertEqual(discovered.token, "disk-token")
            self.assertEqual(discovered.port, 1340)

    def test_sand_root_env_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-sand"
            root.mkdir()
            env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": str(root)}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(resolve_sand_root(), root)

    def test_redact_secret_strips_token(self) -> None:
        self.assertEqual(redact_secret("Bearer super-secret", "super-secret"), "Bearer [redacted]")

    def test_default_url_when_nothing_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                discovered = discover_gateway()
            self.assertEqual(discovered.base_url, DEFAULT_GATEWAY_URL)
            self.assertFalse(discovered.has_token)


class SessionSeatTests(unittest.TestCase):
    def test_remember_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": tmp}, clear=False):
                self.assertIsNone(session_seats.lookup_seat("space-1"))
                session_seats.remember_seat("space-1", "agent-9")
                self.assertEqual(session_seats.lookup_seat("space-1"), "agent-9")

    def test_resolve_refuses_all_and_uses_default_on_new_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **_clear_grokbot_env(),
                "QUIRQ_STATE_ROOT": tmp,
                "GROKBOT_DEFAULT_AGENT_ID": "all",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(GrokbotGatewayError):
                    resolve_target_agent(None, is_new_session=True)
            env["GROKBOT_DEFAULT_AGENT_ID"] = "ada"
            with mock.patch.dict(os.environ, env, clear=False):
                agent_id, mint = resolve_target_agent(None, is_new_session=True)
            self.assertEqual(agent_id, "ada")
            self.assertFalse(mint)

    def test_resolve_uses_remembered_seat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**_clear_grokbot_env(), "QUIRQ_STATE_ROOT": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                session_seats.remember_seat("space-uuid", "host-seat")
                agent_id, mint = resolve_target_agent("space-uuid", is_new_session=False)
            self.assertEqual(agent_id, "host-seat")
            self.assertFalse(mint)

    def test_new_chat_without_default_mints_throwaway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**_clear_grokbot_env(), "QUIRQ_STATE_ROOT": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                agent_id, mint = resolve_target_agent("brand-new-uuid", is_new_session=True)
            self.assertIsNone(agent_id)
            self.assertTrue(mint)


class SessionsDiskTests(unittest.TestCase):
    def test_list_and_read_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "agent-transcripts" / "ada" / "ada.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join([
                    json.dumps({"type": "metadata", "model": "x"}),
                    json.dumps({
                        "role": "user",
                        "message": {"content": [{"type": "text", "text": "Hello from Space"}]},
                    }),
                    json.dumps({
                        "role": "assistant",
                        "message": {"content": [
                            {"type": "text", "text": "Pong"},
                            {"type": "tool_use", "name": "Read", "input": {"path": "a"}},
                            {"type": "tool_result", "name": "Read", "result": "ok"},
                        ]},
                    }),
                ]),
                encoding="utf-8",
            )
            env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": str(root)}
            with mock.patch.dict(os.environ, env, clear=False):
                rows = sessions.list_native_sessions()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], "ada")
                self.assertEqual(rows[0]["title"], "Hello from Space")
                self.assertTrue(sessions.owns_session("ada"))
                self.assertFalse(sessions.owns_session("missing"))
                self.assertEqual(transcript_path("ada"), transcript)
                messages = sessions.get_messages("ada")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["data"]["role"], "user")
            self.assertEqual(messages[0]["parts"][0]["data"]["text"], "Hello from Space")
            self.assertEqual(messages[1]["data"]["role"], "assistant")
            tools = [p for p in messages[1]["parts"] if p["data"]["type"] == "tool"]
            self.assertEqual(tools[0]["data"]["state"]["output"], "ok")

    def test_missing_sand_data_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": str(Path(tmp) / "absent")}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(sessions.list_native_sessions(), [])
                self.assertEqual(sessions.get_messages("ada"), [])


class TranscriptParseTests(unittest.TestCase):
    def test_last_assistant_text_from_host_rows(self) -> None:
        entries = [
            {"kind": "message", "role": "user", "content": "hi"},
            {"kind": "send-message", "message": {"type": "text", "content": "latest reply"}},
            {"kind": "message", "role": "assistant", "content": "older", "streaming": True},
        ]
        self.assertEqual(last_assistant_text(entries), "latest reply")


class AdapterContractTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_name(self) -> None:
        self.assertIs(Adapter, GrokbotAdapter)
        self.assertEqual(GrokbotAdapter({}).adapter_name, "grokbot")

    async def test_health_hits_unauthenticated_health(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            self.assertNotIn("authorization", {k.lower() for k in request.headers})
            return httpx.Response(200, json={"ok": True, "isBusy": False, "activeAgentId": None})

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        env = {
            **_clear_grokbot_env(),
            "GROKBOT_GATEWAY_URL": "http://127.0.0.1:1340",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("httpx.AsyncClient", side_effect=factory):
                payload = await GrokbotAdapter({}).health()
        self.assertIn("GET /health", seen)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gateway"], "up")

    async def test_run_fails_clearly_without_token(self) -> None:
        env = {**_clear_grokbot_env(), "SAND_DATA_ROOT": tempfile.mkdtemp()}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(GrokbotGatewayError) as ctx:
                await GrokbotAdapter({}).run("hello")
        self.assertIn("SAND_GATEWAY_TOKEN", str(ctx.exception))
        self.assertIn("gateway.json", str(ctx.exception))
        self.assertNotIn("token=", str(ctx.exception))

    async def test_gateway_errors_redact_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad secret-token-value")

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        env = {
            **_clear_grokbot_env(),
            "GROKBOT_GATEWAY_URL": "http://127.0.0.1:1340",
            "SAND_GATEWAY_TOKEN": "secret-token-value",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("httpx.AsyncClient", side_effect=factory):
                with self.assertRaises(GrokbotGatewayError) as ctx:
                    await GrokbotGateway().command("listAgents", {})
        self.assertNotIn("secret-token-value", str(ctx.exception))
        self.assertIn("[redacted]", str(ctx.exception))

    def test_missing_token_hint_is_actionable(self) -> None:
        self.assertIn("SAND_GATEWAY_TOKEN", MISSING_TOKEN_HINT)
        self.assertIn("gateway.json", MISSING_TOKEN_HINT)

    async def test_run_turn_mints_throwaway_and_returns_reply(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(f"{request.method} {path}")
            if path == "/api/createAgent":
                return httpx.Response(200, json={"agent": {"id": "seat-1", "name": "xo-space-x"}})
            if path == "/api/sendPrompt":
                body = json.loads(request.content)
                self.assertEqual(body["agentId"], "seat-1")
                self.assertNotEqual(body["agentId"], "all")
                return httpx.Response(200, json={"accepted": True})
            if path == "/api/listAgents":
                return httpx.Response(200, json=[{
                    "id": "seat-1",
                    "isRunning": False,
                    "isComposingMessage": False,
                    "awaitingUserResponse": None,
                }])
            if path in {"/api/getAsyncTasks", "/api/getSubagents"}:
                return httpx.Response(200, json=[])
            if path == "/api/promptAcceptanceStatus":
                return httpx.Response(200, json={
                    "outcome": "found",
                    "record": {"status": "accepted"},
                })
            if path == "/api/getAgentTranscriptTail":
                return httpx.Response(200, json={
                    "entries": [{"kind": "message", "role": "assistant", "content": "PONG"}],
                })
            return httpx.Response(404, text="unexpected")

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **_clear_grokbot_env(),
                "QUIRQ_STATE_ROOT": tmp,
                "SAND_DATA_ROOT": tmp,
                "GROKBOT_GATEWAY_URL": "http://127.0.0.1:1340",
                "SAND_GATEWAY_TOKEN": "test-token",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch("httpx.AsyncClient", side_effect=factory):
                    result = await run_turn("ping", "space-uuid", is_new_session=True)
                    mapped = session_seats.lookup_seat("space-uuid")
        self.assertEqual(result["message"], "PONG")
        self.assertEqual(result["native_session_id"], "seat-1")
        self.assertEqual(mapped, "seat-1")
        self.assertIn("POST /api/createAgent", calls)
        self.assertIn("POST /api/sendPrompt", calls)
        self.assertNotIn("POST /api/broadcastToAgents", calls)


if __name__ == "__main__":
    unittest.main()
