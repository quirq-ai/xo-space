"""The fake CLIs speak their protocols (storm ch 06 §1).

Each fake is started through the real executor, as a runtime will start it,
and driven both ways: what it prints, what it records, and that a host reply
reaches it. A step that doesn't match must fail loudly, never pass silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path

from tests.fakes import MISMATCH_EXIT, expect, install, scenario, write
from tests.fakes import agy, claude, codex
from tests.fakes.host import ScriptedHost

TIMEOUT = 10


class _FakeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.host = ScriptedHost()
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "XO_MARKER": "1"}

    async def start(self, fake, *args: str):
        proc = await self.host.launcher.spawn([str(fake.path), *args], cwd=str(self.root), env=self.env,
                                              label=fake.path.name)
        self.addAsyncCleanup(proc.terminate, 2.0)
        return proc, proc.lines()

    async def send(self, proc, message: dict) -> None:
        await proc.write((json.dumps(message) + "\n").encode())

    async def read(self, lines) -> dict:
        return json.loads(await asyncio.wait_for(anext(lines), TIMEOUT))

    async def read_until(self, lines, predicate) -> list[dict]:
        seen = []
        while True:
            seen.append(await self.read(lines))
            if predicate(seen[-1]):
                return seen


class EngineTests(_FakeTest):
    async def test_model_free_invocation_is_answered_and_recorded(self) -> None:
        fake = install("claude", self.bin, scenario([], argv=[claude.version()]))
        proc, lines = await self.start(fake, "--version")
        self.assertEqual(await asyncio.wait_for(anext(lines), TIMEOUT), claude.VERSION.encode())
        self.assertEqual(await proc.wait(), 0)
        run = fake.invocations()[0]
        self.assertEqual((run["argv"], run["cwd"], run["env"]["XO_MARKER"], run["exit"]),
                         (["--version"], str(self.root), "1", 0))

    async def test_a_step_that_does_not_match_fails_loudly(self) -> None:
        fake = install("claude", self.bin, scenario([expect({"type": "user"})]))
        proc, _ = await self.start(fake)
        await self.send(proc, {"type": "control_request", "request": {"subtype": "interrupt"}})
        self.assertEqual(await asyncio.wait_for(proc.wait(), TIMEOUT), MISMATCH_EXIT)
        self.assertIn("expected", proc.stderr_tail())
        self.assertIn("mismatch", fake.invocations()[0])

    async def test_bound_values_are_substituted_and_stdin_recorded(self) -> None:
        fake = install("codex", self.bin, scenario(codex.call("thread/start", {"thread": {"id": "th_1"}})))
        proc, lines = await self.start(fake)
        await self.send(proc, {"id": 42, "method": "thread/start", "params": {"cwd": "/w"}})
        self.assertEqual(await self.read(lines), {"id": 42, "result": {"thread": {"id": "th_1"}}})
        await proc.close_stdin()
        self.assertEqual(await asyncio.wait_for(proc.wait(), TIMEOUT), 0)
        self.assertEqual(fake.invocations()[0]["stdin"], [{"id": 42, "method": "thread/start", "params": {"cwd": "/w"}}])

    async def test_a_write_step_writes_in_the_working_folder(self) -> None:
        fake = install("claude", self.bin, scenario([write(".claude/settings.json", '{"hooks": {}}'), {"exit": 0}]))
        proc, _ = await self.start(fake)
        self.assertEqual(await asyncio.wait_for(proc.wait(), TIMEOUT), 0)
        self.assertEqual((self.root / ".claude" / "settings.json").read_text(encoding="utf-8"), '{"hooks": {}}')


    async def test_runs_that_overlap_never_interleave(self) -> None:
        """Every record line names its process: a line written by the first
        run after the second started is still filed under the first."""
        fake = install("codex", self.bin, scenario([*codex.call("one", {"n": 1}), *codex.call("two", {"n": 2})]))
        first, first_lines = await self.start(fake, "first")
        await self.send(first, {"id": 1, "method": "one"})
        await self.read(first_lines)                       # its argv is recorded before it answers
        second, second_lines = await self.start(fake, "second")
        await self.send(second, {"id": 1, "method": "one"})
        await self.read(second_lines)
        await self.send(first, {"id": 2, "method": "two"})
        await self.read(first_lines)
        await self.send(second, {"id": 2, "method": "two"})
        await self.read(second_lines)
        for proc in (first, second):
            await proc.close_stdin()
            self.assertEqual(await asyncio.wait_for(proc.wait(), TIMEOUT), 0)
        runs = fake.invocations()
        self.assertEqual([run["argv"] for run in runs], [["first"], ["second"]])      # start order
        for run in runs:
            self.assertEqual([line["method"] for line in run["stdin"]], ["one", "two"])
            self.assertEqual(run["exit"], 0)
            self.assertNotIn("pid", run)                                            # the API is unchanged


    def test_a_line_still_being_written_is_not_read(self) -> None:
        """A test reads the record while fakes still write to it."""
        fake = install("claude", self.bin, scenario([]))
        whole = json.dumps({"pid": 1, "argv": ["claude", "-p"], "cwd": "/w", "env": {}})
        fake.record.write_text(whole + "\n" + whole[:20], encoding="utf-8")
        self.assertEqual([run["argv"] for run in fake.invocations()], [["claude", "-p"]])


class ClaudeFakeTests(_FakeTest):
    async def test_a_turn_with_a_permission_check(self) -> None:
        fake = install("claude", self.bin, scenario([
            *claude.host_control("initialize"),
            claude.init("sess-1"),
            claude.user_message(),
            claude.can_use_tool("req-1", "Bash", {"command": "git push"}),
            claude.permission_answer("req-1", "deny"),
            *claude.text_turn("sess-1", "I could not push."),
            claude.idle("sess-1"),
        ]))
        proc, lines = await self.start(fake, "-p", "--input-format", "stream-json")
        await self.send(proc, {"type": "control_request", "request_id": "c1", "request": {"subtype": "initialize"}})
        self.assertEqual((await self.read(lines))["response"]["request_id"], "c1")
        self.assertEqual((await self.read(lines))["subtype"], "init")
        await self.send(proc, {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "push"}]}})
        ask = await self.read(lines)
        self.assertEqual(ask["request"]["subtype"], "can_use_tool")
        await self.send(proc, {"type": "control_response", "response": {
            "subtype": "success", "request_id": ask["request_id"],
            "response": {"behavior": "deny", "message": "not now"}}})
        seen = await self.read_until(lines, lambda m: m.get("subtype") == "session_state_changed")
        self.assertEqual([m["type"] for m in seen][-3:], ["assistant", "result", "system"])
        self.assertEqual(seen[-2]["result"], "I could not push.")


class CodexFakeTests(_FakeTest):
    async def test_handshake_turn_and_an_approval(self) -> None:
        fake = install("codex", self.bin, scenario([
            *codex.handshake(),
            *codex.thread_start("th_1"),
            *codex.turn_start("th_1", "tu_1"),
            codex.command_approval(900, "th_1", "tu_1", "it_1", "npm test"),
            codex.approval_answer(900, "decline"),
            *codex.agent_message("th_1", "tu_1", "Skipped the tests."),
            codex.turn_completed("th_1", "tu_1"),
        ]))
        proc, lines = await self.start(fake, "app-server", "--listen", "stdio://")
        await self.send(proc, {"id": 1, "method": "initialize", "params": {}})
        self.assertEqual((await self.read(lines))["id"], 1)
        await self.send(proc, {"method": "initialized"})
        await self.send(proc, {"id": 2, "method": "thread/start", "params": {}})
        self.assertEqual((await self.read(lines))["result"]["thread"]["id"], "th_1")
        self.assertEqual((await self.read(lines))["method"], "thread/started")
        await self.send(proc, {"id": 3, "method": "turn/start", "params": {"threadId": "th_1"}})
        self.assertEqual((await self.read(lines))["result"]["turn"]["id"], "tu_1")
        self.assertEqual((await self.read(lines))["method"], "thread/status/changed")
        self.assertEqual((await self.read(lines))["method"], "turn/started")
        approval = await self.read(lines)
        self.assertEqual(approval["method"], "item/commandExecution/requestApproval")
        await self.send(proc, {"id": approval["id"], "result": {"decision": "decline"}})
        seen = await self.read_until(lines, lambda m: m.get("method") == "turn/completed")
        self.assertEqual([m["method"] for m in seen],
                         ["item/started", "item/agentMessage/delta", "item/completed", "turn/completed"])


class AgyFakeTests(_FakeTest):
    async def test_a_turn_then_sigint_ends_the_next_one_and_exits(self) -> None:
        fake = install("agy", self.bin, scenario([
            agy.init("conv-1"),
            agy.user_message(),
            agy.step(0, "user_input", conv="conv-1"), *agy.text(1, "Done.", conv="conv-1"),
            agy.result("Done.", conv="conv-1"),
            agy.stops_on_sigint("conv-1"),
            agy.user_message(),
            agy.step(3, "user_input", conv="conv-1"),
            {"sleep": 30},
        ]))
        proc, lines = await self.start(fake, "--input-format", "stream-json", "--output-format", "stream-json")
        self.assertEqual((await self.read(lines))["conversation_id"], "conv-1")
        await self.send(proc, {"event": "user", "message": {"content": "hello"}})
        seen = await self.read_until(lines, lambda m: m.get("event") == "result")
        self.assertEqual(seen[-1]["result"]["response"], "Done.")
        await self.send(proc, {"event": "user", "message": {"content": "a long task"}})
        self.assertEqual((await self.read(lines))["step_update"]["step_type"], "user_input")
        await proc.signal(signal.SIGINT)
        self.assertEqual((await self.read(lines))["result"]["error"], "interrupted")
        self.assertEqual(await asyncio.wait_for(proc.wait(), TIMEOUT), 1)
        self.assertEqual(fake.invocations()[0]["signals"], ["SIGINT"])


if __name__ == "__main__":
    unittest.main()
