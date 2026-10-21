"""Plain chat on storm through ``/api/chat`` (storm PR 19; ch 17 §2.1, §3;
ch 10 §4.1; owner §0.108, §0.109; sedc 09 #6, #12), on the fake claude CLI:
create and continue, add / now / 409, abort, active, a paused session
resumed, a retried ``client_id``, a pre-switch chat continued, the folder
rule, the model rule, Composio in the process's MCP config and not in the
store, and the branch itself: an agent without a runtime never reaches storm,
and claude never reaches today's dispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.cowork_agent.chat import router as chat_router
from routers.cowork_agent.sessions import router as observation_router
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.connectors.composio import byo_key
from services.cowork_agent.connectors.composio import service as composio_service
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.engine.dispatcher import AgentDispatcher
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit
from tests.test_claude_runtime import SKIP_FLAGS

OLD_CHAT = "aaaaaaaa-0000-4000-8000-000000000019"      # the id today's /api/chat gave a chat
OLD_NATIVE = "11111111-1111-4111-8111-111111111111"     # that chat's CLI transcript
PROXY = "http://127.0.0.1:5399/mcp/composio-proxy/u/FAKE-PROXY-TOKEN"


def app() -> FastAPI:
    application = FastAPI()
    application.include_router(observation_router)
    application.include_router(chat_router)
    return application


class PlainChatCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.env = mock.patch.dict(os.environ, {"AGENT_NAME": AGENT, "PORT": "5399"})
        self.env.start()
        # No Composio key on this machine reaches a test: each test that wants one says so.
        self.no_key = mock.patch.object(byo_key, "configured", return_value=False)
        self.no_key.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.no_key.stop()
        self.env.stop()             # before the kit's: each restores the env it found when it started
        await self.kit.close()

    def started(self, *after: dict, native: str = SID) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(native), *after]

    async def prompt(self, text: str, client_id: str, **body) -> httpx.Response:
        return await self.client.post("/api/chat/prompt", json={"text": text, "client_id": client_id, **body})

    async def ok(self, text: str, client_id: str, **body) -> dict:
        response = await self.prompt(text, client_id, **body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stdin_texts(self, run: dict) -> list[str]:
        return [line["message"]["content"][0]["text"] for line in run["stdin"] if line.get("type") == "user"]


class CreateAndContinueTests(PlainChatCase):
    async def test_a_prompt_creates_a_plain_session_and_the_retry_answers_its_turn(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi there"), claude.idle(SID)))
        creds = kit.home / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "fake"}}))
        before = hashlib.sha256(creds.read_bytes()).hexdigest()
        engine = await kit.boot()
        with mock.patch("services.swarm_api._http.request") as swarm:
            first = await self.ok("hello", "c1", cwd=str(kit.work))
            self.assertEqual((first["session_id"][:4], first["turn_id"]), ("cse_", None))   # it waits for its turn
            finished = await kit.recorder.seen("turn.finished", session_id=first["session_id"])
        swarm.assert_not_called()                              # a chat turn calls no xo-swarm-api route
        self.assertEqual(await self.ok("hello", "c1", cwd=str(kit.work)),
                         {"session_id": first["session_id"], "turn_id": finished.turn_id})
        view = await engine.get_session(first["session_id"])
        self.assertEqual((view["role"], view["preset"], view["agent"], view["permission_mode"], view["cwd"]),
                         ("plain", "plain_chat", AGENT, "auto", str(kit.work)))
        (run,) = kit.runs()
        self.assertEqual(self.stdin_texts(run), ["hello"])     # as typed, delivered once
        self.assertFalse([a for a in run["argv"] if any(flag in a for flag in SKIP_FLAGS)], run["argv"])
        self.assertEqual(run["argv"][run["argv"].index("--permission-mode") + 1], "auto")
        self.assertEqual(run["cwd"], str(kit.work))
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(1,)])
        self.assertEqual(hashlib.sha256(creds.read_bytes()).hexdigest(), before)   # credentials untouched

    async def test_a_prompt_continues_by_session_id_or_native_id(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "one"), claude.idle(SID), claude.user_message(),
                                  *claude.text_turn(SID, "two", message_id="m2"), claude.idle(SID),
                                  claude.user_message(), *claude.text_turn(SID, "three", message_id="m3"),
                                  claude.idle(SID)))
        engine = await kit.boot()
        sid = (await self.ok("first", "c1"))["session_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((await self.ok("second", "c2", session_id=sid))["session_id"], sid)
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        native = (await engine.get_session(sid))["native_id"]          # the id observation lists it under
        self.assertEqual((await self.ok("third", "c3", session_id=native, agent=AGENT))["session_id"], sid)
        await kit.recorder.seen("turn.finished", session_id=sid, count=3)
        (run,) = kit.runs()                                    # one process for the whole chat
        self.assertEqual(self.stdin_texts(run), ["first", "second", "third"])
        self.assertEqual(run["cwd"], os.path.realpath(kit.projects))     # no cwd: the projects folder

    async def test_concurrent_retries_of_a_new_chat_make_one_session(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        await kit.boot()
        answers = await asyncio.gather(*(self.ok("hello", "same") for _ in range(4)))
        self.assertEqual(len({a["session_id"] for a in answers}), 1)
        await kit.recorder.seen("turn.finished", session_id=answers[0]["session_id"])
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(1,)])
        self.assertEqual(self.stdin_texts(kit.runs()[0]), ["hello"])

    async def test_a_client_id_reused_for_another_session_is_refused(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        await kit.boot()
        first = (await self.ok("one", "c1"))["session_id"]
        other = await kit.session()
        response = await self.prompt("two", "c1", session_id=other)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "client_id_reused"))
        await kit.recorder.seen("turn.finished", session_id=first)

    async def test_a_paused_session_is_resumed_by_the_persons_message(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "one"), claude.idle(SID), claude.user_message(),
                                  *claude.text_turn(SID, "two", message_id="m2"), claude.idle(SID)))
        engine = await kit.boot()
        sid = (await self.ok("first", "c1"))["session_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        await engine.pause(sid, reason="the loop breaker stopped a repeated denial")
        await self.ok("go on", "c2", session_id=sid)
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertIsNone((await engine.get_session(sid))["paused"])
        self.assertEqual(self.stdin_texts(kit.runs()[0]), ["first", "go on"])


class DeliveryTests(PlainChatCase):
    async def test_add_joins_the_running_turn(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.user_message(), *claude.text_turn(SID, "both"), claude.idle(SID)))
        await kit.boot()
        sid = (await self.ok("first", "c1"))["session_id"]
        started = await kit.recorder.seen("turn.started", session_id=sid)
        await self.ok("and this", "c2", session_id=sid, delivery="add")
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((finished.turn_id, finished.data["wakes"]),
                         (started.turn_id, ["msg:prompt:c1", "msg:prompt:c2"]))

    async def test_now_over_live_sub_agents_answers_409_until_confirmed(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.task_started(SID, "task-1"), *claude.host_control("interrupt"),
                                  claude.result(SID, "", subtype="error_during_execution",
                                                terminal_reason="aborted_tools"),
                                  *claude.task_killed(SID, "task-1"), claude.idle(SID),
                                  claude.user_message(), *claude.text_turn(SID, "now then", message_id="m2"),
                                  claude.idle(SID)))
        await kit.boot()
        sid = (await self.ok("first", "c1"))["session_id"]
        await kit.recorder.seen("run.started", session_id=sid)
        refused = await self.prompt("urgent", "c2", session_id=sid, delivery="now")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual((refused.json()["detail"]["code"], refused.json()["detail"]["count"]), ("would_stop_runs", 1))
        self.assertEqual(kit.rows("SELECT count(*) FROM wakes WHERE id = 'msg:prompt:c2'"), [(0,)])
        await self.ok("urgent", "c2", session_id=sid, delivery="now", confirm_stop_runs=1)
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(second.data["wakes"], ["msg:prompt:c2"])
        self.assertEqual(kit.recorder.of("turn.finished", sid)[0].data["status"], "interrupted")


class AbortAndActiveTests(PlainChatCase):
    async def test_abort_stops_the_turn_and_active_lists_it_while_it_runs(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "make test"}),
                                  *claude.host_control("interrupt"),
                                  claude.result(SID, "", subtype="error_during_execution",
                                                terminal_reason="aborted_tools"),
                                  claude.idle(SID)))
        await kit.boot()
        self.assertEqual((await self.client.get("/api/chat/active")).json(), {"turns": []})
        sid = (await self.ok("run the tests", "c1"))["session_id"]
        started = await kit.recorder.seen("turn.started", session_id=sid)
        await kit.recorder.seen("step.tool", session_id=sid)
        (turn,) = (await self.client.get("/api/chat/active")).json()["turns"]
        self.assertEqual((turn["session_id"], turn["turn_id"], turn["role"], turn["agent"], turn["origin"]),
                         (sid, started.turn_id, "plain", AGENT, "person"))

        stopped = await self.client.post("/api/chat/abort", json={"session_id": sid})
        self.assertEqual(stopped.json(), {"session_id": sid, "stopped": True, "turn_id": started.turn_id})
        await kit.recorder.seen("turn.stopped", session_id=sid, by="person")
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertEqual((await self.client.get("/api/chat/active")).json(), {"turns": []})
        native = (await self.kit.engine.get_session(sid))["native_id"]
        again = await self.client.post("/api/chat/abort", json={"session_id": native})       # by its native id
        self.assertEqual(again.json(), {"session_id": sid, "stopped": False, "turn_id": None})

    async def test_abort_names_one_target(self) -> None:
        await self.kit.boot()
        for body in ({}, {"session_id": "cse_x", "stream_id": "s"}):
            with self.subTest(body=body):
                response = await self.client.post("/api/chat/abort", json=body)
                self.assertEqual((response.status_code, response.json()["detail"]["code"]), (400, "one_target"))
        missing = await self.client.post("/api/chat/abort", json={"session_id": "cse_nope"})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "session_not_found"))
        extra = await self.client.post("/api/chat/abort", json={"session_id": "cse_x", "why": "x"})
        self.assertEqual(extra.status_code, 422)
        today = await self.client.post("/api/chat/abort", json={"stream_id": "nope"})
        self.assertEqual((today.status_code, today.json()), (200, {"ok": True}))


class PreSwitchChatTests(PlainChatCase):
    """§0.109: a chat today's /api/chat started continues on storm, found in
    XO's session index, resuming its own transcript in its folder."""

    def seed_old_chat(self, *, transcript: bool = True) -> None:
        row = {"sessionId": OLD_CHAT, "nativeSessionId": OLD_NATIVE, "directory": str(self.kit.work),
               "backend": AGENT, "updatedAt": 1, "usage": {"input_tokens": 0, "output_tokens": 0,
                                                            "cache_creation_input_tokens": 0,
                                                            "cache_read_input_tokens": 0}}
        self.assertTrue(sessions_io.write_session_row("demo", "claude:demo:web:0000abcd", row))
        if transcript:
            folder = self.kit.home / ".claude" / "projects" / encode_path(str(self.kit.work))
            folder.mkdir(parents=True)
            (folder / f"{OLD_NATIVE}.jsonl").write_text(json.dumps(
                {"type": "user", "sessionId": OLD_NATIVE, "message": {"role": "user", "content": "before"}}) + "\n")

    async def test_an_old_chat_id_resumes_its_transcript_in_its_folder(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(OLD_NATIVE, "carried on"), claude.idle(OLD_NATIVE),
                                  claude.user_message(),
                                  *claude.text_turn(OLD_NATIVE, "and on", message_id="m2"), claude.idle(OLD_NATIVE),
                                  native=OLD_NATIVE))
        self.seed_old_chat()
        engine = await kit.boot()
        sid = (await self.ok("where were we", "c1", session_id=OLD_CHAT))["session_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((await self.ok("more", "c2", session_id=OLD_CHAT))["session_id"], sid)   # the same one
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        (run,) = kit.runs()
        self.assertEqual(run["argv"][run["argv"].index("--resume") + 1], OLD_NATIVE)
        self.assertEqual(run["cwd"], str(kit.work))
        view = await engine.get_session(sid)
        self.assertEqual((view["native_id"], view["cwd"], view["role"]), (OLD_NATIVE, str(kit.work), "plain"))
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(1,)])

    async def test_an_old_chat_whose_transcript_is_gone_and_an_unknown_id_are_refused(self) -> None:
        kit = self.kit
        kit.scenario([])
        self.seed_old_chat(transcript=False)
        await kit.boot()
        gone = await self.prompt("hi", "c1", session_id=OLD_CHAT)
        self.assertEqual((gone.status_code, gone.json()["detail"]["code"]), (409, "transcript_gone"))
        unknown = await self.prompt("hi", "c2", session_id="aaaaaaaa-0000-4000-8000-00000000dead")
        self.assertEqual((unknown.status_code, unknown.json()["detail"]["code"]), (404, "session_not_found"))
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(0,)])
        self.assertEqual(kit.runs(), [])


class SettingsTests(PlainChatCase):
    async def test_the_folder_must_be_inside_the_projects_root(self) -> None:
        kit = self.kit
        kit.scenario([])
        await kit.boot()
        outside = await self.prompt("hi", "c1", cwd=str(kit.root))
        self.assertEqual((outside.status_code, outside.json()["detail"]["code"]), (400, "outside_projects"))
        escaping = await self.prompt("hi", "c2", cwd=str(kit.work / ".." / ".."))
        self.assertEqual(escaping.json()["detail"]["code"], "outside_projects")
        missing = await self.prompt("hi", "c3", cwd=str(kit.projects / "nope"))
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (400, "no_such_folder"))
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(0,)])

    async def test_a_storm_body_is_strict_and_bypass_is_refused(self) -> None:
        kit = self.kit
        kit.scenario([])
        await kit.boot()
        for body in ({"text": "hi", "client_id": "c", "agent_name": AGENT}, {"text": "hi"},
                     {"text": "", "client_id": "c"}, {"text": "hi", "client_id": "c", "delivery": "later"}):
            with self.subTest(body=body):
                response = await self.client.post("/api/chat/prompt", json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"][0]["loc"][0], "body")
        bypass = await self.prompt("hi", "c4", permission_mode="bypass")
        self.assertEqual((bypass.status_code, bypass.json()["detail"]["code"]), (400, "unsupported_permission_mode"))

    async def test_only_a_real_model_id_is_honoured(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        models = [c.ModelInfo(id="haiku", name="Haiku", efforts=("low", "high")),
                  c.ModelInfo(id="sonnet", name="Sonnet", efforts=("low", "high"))]
        engine = await kit.boot()
        with mock.patch.object(ClaudeRuntime, "models", mock.AsyncMock(return_value=models)):
            profile = await self.ok("hi", "c1", model=f"{AGENT}/main")
            self.assertIsNone((await engine.get_session(profile["session_id"]))["model"])     # ignored
            effort = await self.prompt("hi", "c2", model="haiku", effort="max")
            self.assertEqual((effort.status_code, effort.json()["detail"]["code"]), (400, "unsupported_effort"))
            real = await self.ok("hi", "c3", model="haiku", effort="low")
        self.assertEqual(((await engine.get_session(real["session_id"]))["model"],
                          (await engine.get_session(real["session_id"]))["effort"]), ("haiku", "low"))

    async def test_a_session_keeps_its_agent_folder_and_comms(self) -> None:
        kit = self.kit
        kit.scenario([])
        await kit.boot()
        sid = await kit.session()
        for body, code in (({"agent": "hermes"}, "agent_locked"), ({"cwd": str(kit.projects)}, "fixed_setting"),
                           ({"comms": True}, "fixed_setting")):
            with self.subTest(body=body):
                response = await self.prompt("hi", f"c-{code}-{len(body)}", session_id=sid, **body)
                self.assertEqual(response.json()["detail"]["code"], code, response.text)
        self.assertEqual(kit.rows("SELECT count(*) FROM wakes"), [(0,)])


class ComposioTests(PlainChatCase):
    async def test_composio_is_in_the_processs_mcp_config_and_never_in_the_store(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        await kit.boot()
        with mock.patch.object(byo_key, "configured", return_value=True), \
                mock.patch.object(composio_service, "_composio_proxy_url", return_value=PROXY):
            sid = (await self.ok("hello", "c1"))["session_id"]
            await kit.recorder.seen("turn.finished", session_id=sid)
        (run,) = kit.runs()
        config = Path(run["argv"][run["argv"].index("--mcp-config") + 1])
        self.assertEqual(json.loads(config.read_text())["mcpServers"]["composio"], {"type": "http", "url": PROXY})
        self.assertEqual(oct(config.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("FAKE-PROXY-TOKEN", " ".join(run["argv"]))
        stored = json.loads(kit.rows("SELECT spec FROM sessions WHERE id = ?", (sid,))[0][0])
        self.assertEqual([(s["name"], s["url"]) for s in stored["mcp_servers"]], [("composio", None)])
        for path in (kit.state / "engine").iterdir():       # every byte the store holds, its WAL included
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertNotIn(b"FAKE-PROXY-TOKEN", path.read_bytes())

    async def test_without_a_key_the_process_gets_no_composio(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        await kit.boot()
        with mock.patch.object(composio_service, "_composio_proxy_url", side_effect=AssertionError("no key")):
            sid = (await self.ok("hello", "c1"))["session_id"]
            await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertNotIn("--mcp-config", kit.runs()[0]["argv"])


class ObservationTests(PlainChatCase):
    async def test_a_plain_chat_is_seen_by_observation_under_its_native_id(self) -> None:
        """The observation diff's target (ch 07 §11; inventory §6.2): a chat
        made through POST /api/chat/prompt writes the session-index row that
        observation, usage and the visualizer read, as today's chat did."""
        from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = (await self.ok("Fix the login bug", "c1", cwd=str(kit.work)))["session_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        native = (await engine.get_session(sid))["native_id"]
        await kit.until(lambda: any(r.get("usage", {}).get("input_tokens")
                                    for r in sessions_io.read_session_index("demo").values()))
        # The transcript is the CLI's own; the fake writes none, so write it where claude would.
        folder = kit.home / ".claude" / "projects" / encode_path(str(kit.work))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{native}.jsonl").write_text("\n".join(json.dumps(line) for line in (
            {"type": "user", "uuid": "u1", "timestamp": "2026-10-15T10:00:00Z", "cwd": str(kit.work),
             "sessionId": native, "message": {"role": "user", "content": "Fix the login bug"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-15T10:00:02Z", "cwd": str(kit.work),
             "sessionId": native, "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        )) + "\n")

        (key, row), = sessions_io.read_session_index("demo").items()
        self.assertEqual(key, f"{AGENT}:demo:storm:{sid}")
        self.assertEqual(set(row), {"sessionId", "nativeSessionId", "directory", "backend", "updatedAt", "usage"})
        self.assertEqual((row["sessionId"], row["nativeSessionId"], row["backend"], row["directory"]),
                         (native, native, AGENT, str(kit.work)))
        listed = [s for s in await asyncio.to_thread(sessions_io.load_all_sessions) if s["id"] == native]
        self.assertEqual([(s["agent"], s["title"]) for s in listed], [("demo", "Fix the login bug")])
        self.assertEqual(await asyncio.to_thread(sessions_io.find_session_backend, native), AGENT)
        self.assertIsNotNone(await asyncio.to_thread(sessions_io.find_session_file, native))
        messages = (await self.client.get(f"/api/messages/{native}")).json()
        self.assertEqual([m["parts"][0]["data"]["text"] for m in messages["messages"]], ["Fix the login bug", "ok"])
        self.assertEqual([k for _p, k, _r in iter_sessionslist_rows(AGENT)], [key])


    async def test_a_chat_with_no_folder_is_indexed_at_the_space_level(self) -> None:
        """#146's rule for a session started with no project: no project id
        (an empty key segment), its row in the root index, found by both
        readers, and nothing created in the projects root."""
        kit = self.kit
        kit.work.rmdir()
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = (await self.ok("hey there", "c1"))["session_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        native = (await engine.get_session(sid))["native_id"]
        await kit.until(lambda: bool(sessions_io.read_root_session_index()))
        (key, row), = sessions_io.read_root_session_index().items()
        self.assertEqual((key, row["sessionId"], row["directory"]),
                         (f"{AGENT}::storm:{sid}", native, os.path.realpath(kit.projects)))
        self.assertEqual(await asyncio.to_thread(sessions_io.find_session_backend, native), AGENT)
        self.assertEqual([s["agent"] for s in await asyncio.to_thread(sessions_io.load_all_sessions)
                          if s["id"] == native], [""])
        self.assertEqual(sorted(p.name for p in kit.projects.iterdir()), [])


class BranchTests(PlainChatCase):
    async def test_a_runtime_agent_never_reaches_todays_dispatcher(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        await kit.boot()
        with mock.patch.object(AgentDispatcher, "__init__", side_effect=AssertionError("dispatcher built")):
            sid = (await self.ok("hello", "c1"))["session_id"]
            await kit.recorder.seen("turn.finished", session_id=sid)

    async def test_an_agent_with_neither_a_runtime_nor_a_chat_of_its_own_is_refused_up_front(self) -> None:
        """§0.100: codex and agy chat again with their runtimes (C1, C2);
        until then a prompt for them is refused before anything is made."""
        kit = self.kit
        kit.scenario([])
        await kit.boot()
        for agent in ("codex", "antigravity"):
            for body in ({"agent": agent, "client_id": "c"}, {"agent_name": agent}):
                with self.subTest(agent=agent, body=body):
                    response = await self.client.post("/api/chat/prompt", json={"text": "hi", **body})
                    self.assertEqual(response.status_code, 501)
                    self.assertEqual({k: response.json()["detail"][k] for k in ("code", "feature")},
                                     {"code": "unsupported", "feature": "chat"})
                    self.assertIn(f"{agent} has no storm runtime", response.json()["detail"]["message"])
            with self.subTest(space=agent), mock.patch.dict(os.environ, {"AGENT_NAME": agent}):
                response = await self.client.post("/api/chat/prompt", json={"text": "hi"})
                self.assertEqual((response.status_code, response.json()["detail"]["feature"]), (501, "chat"))
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(0,)])
        self.assertEqual(kit.runs(), [])

    async def test_an_agent_without_a_runtime_keeps_todays_path_and_never_reaches_storm(self) -> None:
        from services.cowork_agent.adapters.hermes import gateway_pool, sessionslist, state_db, streaming
        kit = self.kit
        kit.scenario([])
        await kit.boot()

        async def reply(question, session_id, gateway_base=None):
            yield {"type": "token", "token": "PONG"}
            yield {"done": True, "native_session_id": "native-h1"}
        with mock.patch.object(gateway_pool, "ensure_gateway", return_value=None), \
                mock.patch.object(state_db, "register_inflight_exchange"), \
                mock.patch.object(sessionslist, "write_session_row"), \
                mock.patch.object(streaming, "stream_to_normalized", reply):
            answer = await self.client.post("/api/chat/prompt", json={"text": "ping", "agent_name": "hermes",
                                                                     "workspace": "/x", "extra": 1})
            self.assertEqual(answer.status_code, 200)
            self.assertEqual(sorted(answer.json()), ["session_id", "stream_id"])
            stream = await self.client.get(f"/api/chat/stream/{answer.json()['stream_id']}")
        self.assertIn('event: text-delta\ndata: {"text": "PONG"}', stream.text)
        self.assertEqual(kit.rows("SELECT count(*) FROM sessions"), [(0,)])
        self.assertEqual(kit.runs(), [])


if __name__ == "__main__":
    unittest.main()
