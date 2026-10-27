"""``auto`` on a runtime whose CLI can't decide for itself (§0.80, §0.122).

A runtime lists ``permissions.auto`` as ``emulated`` when its CLI's own
safety check can't run (codex here: its reviewer needs its sandbox). Then XO's
policy decides, as a CLI's own auto would: what the policy would ask about
runs unasked if it is routine or notable; a destructive action, a read that
may touch credentials and anything XO can't see through still ask the person
(as on claude, whose auto sends what its classifier won't allow to XO). A
thread runs in such an ``auto`` too. Driven through the engine with claude's
fake CLI and a runtime whose feature list says ``emulated``.
"""

from __future__ import annotations

import dataclasses
import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.adapters.claude_code.runtime import support as claude_support
from services.cowork_agent.storm.presets.common import thread_mode
from tests.fakes import claude
from tests.storm_engine_kit import SID, Desk, Kit, message

EMULATED = c.FeatureSupport(support="emulated", note="the test's: XO's policy decides")


class EmulatedAutoRuntime(ClaudeRuntime):
    """claude's runtime with its ``auto`` listed as emulated."""

    def features(self) -> c.FeatureList:
        listed = super().features()
        return dataclasses.replace(listed, features={**listed.features, "permissions.auto": EMULATED})


def bash(call_id: str, request_id: str, command: str, answer: str) -> list[dict]:
    args = {"command": command}
    return [claude.tool_call(SID, call_id, "Bash", args, message_id=f"m_{call_id}"),
            claude.can_use_tool(request_id, "Bash", args, tool_use_id=call_id),
            claude.permission_answer(request_id, answer),
            claude.tool_result(SID, call_id, "ok" if answer == "allow" else "denied", is_error=answer != "allow")]


class EmulatedAutoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.desk = Desk()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    async def run_turn(self, steps: list[dict], *, mode: str = "auto", orchestration: str = "none",
                       runtime: ClaudeRuntime | None = None) -> str:
        kit = self.kit
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID), *steps,
                      *claude.text_turn(SID, "done"), claude.idle(SID)])
        engine = await kit.boot_with(runtime or EmulatedAutoRuntime(claude_home=kit.home / ".claude"), desk=self.desk)
        sid = await kit.session(permissions=c.PermissionPolicy(mode=mode), orchestration=orchestration)
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        return sid

    def answer(self, request_id: str) -> dict:
        (run,) = self.kit.runs()
        line = next(line for line in run["stdin"] if line.get("type") == "control_response"
                    and line["response"].get("request_id") == request_id)
        return line["response"]["response"]

    async def test_notable_runs_unasked_and_destructive_still_asks(self) -> None:
        kit = self.kit
        sid = None
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      *bash("t1", "p1", "npm install left-pad", "allow"),
                      *bash("t2", "p2", "curl -sI https://example.org", "allow"),
                      *bash("t3", "p3", "rm -rf /tmp/build-cache", "deny"),
                      *claude.text_turn(SID, "done"), claude.idle(SID)])
        engine = await kit.boot_with(EmulatedAutoRuntime(claude_home=kit.home / ".claude"), desk=self.desk)
        sid = await kit.session(permissions=c.PermissionPolicy(mode="auto"))
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("review.opened", session_id=sid)
        self.assertEqual(self.answer("p1")["behavior"], "allow")     # neither asked anybody
        self.assertEqual(self.answer("p2")["behavior"], "allow")
        (item, ask), = self.desk.opened                              # only the destructive one
        self.assertEqual(ask.request.command, "rm -rf /tmp/build-cache")

    async def test_a_credential_read_still_asks(self) -> None:
        kit = self.kit
        read = {"command": "cat ~/.ssh/id_rsa"}
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      claude.tool_call(SID, "t1", "Bash", read, message_id="m_t1"),
                      claude.can_use_tool("p1", "Bash", read, tool_use_id="t1"),
                      claude.permission_answer("p1", "deny"), claude.tool_result(SID, "t1", "denied", is_error=True),
                      *claude.text_turn(SID, "done"), claude.idle(SID)])
        engine = await kit.boot_with(EmulatedAutoRuntime(claude_home=kit.home / ".claude"), desk=self.desk)
        sid = await kit.session(permissions=c.PermissionPolicy(mode="auto"))
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("review.opened", session_id=sid)
        (item, ask), = self.desk.opened
        self.assertEqual(ask.request.command, "cat ~/.ssh/id_rsa")

    async def test_a_native_auto_and_other_modes_are_untouched(self) -> None:
        """claude's own auto is native: what reaches XO is asked as before;
        so is ``ask`` mode on an emulated runtime."""
        for runtime, mode in ((None, "auto"), ("emulated", "ask")):
            with self.subTest(runtime=runtime, mode=mode):
                await self.kit.close()       # each case its own kit; the last one closes in tearDown
                kit = self.kit = Kit()
                self.desk = Desk()
                rt = EmulatedAutoRuntime(claude_home=kit.home / ".claude") if runtime else \
                    ClaudeRuntime(claude_home=kit.home / ".claude")
                kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                              claude.tool_call(SID, "t1", "Bash", {"command": "npm install left-pad"}, message_id="m"),
                              claude.can_use_tool("p1", "Bash", {"command": "npm install left-pad"},
                                                  tool_use_id="t1"),
                              claude.permission_answer("p1", "deny"),
                              claude.tool_result(SID, "t1", "denied", is_error=True),
                              *claude.text_turn(SID, "done"), claude.idle(SID)])
                engine = await kit.boot_with(rt, desk=self.desk)
                sid = await kit.session(permissions=c.PermissionPolicy(mode=mode))
                await engine.deliver(sid, message(1))
                await kit.recorder.seen("review.opened", session_id=sid)
                self.assertEqual(len(self.desk.opened), 1)

    def test_a_thread_runs_in_an_emulated_auto(self) -> None:
        listed = claude_support.features("2.1.301")
        emulated = dataclasses.replace(listed, features={**listed.features, "permissions.auto": EMULATED})
        missing = dataclasses.replace(listed, features={k: v for k, v in listed.features.items()
                                                        if k != "permissions.auto"})
        self.assertEqual([thread_mode(f) for f in (listed, emulated, missing)], ["auto", "auto", "accept_edits"])

if __name__ == "__main__":
    unittest.main()
