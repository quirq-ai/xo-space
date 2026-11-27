"""The shared ACP session with each CLI's additions, against the wire shapes
grok and cursor-agent sent on 11-27 (sealed probe sessions on their own
logins): permission requests filled from their tool call, titles made from a
machine name, Stop answering what is open, questions, steering and usage."""

from __future__ import annotations

import asyncio
import json
import unittest

from services.cowork_agent.acp.session import AcpSession, usage_of
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import load_runtime

SID = "native-1"


class FakeCli:
    """An ACP agent on the other end of the pipe: answers the handshake, and lets a test
    send what the CLI would and read what XO answered."""

    def __init__(self, *, models=None, interject=None):
        self.interject = {"status": "queued"} if interject is None else interject
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.exited = asyncio.Event()
        self.prompt_id = None
        self.answers: dict[int, asyncio.Future] = {}
        self.calls: list[dict] = []
        self.models = models
        self._next = 1000

    # ProcessHandle
    async def lines(self):
        while True:
            line = await self.inbox.get()
            if line is None:
                return
            yield line

    async def stderr(self):
        return
        yield

    async def wait(self):
        await self.exited.wait()
        return 0

    async def terminate(self, grace_s):
        self.exited.set()
        self.inbox.put_nowait(None)

    async def write(self, data: bytes):
        msg = json.loads(data)
        self.sent.append(msg)
        method = msg.get("method")
        if method == "initialize":
            self.reply(msg["id"], {"protocolVersion": 1, "authMethods": [{"id": "login"}]})
        elif method == "session/new":
            self.reply(msg["id"], {"sessionId": SID, **({"models": self.models} if self.models else {})})
        elif method == "session/prompt":
            self.prompt_id = msg["id"]
        elif method == "_x.ai/interject":
            self.calls.append(msg)
            self.reply(msg["id"], self.interject)
        elif method is None and msg.get("id") in self.answers:
            self.answers.pop(msg["id"]).set_result(msg)

    # what the CLI sends
    def push(self, msg: dict) -> None:
        self.inbox.put_nowait(json.dumps({"jsonrpc": "2.0", **msg}))

    def reply(self, key, result) -> None:
        self.push({"id": key, "result": result})

    def update(self, update: dict) -> None:
        self.push({"method": "session/update", "params": {"sessionId": SID, "update": update}})

    def request(self, method: str, params: dict) -> asyncio.Future:
        self._next += 1
        answer = asyncio.get_running_loop().create_future()
        self.answers[self._next] = answer
        self.push({"id": self._next, "method": method, "params": params})
        return answer

    def finish(self, result=None) -> None:
        self.reply(self.prompt_id, {"stopReason": "end_turn", **(result or {})})


class FakeHost:
    def __init__(self, *, decision=None, answer=None):
        self.decision = decision
        self.answer = answer
        self.requests: list[c.PermissionRequest] = []
        self.questions: list[c.Question] = []

    async def decide_permission(self, req):
        self.requests.append(req)
        if self.decision is None:
            await asyncio.Event().wait()                  # the person hasn't answered
        return self.decision

    async def answer_question(self, q):
        self.questions.append(q)
        return self.answer


class AcpSessionTests(unittest.IsolatedAsyncioTestCase):
    async def open(self, agent=None, *, host=None, models=None, interject=None):
        cli = FakeCli(models=models, interject=interject)
        dialect = load_runtime(agent) if agent else None
        session = AcpSession("cse_1", c.SessionSpec(cwd="/work"), host or FakeHost(), None, cli, dialect=dialect)
        self.addAsyncCleanup(session.close, 1)
        await session.start(None, None, "1.0", {}, identity="login")
        return cli, session

    async def events(self, session, until):
        seen = []
        async with asyncio.timeout(5):
            async for event in session.events():
                seen.append(event)
                if isinstance(event, until):
                    return seen
        return seen

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_a_permission_request_without_its_input_is_filled_from_its_tool_call(self):
        host = FakeHost(decision=c.PermissionDecision(allow=False, message="no"))
        cli, session = await self.open("cursor", host=host)
        await session.send(c.UserInput(text="go"))
        call = "call-1\nfc_1"
        cli.update({"sessionUpdate": "tool_call", "toolCallId": call, "title": "`rm -rf .claude`",
                    "kind": "execute", "status": "pending", "rawInput": {"command": "rm -rf .claude"}})
        answer = cli.request("session/request_permission", {
            "sessionId": SID, "toolCall": {"toolCallId": call, "title": "`rm -rf .claude`", "kind": "execute",
                                           "status": "pending", "content": []},
            "options": [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]})
        reply = await asyncio.wait_for(answer, 5)
        self.assertEqual(reply["result"], {"outcome": {"outcome": "selected", "optionId": "r"}})
        self.assertEqual((host.requests[0].kind, host.requests[0].command), ("command", "rm -rf .claude"))

    async def test_a_tool_titled_by_its_machine_name_gets_a_plain_title_and_target(self):
        cli, session = await self.open("grok_build")
        await session.send(c.UserInput(text="go"))
        for key, name, kind, args in (("t1", "run_terminal_command", "execute", {"command": "ls"}),
                                      ("t2", "list_dir", "list", {"target_directory": "/work/src"})):
            cli.update({"sessionUpdate": "tool_call", "toolCallId": key, "title": name, "kind": None,
                        "_meta": {"x.ai/tool": {"name": name, "kind": kind, "label": "Run Command"}},
                        "rawInput": args})
        await self.settle()
        cli.finish()
        started = [e for e in await self.events(session, c.TurnFinished) if isinstance(e, c.ToolStarted)]
        self.assertEqual([(e.tool, e.kind, e.title, e.target) for e in started],
                         [("run_terminal_command", "command", "Run `ls`", "ls"),
                          ("list_dir", "list", "List `/work/src`", "/work/src")])

    async def test_a_cli_s_own_tool_names_are_reviewed_by_kind_through_its_hook(self):
        host = FakeHost(decision=c.PermissionDecision(allow=True))
        _cli, session = await self.open("grok_build", host=host)
        await session.send(c.UserInput(text="go"))
        self.assertTrue(await session.tool_hook({"tool_name": "search_replace", "tool_use_id": "t1",
                                                 "tool_input": {"file_path": "/work/a.py", "old_string": "a",
                                                                "new_string": "b"}}))
        self.assertTrue(await session.tool_hook({"tool_name": "ask_user_question", "tool_input": {"questions": []}}))
        self.assertEqual([(r.kind, r.paths) for r in host.requests], [("edit_file", ("/work/a.py",))])

    async def test_stop_answers_an_open_permission_request_cancelled(self):
        cli, session = await self.open("cursor")
        await session.send(c.UserInput(text="go"))
        answer = cli.request("session/request_permission", {
            "sessionId": SID, "toolCall": {"toolCallId": "t1", "kind": "execute", "rawInput": {"command": "ls"}},
            "options": [{"optionId": "a", "kind": "allow_once"}]})
        await self.settle()
        self.assertEqual(session.state, "waiting_host")
        stopping = asyncio.create_task(session.interrupt(grace_s=5))
        reply = await asyncio.wait_for(answer, 5)          # answered before the CLI ends its turn
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        cli.reply(cli.prompt_id, {"stopReason": "cancelled"})
        await stopping
        self.assertEqual(session.state, "idle")

    async def test_grok_s_question_is_put_to_the_person_and_answered_with_its_own_labels(self):
        host = FakeHost(answer={"0": "Blue"})
        cli, session = await self.open("grok_build", host=host)
        await session.send(c.UserInput(text="go"))
        reply = await asyncio.wait_for(cli.request("_x.ai/ask_user_question", {
            "sessionId": SID, "toolCallId": "q1", "mode": "default",
            "questions": [{"question": "Which colour?", "multiSelect": False,
                           "options": [{"label": "Red", "description": "warm"},
                                       {"label": "Blue (Recommended)", "description": "cool"}]}]}), 5)
        self.assertEqual(reply["result"], {"outcome": "accepted", "answers": {"Which colour?": "Blue (Recommended)"}})
        offered = host.questions[0].questions[0]["options"]
        self.assertEqual([(o["label"], o.get("recommended", False)) for o in offered],
                         [("Red", False), ("Blue", True)])

    async def test_an_unanswered_question_is_cancelled(self):
        cli, session = await self.open("grok_build", host=FakeHost(answer={}))
        await session.send(c.UserInput(text="go"))
        reply = await asyncio.wait_for(cli.request("_x.ai/ask_user_question", {
            "sessionId": SID, "questions": [{"question": "Which?", "options": [{"label": "A"}]}]}), 5)
        self.assertEqual(reply["result"], {"outcome": "cancelled"})

    async def test_a_method_no_runtime_declares_is_still_refused(self):
        cli, session = await self.open("cursor")
        await session.send(c.UserInput(text="go"))
        reply = await asyncio.wait_for(cli.request("_x.ai/ask_user_question", {"questions": []}), 5)
        self.assertEqual(reply["error"]["code"], -32601)
        reply = await asyncio.wait_for(cli.request("cursor/ask_question", {"questions": []}), 5)
        self.assertEqual(reply["error"]["code"], -32601)

    async def test_a_message_sent_mid_turn_joins_it_where_the_cli_takes_one(self):
        cli, session = await self.open("grok_build")
        turn = await session.send(c.UserInput(text="go"))
        self.assertEqual(await session.send(c.UserInput(text="also this", context="[wake]")), turn)
        self.assertEqual(cli.calls[0]["params"], {"sessionId": SID, "text": "[wake]\n\nalso this"})
        cursor_cli, cursor = await self.open("cursor")
        await cursor.send(c.UserInput(text="go"))
        with self.assertRaises(c.RuntimeFailure):
            await cursor.send(c.UserInput(text="also this"))
        self.assertEqual(load_runtime("grok_build").features().features["send_while_running"].behaviour, "steered")
        self.assertEqual(load_runtime("cursor").features().features["send_while_running"].behaviour, "queued")

    async def test_a_message_the_cli_doesnt_take_mid_turn_fails_as_retryable_and_leaves_the_turn_running(self):
        """Round 6: a refused steer raised a non-retryable failure the engine held for the person's next
        message; it is retryable, and the engine keeps the message for the turn's end."""
        cli, session = await self.open("grok_build", interject={"status": "rejected"})
        await session.send(c.UserInput(text="go"))
        with self.assertRaises(c.RuntimeFailure) as failed:
            await session.send(c.UserInput(text="also this"))
        self.assertTrue(failed.exception.retryable)
        self.assertEqual(session.state, "running")
        self.assertEqual(len(cli.calls), 1)

    async def test_the_cli_s_current_model_is_reported_when_xo_chose_none(self):
        _cli, session = await self.open(models={"currentModelId": "grok-4.7", "availableModels": []})
        started = await self.events(session, c.SessionStarted)
        self.assertEqual(started[-1].model, "grok-4.7")

    def test_usage_reads_both_spellings_of_cache_writes_and_reasoning(self):
        grok = usage_of({"inputTokens": 1000, "outputTokens": 50, "cachedReadTokens": 600,
                         "cacheCreationTokens": 100, "reasoningTokens": 30})
        agy = usage_of({"inputTokens": 1000, "outputTokens": 50, "cachedReadTokens": 600,
                        "cachedWriteTokens": 100, "thoughtTokens": 30})
        self.assertEqual(grok, agy)
        self.assertEqual((grok.input, grok.cache_read, grok.cache_write, grok.reasoning), (300, 600, 100, 30))
        self.assertIsNone(usage_of({"totalTokens": 5}))


if __name__ == "__main__":
    unittest.main()
