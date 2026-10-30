"""Regression cases for startup, readiness, and session-lifetime buffering."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.codex.runtime import params as p
from services.cowork_agent.adapters.codex.runtime import session as session_module
from services.cowork_agent.adapters.codex.runtime.wire import Wire
from tests.fakes import codex as cx
from tests.fakes.host import ScriptedHost
from tests.test_codex_runtime import FakeCodex, T, CHILD
from tests.test_runtime_conformance import wait_for_event
from utils.commands import live_processes


COMMS = c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1:9/mcp",
                    auto_approve=True, terminal_tools=("reply",))


def feed(wire: Wire, *steps: dict) -> list:
    out = []
    for step in steps:
        msg = step["emit"]
        out.extend(wire.notification(msg["method"], msg["params"]))
    return out


class RetentionTests(unittest.TestCase):
    def wire(self) -> Wire:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.thread_id = T
        return wire

    def test_finished_output_does_not_accumulate_across_turns(self) -> None:
        wire = self.wire()
        for turn in range(20):
            tid = f"turn-{turn}"
            wire.start_turn(tid)
            for tool in range(5):
                cid = f"tool-{turn}-{tool}"
                outs = feed(wire, cx.command_started(T, tid, cid, "echo hello"),
                            cx.command_completed(T, tid, cid, "echo hello", "x" * 16384 + "\nlast line"))
                self.assertEqual(outs[-1].fields["summary"], "last line")
            feed(wire, *cx.turn_end(T, tid))
            self.assertEqual((len(wire._items), len(wire._item_runs), len(wire._completed_items)), (0, 0, 0))

    def test_completed_patch_is_kept_until_all_approval_handlers_settle(self) -> None:
        wire = self.wire()
        wire.start_turn("tu")
        changes = [cx.add("/w/a", "hello"), cx.add("/w/b", "world")]
        feed(wire, cx.change_started(T, "tu", "patch", changes))
        wire.hold_item("patch")
        wire.hold_item("patch")
        feed(wire, cx.change_completed(T, "tu", "patch", changes))
        self.assertEqual(wire.item("patch")["changes"], changes)
        wire.release_item("patch")
        self.assertIsNotNone(wire.item("patch"))
        wire.release_item("patch")
        self.assertIsNone(wire.item("patch"))
        self.assertFalse(wire._held_items)
        self.assertEqual(feed(wire, cx.notify("item/fileChange/patchUpdated", {
            "threadId": T, "itemId": "patch", "changes": changes})), [])

    def test_active_child_survives_root_completion_and_is_pruned_when_it_ends(self) -> None:
        wire = self.wire()
        wire.start_turn("tu")
        feed(wire, *cx.spawned(T, "tu", CHILD, "/root/worker"), *cx.child_turn(CHILD, "ct", "tu"),
             cx.command_started(CHILD, "ct", "child-tool", "sleep 60"),
             cx.command_started(T, "tu", "root-tool", "sleep 1"), *cx.turn_end(T, "tu"))
        self.assertIsNotNone(wire.item("child-tool"))
        self.assertIsNone(wire.item("root-tool"))
        outs = feed(wire, cx.output_delta(CHILD, "ct", "child-tool", "still running"))
        self.assertEqual(outs[0].fields["delta"], "still running")
        feed(wire, cx.turn_completed(CHILD, "ct", status="interrupted"))
        self.assertEqual(wire._items, {})

    def test_empty_patch_stays_available_before_completion(self) -> None:
        wire = self.wire()
        wire.start_turn("tu")
        feed(wire, cx.change_started(T, "tu", "patch", []), cx.command_completed(T, "tu", "other", "ls"))
        self.assertIsNotNone(wire.item("patch"))
        feed(wire, *cx.turn_end(T, "tu"))
        self.assertEqual(wire._items, {})


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def cli(self, steps: list[dict]) -> FakeCodex:
        cli = FakeCodex(steps)

        async def cleanup() -> None:
            try:
                if hasattr(cli, "session"):
                    await cli.finish()
            finally:
                cli.env.stop()
                cli.tmp.cleanup()
        self.addAsyncCleanup(cleanup)
        return cli

    async def test_startup_burst_returns_before_consumer_and_preserves_order(self) -> None:
        burst = [cx.notify("warning", {"message": f"startup-{n}"}) for n in range(1050)]
        cli = await self.cli([*cx.handshake(), *burst, *cx.thread_start(T), *cx.text_turn(T, "tu", "ok")])
        await asyncio.wait_for(cli.open(), 10)
        await cli.session.send(c.UserInput(text="hello"))
        await wait_for_event(cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertIsInstance(cli.events[0], c.SessionStarted)
        notices = cli.of(c.Notice)
        self.assertEqual([e.message for e in notices], [f"startup-{n}" for n in range(1050)])
        self.assertEqual([e.seq for e in cli.events], list(range(1, len(cli.events) + 1)))
        self.assertLessEqual(cli.session._queue.qsize(), session_module.EVENT_QUEUE_SIZE)

    async def test_cancelling_startup_reaps_process_and_host_handlers(self) -> None:
        cli = await self.cli([*cx.handshake(), {"sleep": 30}])
        host = ScriptedHost()
        opening = asyncio.create_task(cli.open(host))
        while not host.launcher.spawned:
            await asyncio.sleep(0.01)
        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await opening
        live = {proc.pid for proc in live_processes()}
        self.assertFalse(live.intersection(h.pid for h in host.launcher.spawned))

    async def test_resumed_live_turn_does_not_look_idle_during_bootstrap(self) -> None:
        cli = await self.cli([*cx.handshake(), cx.notify("turn/started", {
            "threadId": T, "turn": cx.turn_obj("tu")}),
            *cx.call("thread/resume", {"thread": cx.thread(T, status="active"), "model": cx.MODEL})])
        await cli.open(resume=T)
        self.assertFalse(cli.session._turn_idle.is_set())

    async def test_process_loss_at_startup_does_not_return_a_dead_session(self) -> None:
        cli = await self.cli([*cx.started(T), {"exit": 0}])
        original = session_module.CodexSession._attach

        async def attach(session):
            notices = await original(session)
            await asyncio.wait_for(session._finished.wait(), 5)
            return notices
        with mock.patch.object(session_module.CodexSession, "_attach", attach):
            with self.assertRaises(c.RuntimeFailure) as caught:
                await cli.open()
        self.assertEqual(caught.exception.code, "process_died")
        self.assertTrue(caught.exception.retryable)

    async def test_resume_excludes_returned_history_and_keeps_model_and_effort(self) -> None:
        cli = await self.cli([*cx.handshake(), *cx.thread_start(T, method="thread/resume"),
                              *cx.text_turn(T, "tu", "continued", model="chosen-model", effort="high")])
        await cli.open(resume=T, model="chosen-model", effort="high")
        params = next(line["params"] for line in cli.stdin() if line.get("method") == "thread/resume")
        self.assertTrue(params["excludeTurns"])
        self.assertNotIn("ephemeral", params)
        await cli.session.send(c.UserInput(text="continue"))
        await wait_for_event(cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertEqual(cli.mismatches(), [])

    async def test_only_exact_missing_resume_error_is_classified_as_not_found(self) -> None:
        for method, message, code in (
                ("thread/resume", f"no rollout found for thread id {T}", "session_not_found"),
                ("thread/resume", "permission denied reading rollout", "protocol"),
                ("thread/fork", f"no rollout found for thread id {T}", "protocol")):
            with self.subTest(method=method, message=message):
                cli = await self.cli([*cx.handshake(), *cx.refuse(method, message)])
                with self.assertRaises(c.RuntimeFailure) as caught:
                    await cli.open(resume=T, fork=method == "thread/fork")
                self.assertEqual(caught.exception.code, code)

    async def test_required_comms_initialization_failure_is_retryable(self) -> None:
        cli = await self.cli([*cx.handshake(), *cx.refuse("thread/start",
                              "Fatal error: required MCP servers failed to initialize: xo_comms", code=-32603)])
        with self.assertRaises(c.RuntimeFailure) as caught:
            await cli.open(mcp_servers=(COMMS,))
        self.assertTrue(caught.exception.retryable)
        self.assertIn("conversation tools", caught.exception.message)

    async def test_required_tools_are_verified_on_the_threads_own_connection(self) -> None:
        for tools, success in (({"mcp__xo_comms__reply": {"name": "reply"}}, True), ({}, False)):
            with self.subTest(tools=tools):
                cli = await self.cli([*cx.started(T), *cx.call("mcpServerStatus/list", {
                    "data": [{"name": "xo_comms", "tools": tools, "runtimeStatus": "connected"}]},
                    threadId=T, serverName="xo_comms", detail="toolsAndAuthOnly")])
                if success:
                    await cli.open(mcp_servers=(COMMS,))
                else:
                    with self.assertRaises(c.RuntimeFailure) as caught:
                        await cli.open(mcp_servers=(COMMS,))
                    self.assertTrue(caught.exception.retryable)
                self.assertEqual(cli.mismatches(), [])

    async def test_optional_auto_approved_integration_does_not_gate_startup(self) -> None:
        optional = c.McpServer(name="optional", transport="http", url="http://127.0.0.1:9", auto_approve=True)
        cli = await self.cli([*cx.started(T), cx.notify("mcpServer/startupStatus/updated", {
            "name": "optional", "status": "failed", "error": "unavailable"})])
        await cli.open(mcp_servers=(optional,))
        self.assertNotIn("required", p.mcp_servers(c.SessionSpec(cwd="/w", mcp_servers=(optional,)))["optional"])
        self.assertEqual(cli.mismatches(), [])

    async def test_completion_before_approval_answer_keeps_item_until_handler_finishes(self) -> None:
        release = asyncio.Event()

        async def decide(request):
            await release.wait()
            return c.PermissionDecision(allow=True)
        changes = [cx.add("/w/a", "pending patch")]
        cli = await self.cli([*cx.started(T), *cx.turn_start(T, "tu"),
                              cx.change_started(T, "tu", "patch", changes),
                              cx.change_approval(0, T, "tu", "patch"),
                              cx.change_completed(T, "tu", "patch", changes),
                              cx.approval_answer(0, "accept"), *cx.turn_end(T, "tu")])
        await cli.open(ScriptedHost(decide=decide))
        await cli.session.send(c.UserInput(text="edit"))
        await wait_for_event(cli.events, c.ToolFinished)
        self.assertEqual(cli.session._wire.item("patch")["changes"], changes)
        release.set()
        await wait_for_event(cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertEqual(cli.session._wire._items, {})
        self.assertEqual(cli.session._wire._held_items, {})
        self.assertEqual(cli.mismatches(), [])
