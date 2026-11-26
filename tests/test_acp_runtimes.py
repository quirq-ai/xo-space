import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.storm import engine as storm
from tests.fakes import acp_native, install
from tests.storm_engine_kit import Kit, message


class ObservationTests(unittest.TestCase):
    def test_messages_and_prompt_details(self):
        for provider in ("grok_build", "cursor"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                root, child = acp_native.transcript(home, provider)
                with mock.patch.dict(os.environ, {"HOME": tmp, "GROK_HOME": str(home / ".grok"),
                                                  "CURSOR_HOME": str(home / ".cursor")}):
                    sessions = try_load_capability("sessions", agent=provider)
                    path = dict(sessions.history.runs(root))[child]
                    native = root if provider == "cursor" else child
                    if native == root:
                        sessions.resolve_native_file({}, native).write_bytes(path.read_bytes())
                    messages = sessions.get_messages(native)
                    self.assertEqual([m["data"]["role"] for m in messages], ["user", "assistant"])
                    tool = messages[1]["parts"][0]["data"]
                    self.assertEqual(tool["state"]["status"], "completed")
                    prompts = try_load_capability("session_prompts", agent=provider).collect_session_prompts(native)
                    self.assertEqual(prompts["prompts"][0]["text"], "question")
                    self.assertEqual(prompts["prompts"][0]["tool_uses"], 1)


class AcpEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_providers_share_the_engine_and_keep_their_turns(self):
        kit = Kit()
        self.addAsyncCleanup(kit.close)
        providers = ("cursor", "grok_build")
        env = {}
        for agent in providers:
            runtime = storm.load_runtime(agent)
            fake = install(agent, kit.root / "bin", acp_native.cases(agent)["turn"]())
            env[runtime.path_env] = str(fake.path)
        with mock.patch.dict(os.environ, env):
            engine = await storm.boot(providers=providers, listeners=[kit.recorder])
            kit.engine = engine
            self.assertIsNotNone(engine)
            contexts = []
            for number, agent in enumerate(providers):
                sid = await engine.create_session(c.SessionSpec(cwd=str(kit.work)), agent=agent,
                                                  role="plain", preset="plain_chat")
                contexts.append(sid)
                await engine.deliver(sid, message(number + 1, "hello"))
            for sid in contexts:
                event = await asyncio.wait_for(kit.recorder.seen("turn.finished", session_id=sid), 20)
                self.assertEqual(event.data["status"], "finished")
                self.assertIn("step.text", kit.recorder.types(sid))
            self.assertEqual(kit.rows("SELECT count(*) FROM wakes WHERE state='consumed'"), [(2,)])

    async def test_a_cli_that_runs_no_guard_hook_is_never_opened_and_its_thread_says_why(self):
        kit = Kit()
        self.addAsyncCleanup(kit.close)
        runtime = storm.load_runtime("cursor")
        fake = install("cursor", kit.root / "bin", acp_native.cases("cursor")["no_guard"]())
        with mock.patch.dict(os.environ, {runtime.path_env: str(fake.path)}):
            engine = await storm.boot(providers=("cursor",), listeners=[kit.recorder])
            kit.engine = engine
            sid = await engine.create_session(c.SessionSpec(cwd=str(kit.work)), agent="cursor",
                                              role="plain", preset="plain_chat")
            await engine.deliver(sid, message(1, "hello"))
            event = await asyncio.wait_for(kit.recorder.seen("session", session_id=sid, reason="open_failed"), 20)
        self.assertFalse(event.data["retryable"])
        self.assertIn("didn't run XO's tool-guard hook", event.data["error"])
        self.assertNotIn("turn.started", kit.recorder.types(sid))
