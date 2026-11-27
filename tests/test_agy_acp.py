"""The agy runtime on ``agy acp`` (the Agent Client Protocol, agy 1.5.15+)
against its fake: the transport choice, a turn's stream, XO's hook in agy's
Auto, agy's own asks, the sandbox fallback, interrupt, live updates, resume,
sub-agents and plans.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.antigravity.runtime import acp_session, config, core, support
from services.cowork_agent.adapters.antigravity.runtime.acp_wire import AcpWire, acp_usage, command_result, diff_stat
from services.cowork_agent.adapters.antigravity.runtime.core import AgyRuntime
from tests.fakes import agy, expect, install as install_fake, scenario
from tests.fakes.agy import acp
from tests.fakes.host import ScriptedHost
from tests.test_agy_runtime import wait_for
from tests.test_agy_wire import CHILD

SID = acp.SID



def settings(**values: str):
    """agy's settings.json, as these tests want it."""
    return mock.patch.object(config, "load", return_value=dict(values))


class TransportTests(unittest.TestCase):
    def test_acp_is_chosen_only_when_asked_for_on_a_new_enough_agy(self) -> None:
        with settings(transport="acp"):
            self.assertEqual(core.transport_for("1.5.15"), "acp")
            self.assertEqual(core.transport_for("agy 1.6.0 (abc)"), "acp")
            self.assertEqual(core.transport_for("1.4.10"), "print")
            self.assertEqual(core.transport_for(None), "print")
            schema = c.SessionSpec(cwd="/w", output_schema={"type": "object"})
            self.assertEqual(core.transport_for("1.5.15", schema), "print")
        with settings(transport="print"):
            self.assertEqual(core.transport_for("1.5.15"), "print")

    def test_acp_is_the_default_and_an_old_agy_keeps_print(self) -> None:
        for values in ({}, {"transport": "carrier pigeon"}):
            with settings(**values):
                self.assertEqual(core.transport_for("1.5.15"), "acp")
                self.assertEqual(core.transport_for("1.4.10"), "print")

    def test_the_shipped_settings_choose_acp_and_check_the_sandbox(self) -> None:
        self.assertEqual((config.transport(), config.terminal_sandbox()), ("acp", "auto"))

    def test_features_keep_their_keys_on_either_transport(self) -> None:
        printed = support.features("1.5.15", auth_env=core.AUTH_VARS, transport="print")
        spoken = support.features("1.5.15", auth_env=core.AUTH_VARS, transport="acp")
        self.assertEqual(set(printed.features), set(spoken.features))
        self.assertEqual(spoken.features["permissions.auto"].support, "emulated")    # threads still pick auto
        self.assertEqual(spoken.features["interrupt"].behaviour, "stops_runs")
        self.assertEqual(spoken.features["update.model"].support, "native")


class WireTests(unittest.TestCase):
    def test_usage_takes_the_context_last_reported(self) -> None:
        used = acp_usage({"inputTokens": 11881, "outputTokens": 143, "cachedReadTokens": 7566}, (19590, 1048576))
        self.assertEqual((used.input, used.output, used.cache_read, used.context_tokens, used.context_window),
                         (11881, 143, 7566, 19590, 1048576))
        self.assertIsNone(acp_usage("nope", None))

    def test_a_diff_counts_its_lines(self) -> None:
        stat, size = diff_stat([{"path": "/w/a", "oldText": "a\nb\n", "newText": "a\nc\nd\n"}])
        self.assertEqual((stat, size), ((2, 1), 6))
        self.assertEqual(diff_stat([]), (None, None))

    def test_a_command_is_summed_up_without_agys_header(self) -> None:
        head = "Created At: 2026-11-23T16:11:06Z\nCompleted At: 2026-11-23T16:11:07Z\n"
        ran = head + "Command exited with code 0.\n\nCommand output:\nhello.txt\nnotes.md\n"
        self.assertEqual(command_result(ran, {"output": ran, "exit_code": 0}), (False, "hello.txt"))
        quiet = head + "Command exited with code 0.\n"
        self.assertEqual(command_result(quiet, {"output": quiet}), (False, "Command exited with code 0."))
        self.assertEqual(command_result("12 lines", None), (False, "12 lines"))   # not a command: as it is
        self.assertEqual(command_result("", None), (False, None))

    def test_a_command_that_failed_or_never_ran_is_an_error(self) -> None:
        head = "Created At: 2026-11-23T16:11:06Z\n"
        failed = head + "Command exited with code 2.\n\nCommand output:\nls: cannot access 'x'\n"
        self.assertEqual(command_result(failed, {"exit_code": 2}), (True, "exit 2: ls: cannot access 'x'"))
        self.assertEqual(command_result(failed, None), (True, "exit 2: ls: cannot access 'x'"))
        sandbox = head + "Command failed to run: connecting to sandbox server: refused\n\nNo output from the command."
        self.assertEqual(command_result(sandbox, {"output": sandbox}),
                         (True, "Command failed to run: connecting to sandbox server: refused"))

    def test_a_completed_command_that_failed_finishes_as_an_error(self) -> None:
        wire = AcpWire(cwd="/w")
        wire.update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "List", "kind": "execute",
                     "status": "in_progress", "rawInput": {"CommandLine": "ls"}})
        out = "Command exited with code 1.\n\nCommand output:\nboom\n"
        outs = wire.update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
                            "rawOutput": {"output": out, "exit_code": 1}})
        finished = [o for o in outs if o.cls is c.ToolFinished]
        self.assertEqual([(o.fields["status"], o.fields["summary"]) for o in finished], [("error", "exit 1: boom")])

    def test_an_unknown_update_is_said_once(self) -> None:
        wire = AcpWire(cwd="/w")
        self.assertEqual(len(wire.update({"sessionUpdate": "hologram"})), 1)
        self.assertEqual(wire.update({"sessionUpdate": "hologram"}), [])
        self.assertEqual(wire.update({"sessionUpdate": "available_commands_update"}), [])


class AcpCase(unittest.IsolatedAsyncioTestCase):
    """One agy session on ``agy acp`` against the fake."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.home = self.root / "home"
        self.brain = self.home / ".gemini" / "antigravity-cli" / "brain"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def run_session(self, steps: list | None = None, *, runs: list | None = None, spec: dict | None = None,
                          host: ScriptedHost | None = None, drive=None, runtime: AgyRuntime | None = None,
                          sandbox: str = "on"):
        body = {"runs": runs, "argv": acp.probes()} if runs is not None else scenario(steps or [], argv=acp.probes())
        fake = install_fake("agy", self.root / "bin", body)
        host = host or ScriptedHost(decide=lambda req: c.PermissionDecision(allow=True))
        env = {"AGY_CLI_PATH": str(fake.path), "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.root / "state")}
        events: list[c.Event] = []
        with mock.patch.dict(os.environ, env), settings(transport="acp", terminal_sandbox=sandbox):
            session = await (runtime or AgyRuntime()).open(c.SessionSpec(cwd=str(self.work), **(spec or {})), host,
                                                           session_id="cse_agy_acp")

            async def consume() -> None:
                async for ev in session.events():
                    events.append(ev)
            reader = asyncio.create_task(consume())
            try:
                if drive is None:
                    await session.send(c.UserInput(text="hello"))
                    await wait_for(events, c.SessionIdle)
                else:
                    await drive(session, events)
            finally:
                await asyncio.wait_for(session.close(), 20)
                await asyncio.wait_for(reader, 20)
        mismatches = [run.get("mismatch") for run in fake.invocations() if run.get("mismatch")]
        self.assertEqual(mismatches, [], "the fake saw something it didn't expect")
        return events, host, fake


def of(events: list, kind: type) -> list:
    return [e for e in events if isinstance(e, kind)]


class TurnTests(AcpCase):
    async def test_a_turn_streams_thinking_text_tools_plan_and_usage(self) -> None:
        path = str(self.work / "a.txt")
        diff = {"path": path, "oldText": "", "newText": "hi\nthere\n"}
        events, host, fake = await self.run_session([
            *acp.started(), acp.prompt("hello"),
            *acp.think("Reading the ", "request.\nThen writing."),
            acp.update("plan", entries=[{"content": "write a.txt", "status": "in_progress", "priority": "medium"},
                                        {"content": "say so", "status": "pending", "priority": "medium"}]),
            *acp.tool("call-1", "write_file", {"Path": path, "Content": "hi\nthere\n"}, diff=diff,
                      output="Created file", kind="edit"),
            *acp.say("Wrote ", "it."), acp.update("usage_update", used=15000, size=1048576),
            acp.stop(), acp.done()])
        run = fake.invocations()[-1]
        self.assertEqual(run["argv"], ["acp"])
        sent = run["stdin"][0]
        self.assertEqual(sent["params"]["clientInfo"]["name"], "xo_space")    # never another client's name
        self.assertEqual([type(e).__name__ for e in events if not isinstance(e, (c.TextDelta, c.ThinkingDelta))],
                         ["SessionStarted", "TurnStarted", "ThinkingDone", "PlanUpdated", "ToolStarted",
                          "PermissionRequested", "PermissionResolved", "ToolFinished", "TextDone", "Usage",
                          "TurnFinished", "SessionIdle", "SessionClosed"])
        self.assertEqual(of(events, c.ThinkingDone)[0].steps, ("Reading the request.", "Then writing."))
        self.assertEqual([(i.text, i.state) for i in of(events, c.PlanUpdated)[0].items],
                         [("write a.txt", "doing"), ("say so", "todo")])
        finished = of(events, c.ToolFinished)[0]
        self.assertEqual((finished.call_id, finished.status, finished.diff_stat, finished.size),
                         ("call-1", "ok", (2, 0), 9))
        usage = of(events, c.Usage)[0].usage
        self.assertEqual((usage.input, usage.cache_read, usage.context_tokens), (1200, 300, 15000))
        turn = of(events, c.TurnFinished)[0]
        self.assertEqual((turn.status, turn.text), ("completed", "Wrote it."))
        self.assertEqual([r.call_id for r in host.permission_requests], ["call-1"])   # ask mode: XO's hook asks

    async def test_thinking_off_is_not_shown(self) -> None:
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt(), *acp.think("hmm"), *acp.say("ok"), acp.stop(), acp.done()],
            spec={"thinking": "off"})
        self.assertEqual(of(events, c.ThinkingDone), [])
        self.assertEqual(of(events, c.TurnFinished)[0].text, "ok")

    async def test_two_messages_are_two_prompts_in_order(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await session.send(c.UserInput(text="two"))
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("first"), acp.done(),
            acp.prompt("two"), *acp.say("second"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual([(e.status, e.text) for e in of(events, c.TurnFinished)],
                         [("completed", "first"), ("completed", "second")])
        self.assertEqual(len(of(events, c.SessionIdle)), 1)

    async def test_a_message_sent_mid_turn_joins_it_at_agys_next_model_call(self) -> None:
        turns = []

        async def drive(session, events) -> None:
            turns.append(await session.send(c.UserInput(text="one")))
            await wait_for(events, c.TextDelta)
            turns.append(await session.send(c.UserInput(text="two", context="[wake]")))
            await wait_for(events, c.SessionIdle)
        invocation = {"conversationId": SID, "invocationNum": 1}
        events, _, fake = await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("first "), {"sleep": 1.5},
            agy.hook("PreInvocation", invocation, bind={"steered": "injectSteps.0.userMessage"}),
            *acp.say("$steered"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual(turns[0], turns[1])                     # one turn: the message joined it
        self.assertEqual([(e.status, e.text) for e in of(events, c.TurnFinished)],
                         [("completed", "first [wake]\n\ntwo")])
        self.assertEqual(support.features("1.5.18", auth_env=(), transport="acp").features[
            "send_while_running"].behaviour, "steered")

    async def test_a_message_after_agys_last_model_call_keeps_the_turn_going(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await wait_for(events, c.TextDelta)
            await session.send(c.UserInput(text="two"))
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("first "), {"sleep": 1.5},
            agy.hook("Stop", {"conversationId": SID, "executionNum": 0, "terminationReason": "NO_TOOL_CALL",
                              "fullyIdle": True}, expect_decision="continue"),
            agy.hook("PreInvocation", {"conversationId": SID, "invocationNum": 2},
                     bind={"steered": "injectSteps.0.userMessage"}),
            *acp.say("$steered"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual([(e.status, e.text) for e in of(events, c.TurnFinished)], [("completed", "first two")])

    async def test_a_message_the_turn_ended_before_reading_runs_as_the_next_turn(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await wait_for(events, c.TextDelta)
            await session.send(c.UserInput(text="two"))
            await wait_for(events, c.TurnFinished, count=2)
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("first"), {"sleep": 1.5}, acp.done(),
            acp.prompt("two"), *acp.say("second"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual([(e.status, e.text) for e in of(events, c.TurnFinished)],
                         [("completed", "first"), ("completed", "second")])

    async def test_a_failed_prompt_fails_its_turn(self) -> None:
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt(),
            {"emit": {"jsonrpc": "2.0", "id": "$pid", "error": {"code": -32603, "message": "model overloaded"}}}])
        turn = of(events, c.TurnFinished)[0]
        self.assertEqual((turn.status, turn.error), ("failed", "model overloaded"))


class AutoTests(AcpCase):
    async def test_in_agys_auto_xos_checks_come_first_and_agy_decides_the_rest(self) -> None:
        events, host, _ = await self.run_session([
            *acp.started(), *acp.call("session/set_mode", None, sessionId=SID, modeId="auto"),
            acp.prompt(),
            *acp.tool("call-1", "write_file", {"Path": str(self.work / "a.txt"), "Content": "x"},
                      decision="ask"),
            *acp.tool("call-2", "write_file", {"Path": str(self.work / ".agents/hooks.json"),
                                                  "Content": "{}"}, decision="deny"),
            acp.pre_tool("call-3", "write_file", {"Path": str(self.home / ".gemini/settings.json"),
                                                     "Content": "{}"}, decision="deny"),
            *acp.say("done"), acp.stop(), acp.done()],
            spec={"permissions": c.PermissionPolicy(mode="auto")})
        self.assertEqual(host.permission_requests, [])
        self.assertEqual({e.call_id: e.status for e in of(events, c.ToolFinished)},
                         {"call-1": "ok", "call-2": "denied"})
        refused = [e.decision.message for e in of(events, c.PermissionResolved)]
        self.assertTrue(any(".agents/**" in r for r in refused), refused)
        self.assertTrue(any("~/.gemini/**" in r for r in refused), refused)

    async def test_what_agy_asks_is_decided_by_xos_host(self) -> None:
        def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            return c.PermissionDecision(allow=req.command != "rm -rf build", message="not that")
        events, host, _ = await self.run_session([
            *acp.started(), *acp.call("session/set_mode", None, modeId="auto"), acp.prompt(),
            acp.tool_call("call-1", "run_command", {"CommandLine": "ls /etc"}, kind="execute"),
            acp.pre_tool("call-1", "run_command", {"CommandLine": "ls /etc"}, decision="ask"),
            *acp.ask("call-1", "run_command", {"CommandLine": "ls /etc"}, rid="perm-1", chosen="allow"),
            acp.tool_done("call-1", "run_command", {"CommandLine": "ls /etc"}, output="hosts", kind="execute"),
            acp.tool_call("call-2", "run_command", {"CommandLine": "rm -rf build"}, kind="execute"),
            acp.pre_tool("call-2", "run_command", {"CommandLine": "rm -rf build"}, decision="ask"),
            *acp.ask("call-2", "run_command", {"CommandLine": "rm -rf build"}, rid="perm-2", chosen="deny"),
            acp.tool_done("call-2", "run_command", {"CommandLine": "rm -rf build"}, status="failed",
                          output="permission denied by the user", kind="execute"),
            *acp.say("ok"), acp.stop(), acp.done()],
            spec={"permissions": c.PermissionPolicy(mode="auto")}, host=ScriptedHost(decide=decide))
        self.assertEqual([(r.call_id, r.command) for r in host.permission_requests],
                         [("call-1", "ls /etc"), ("call-2", "rm -rf build")])
        self.assertEqual({e.call_id: e.status for e in of(events, c.ToolFinished)}, {"call-1": "ok", "call-2": "denied"})

    async def test_a_sandbox_that_cant_start_hands_auto_back_to_xo(self) -> None:
        runtime = AgyRuntime()
        failed = "Command failed to run: connecting to sandbox server: dial unix: no such file"

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="hello"))
            await wait_for(events, c.SessionIdle)
            notes = [e.code for e in of(events, c.Notice)]
            self.assertEqual(notes.count("agy_sandbox_unavailable"), 1)
            await session.send(c.UserInput(text="again"))
            await wait_for(events, c.SessionIdle, count=2)
        events, host, _ = await self.run_session([
            *acp.started(), *acp.call("session/set_mode", None, modeId="auto"), acp.prompt("hello"),
            acp.tool_call("call-1", "run_command", {"CommandLine": "ls"}, kind="execute"),
            acp.pre_tool("call-1", "run_command", {"CommandLine": "ls"}, decision="ask"),
            acp.tool_done("call-1", "run_command", {"CommandLine": "ls"}, status="failed", output=failed,
                          kind="execute"),
            *acp.call("session/set_mode", None, modeId="default"),           # XO's trusted auto from here on
            acp.pre_tool("call-2", "run_command", {"CommandLine": "ls"}, step=3),
            *acp.say("ok"), acp.stop(), acp.done(),
            acp.prompt("again"), acp.pre_tool("call-3", "run_command", {"CommandLine": "ls"}, step=5),
            *acp.say("ok"), acp.stop(), acp.done()],
            spec={"permissions": c.PermissionPolicy(mode="auto")}, drive=drive, runtime=runtime)
        self.assertEqual(host.permission_requests, [])
        self.assertTrue(runtime._sandbox.broken)                               # every later session knows it
        self.assertIn("sandbox", runtime._auto_probe())

    async def test_where_the_sandbox_cant_start_xo_decides_from_the_first_command(self) -> None:
        runtime = AgyRuntime()
        events, host, _ = await self.run_session([
            *acp.started(), acp.prompt("hello"),                                # agy stays in its default mode
            acp.pre_tool("call-1", "run_command", {"CommandLine": "ls"}),       # XO's trusted auto runs it
            *acp.say("ok"), acp.stop(), acp.done()],
            spec={"permissions": c.PermissionPolicy(mode="auto")}, runtime=runtime, sandbox="off")
        self.assertEqual(host.permission_requests, [])
        self.assertEqual([e.code for e in of(events, c.Notice)], [])            # nothing failed, nothing to say
        self.assertTrue(runtime._sandbox.broken)
        self.assertIn("sandbox", runtime._auto_probe())


class SandboxCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def unshare(self, code: int) -> dict[str, str]:
        exe = self.bin / "unshare"
        exe.write_text(f"#!/bin/sh\nexit {code}\n")
        exe.chmod(0o755)
        return {"PATH": str(self.bin)}

    async def test_the_setting_wins(self) -> None:
        for value, want in (("on", True), ("off", False)):
            with mock.patch.dict(os.environ, self.unshare(1)), settings(terminal_sandbox=value):
                self.assertIs(await acp_session.sandbox_can_start(), want)

    async def test_a_refused_mount_means_no_sandbox(self) -> None:
        with mock.patch.dict(os.environ, self.unshare(1)), settings(terminal_sandbox="auto"), \
                mock.patch.object(acp_session.sys, "platform", "linux"):
            self.assertIs(await acp_session.sandbox_can_start(), False)
            state = acp_session.SandboxState()
            await state.check()
            self.assertTrue(state.broken)
        with mock.patch.dict(os.environ, self.unshare(0)), settings(), \
                mock.patch.object(acp_session.sys, "platform", "linux"):
            self.assertIs(await acp_session.sandbox_can_start(), True)

    async def test_no_telling_leaves_it_to_the_first_command(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), settings(), \
                mock.patch.object(acp_session.sys, "platform", "linux"):
            self.assertIsNone(await acp_session.sandbox_can_start())           # no unshare
        with settings(), mock.patch.object(acp_session.sys, "platform", "darwin"):
            self.assertIsNone(await acp_session.sandbox_can_start())
            state = acp_session.SandboxState()
            await state.check()
            self.assertFalse(state.broken)


class ControlTests(AcpCase):
    async def test_an_interrupt_cancels_the_turn_and_agy_stays_up(self) -> None:
        async def drive(session, events) -> None:
            # "queued" must be queued behind "one", not steered into it: hold the prompt loop until both are in
            # (on a busy machine "one"'s turn could otherwise begin while the second send reads the login).
            gate, sync = asyncio.Event(), session._sync_settings

            async def held() -> None:
                await gate.wait()
                await sync()
            with mock.patch.object(session, "_sync_settings", held):
                await session.send(c.UserInput(text="one"))
                await session.send(c.UserInput(text="queued"))
                gate.set()
                await wait_for(events, c.ToolStarted)
                await session.interrupt()
                await wait_for(events, c.SessionIdle)
                await session.send(c.UserInput(text="two"))
                await wait_for(events, c.SessionIdle, count=2)
        events, _, fake = await self.run_session([
            *acp.started(), acp.prompt("one"),
            acp.tool_call("call-1", "run_command", {"CommandLine": "sleep 60"}, kind="execute"),
            acp.pre_tool("call-1", "run_command", {"CommandLine": "sleep 60"}),
            expect({"method": "session/cancel", "params": {"sessionId": SID}}),
            acp.tool_done("call-1", "run_command", {"CommandLine": "sleep 60"}, status="failed", output="cancelled",
                          kind="execute"),
            acp.done("cancelled"),
            acp.prompt("two"), *acp.say("back"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual([(e.status, e.error) for e in of(events, c.TurnFinished)],
                         [("interrupted", "agy was stopped before it started this message"), ("interrupted", None),
                          ("completed", None)])
        self.assertEqual(len([r for r in fake.invocations() if r["argv"] == ["acp"]]), 1)   # agy stayed up

    async def test_a_message_steered_into_a_stopped_turn_goes_with_the_next_one(self) -> None:
        """Round 6: an interrupt dropped what was sent into the turn that agy hadn't read yet; the engine
        had counted it as taken by that turn, so it was lost."""
        kept = []

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await wait_for(events, c.TextDelta)
            await session.send(c.UserInput(text="two", context="[wake]"))
            await session.interrupt()
            await wait_for(events, c.SessionIdle)
            kept.extend(session._notes)
        await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("first "),
            expect({"method": "session/cancel", "params": {"sessionId": SID}}),
            acp.done("cancelled")], drive=drive)
        self.assertEqual(kept, [f"{acp_session.UNREAD}\n\n[wake]\n\ntwo"])

    async def test_model_effort_and_mode_change_without_a_restart(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await wait_for(events, c.SessionIdle)
            report = await session.update(model="claude-sonnet-4-6", effort="medium", permission_mode="accept_edits")
            self.assertEqual({i.support for i in report.items}, {"native"})
            await session.send(c.UserInput(text="two"))
            await wait_for(events, c.SessionIdle, count=2)
        _, _, fake = await self.run_session([
            *acp.started(), acp.prompt("one"), *acp.say("one"), acp.stop(), acp.done(),
            *acp.call("session/set_model", None, sessionId=SID, modelId="claude-sonnet-4-6"),
            *acp.call("session/set_config_option", {"configOptions": []}, configId="thought_level", value="medium"),
            acp.prompt("two"), *acp.say("two"), acp.stop(), acp.done()], drive=drive)
        self.assertEqual(len([r for r in fake.invocations() if r["argv"] == ["acp"]]), 1)

    async def test_an_effort_the_model_cant_take_is_said_once(self) -> None:
        events, _, _ = await self.run_session([
            *acp.initialize(), *acp.call("session/new", acp.opened(model="claude-sonnet-4-6", level=None)),
            *acp.call("session/set_model", None, modelId="gemini-3.8-flash-high"),
            acp.prompt(), *acp.say("ok"), acp.stop(), acp.done()],
            spec={"model": "gemini-3.8-flash-high", "effort": "high"})
        self.assertEqual([e.code for e in of(events, c.Notice)], ["effort_unsupported"])

    async def test_resume_loads_the_conversation_without_replaying_it(self) -> None:
        events, _, _ = await self.run_session([*acp.loaded(), acp.prompt(), *acp.say("new"), acp.stop(), acp.done()],
                                              spec={"resume": SID})
        self.assertEqual([e.text for e in of(events, c.TextDone)], ["new"])

    async def test_a_conversation_agy_doesnt_have_is_not_replaced_silently(self) -> None:
        fake = install_fake("agy", self.root / "bin", scenario(
            [*acp.initialize(), *acp.refuse("session/load", "Session not found")], argv=acp.probes()))
        env = {"AGY_CLI_PATH": str(fake.path), "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.root / "state"),
               }
        with mock.patch.dict(os.environ, env), settings(transport="acp"):
            with self.assertRaises(c.RuntimeFailure) as caught:
                await AgyRuntime().open(c.SessionSpec(cwd=str(self.work), resume=SID), ScriptedHost(),
                                        session_id="cse_agy_missing")
        self.assertEqual(caught.exception.code, "session_not_found")

    async def test_an_old_agy_runs_on_print_mode(self) -> None:
        fake = install_fake("agy", self.root / "bin", scenario([agy.init(), agy.user_message(),
                                                                *agy.text_turn("hi")], argv=agy.probes()))
        env = {"AGY_CLI_PATH": str(fake.path), "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.root / "state"),
               }
        with mock.patch.dict(os.environ, env), settings(transport="acp"):
            session = await AgyRuntime().open(c.SessionSpec(cwd=str(self.work)), ScriptedHost(), session_id="cse_old")
            self.assertNotIsInstance(session, __import__(
                "services.cowork_agent.adapters.antigravity.runtime.acp_session", fromlist=["AcpSession"]).AcpSession)
            await session.close()
        self.assertIn("--input-format", fake.invocations()[-1]["argv"])


class SubagentTests(AcpCase):
    async def test_a_sub_agent_is_a_run_its_steps_from_its_transcript(self) -> None:
        logs = self.brain / CHILD / ".system_generated" / "logs"
        child = [
            {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "content": "go"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "run_command", "args": {"CommandLine": "make"}}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "built"}]
        written = {"write": {"path": str(logs / "transcript_full.jsonl"),
                             "text": "".join(json.dumps(s) + "\n" for s in child)}}
        spawn = {"Role": "Builder", "TypeName": "self", "Prompt": "make"}

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="go"))
            await wait_for(events, c.RunFinished)
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *acp.started(), acp.prompt(),
            acp.tool_call("call-1", "invoke_subagent", spawn),
            acp.pre_tool("call-1", "invoke_subagent", spawn),
            acp.tool_done("call-1", "invoke_subagent", spawn, output="started"),
            agy.hook("SessionStart", {"conversationId": CHILD}),
            written, {"sleep": 1.5}, acp.stop(conv=CHILD),
            *acp.say("it built"), acp.stop(), acp.done()], drive=drive)
        started = of(events, c.RunStarted)
        self.assertEqual([(e.run, e.info.title) for e in started][:1][0][0], CHILD)
        runs = [e for e in events if e.run == CHILD]
        self.assertEqual([type(e).__name__ for e in runs if not isinstance(e, c.RunProgress)],
                         ["RunStarted", "ToolStarted", "ToolFinished", "RunFinished"])
        self.assertEqual(of(events, c.RunFinished)[0].status, "completed")


class PlanTests(AcpCase):
    async def test_an_approved_plan_leaves_plan_mode_and_is_carried_out_in_the_same_process(self) -> None:
        plan = "1. write hello.txt\n2. test it"

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="plan it"))
            await wait_for(events, c.SessionIdle)
        events, host, fake = await self.run_session([
            *acp.started(), *acp.call("session/set_mode", None, modeId="plan"), acp.prompt("plan it"),
            acp.pre_tool("call-1", "write_file", {"Path": f"/nowhere/brain/{SID}/plan.md",
                                                     "Content": plan,
                                                     "ArtifactMetadata": {"RequestFeedback": True}}),
            *acp.tool("call-2", "write_file", {"Path": str(self.work / "hello.txt"), "Content": "hi"},
                      decision="deny", step=3),
            *acp.say("Here is the plan."), acp.stop(), acp.done(),
            *acp.call("session/set_mode", None, modeId="default"),
            acp.prompt("The person approved your plan. Carry it out now."), *acp.say("done"), acp.stop(),
            acp.done()],
            spec={"permissions": c.PermissionPolicy(mode="plan")}, drive=drive)
        self.assertEqual([r.plan for r in host.permission_requests if r.kind == "plan"], [plan])
        self.assertEqual([e.origin for e in of(events, c.TurnStarted)], ["person", "system"])
        self.assertEqual(len([r for r in fake.invocations() if r["argv"] == ["acp"]]), 1)


if __name__ == "__main__":
    unittest.main()
