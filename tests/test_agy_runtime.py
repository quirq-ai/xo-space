"""The agy runtime (storm ch 04) against its fake CLI: XO's hook answers per
mode, the role prompt and hidden context, idle, interrupts, updates, plans,
sub-agents, the comms relay; and what it supports.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.antigravity.runtime import support
from services.cowork_agent.adapters.antigravity.runtime.core import AgyRuntime, models_from
from tests.fakes import agy, install as install_fake, scenario
from tests.fakes.host import ScriptedHost
from tests.test_agy_wire import CHILD

REPO = Path(__file__).resolve().parents[1]


class SupportTests(unittest.TestCase):
    def test_auto_is_emulated_and_threads_pick_it(self) -> None:
        from services.cowork_agent.storm.presets.common import thread_mode
        features = support.features("1.4.10", auth_env=())
        self.assertEqual(features.features["permissions.auto"].support, "emulated")
        self.assertEqual(thread_mode(features), "auto")
        self.assertEqual(features.features["send_while_running"].behaviour, "queued")
        self.assertEqual(features.features["project_config_paths"].values, (".agents/**",))

    def test_what_agy_cant_do_is_refused(self) -> None:
        for spec in (c.SessionSpec(cwd="/", system_prompt=c.SystemPrompt(text="x", mode="replace")),
                     c.SessionSpec(cwd="/", isolation="isolated"), c.SessionSpec(cwd="/", fork=True, resume="x"),
                     c.SessionSpec(cwd="/", mcp_servers=(c.McpServer(name="s", transport="stdio", command=("x",)),)),
                     c.SessionSpec(cwd="/", orchestration="coordinator", permissions=c.PermissionPolicy(mode="auto"))):
            with self.assertRaises(c.SpecRefused):
                support.check(spec)

    def test_a_coordinator_is_read_only_by_the_host(self) -> None:
        report = support.check(c.SessionSpec(cwd="/", orchestration="coordinator", tools=c.ToolPolicy(
            preset="read_only", deny=("edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule")),
            permissions=c.PermissionPolicy(mode="deny_unasked")))
        for feature in ("orchestration", "permissions.mode", "tools.deny"):
            self.assertIn(report.weakest(feature).enforced_by, ("cli", "hook", "host"), feature)


class ModelsTests(unittest.TestCase):
    def test_agys_text_list(self) -> None:
        found = models_from("Fetching available models...\n" + "\n".join(f"{i}\t{n}" for i, n in agy.MODELS))
        self.assertEqual([m.id for m in found][:3], ["gemini-3.8-flash-high", "gemini-3.8-flash-medium",
                                                     "gemini-3.8-flash-low"])
        self.assertTrue(found[0].is_default and not found[1].is_default)
        self.assertEqual(found[3].name, "Claude Sonnet 4.6 (Thinking)")


async def wait_for(events: list, kind: type, *, count: int = 1, timeout: float = 20) -> None:
    async def seen() -> None:
        while sum(isinstance(e, kind) for e in events) < count:
            await asyncio.sleep(0.02)
    await asyncio.wait_for(seen(), timeout)


class SessionCase(unittest.IsolatedAsyncioTestCase):
    """One agy session against the fake, speaking the contract."""

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
                          host: ScriptedHost | None = None, drive=None):
        body = {"runs": runs, "argv": agy.probes()} if runs is not None else scenario(steps or [], argv=agy.probes())
        fake = install_fake("agy", self.root / "bin", body)
        host = host or ScriptedHost(decide=lambda req: c.PermissionDecision(allow=True))
        env = {"AGY_CLI_PATH": str(fake.path), "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.root / "state")}
        events: list[c.Event] = []
        with mock.patch.dict(os.environ, env):
            session = await AgyRuntime().open(c.SessionSpec(cwd=str(self.work), **(spec or {})), host,
                                              session_id="cse_agy_test")

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


START = [agy.init(), agy.user_message(), agy.step(0, "user_input")]


class ModeTests(SessionCase):
    async def test_trusted_auto_asks_nobody_but_guards_protected_paths_and_comms(self) -> None:
        comms = c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1:9/mcp", auto_approve=True,
                            terminal_tools=("reply",))
        events, host, _ = await self.run_session([
            *START,
            *agy.tool(2, "run_command", {"CommandLine": "curl -sI https://example.com"}, output="200"),
            *agy.tool(3, "write_file", {"Path": str(self.work / "a.txt")}, args={"Path": str(self.work / "a.txt"),
                                                                                 "Content": "hi"}),
            *agy.tool(4, "write_file", {"Path": str(self.work / ".agents/hooks.json")}, decision="deny",
                      args={"Path": str(self.work / ".agents/hooks.json"), "Content": "{}"}),
            agy.pre_tool(5, "write_file", {"Path": f"/nowhere/brain/{agy.CONV}/plan.md", "Content": "p"}),
            agy.pre_tool(6, "call_mcp_tool", {"ServerName": "xo_xo_comms", "ToolName": "reply", "Arguments": {}}),
            agy.pre_tool(2, "call_mcp_tool", {"ServerName": "xo_xo_comms", "ToolName": "reply", "Arguments": {}},
                         conv=CHILD, decision="deny"),
            agy.pre_tool(7, "define_subagent", {"definition": "{}"}, decision="deny"),
            agy.pre_tool(8, "send_message", {"Message": "x"}),
            agy.pre_tool(9, "ask_permission", {"Reason": "x"}, decision="deny"),
            *agy.text(10, "done"), *agy.end("done")],
            spec={"permissions": c.PermissionPolicy(mode="auto"), "mcp_servers": (comms,)})
        self.assertEqual(host.permission_requests, [])            # nobody asked
        refused = [e for e in events if isinstance(e, c.PermissionResolved)]
        self.assertEqual([(e.request_id, e.decision.allow) for e in refused], [(f"agy-{agy.CONV}:4", False)])
        self.assertIn(".agents/**", refused[0].decision.message)
        finished = {e.call_id: e.status for e in events if isinstance(e, c.ToolFinished)}
        self.assertEqual(finished, {f"{agy.CONV}:2": "ok", f"{agy.CONV}:3": "ok", f"{agy.CONV}:4": "denied"})

    async def test_accept_edits_takes_own_edits_and_asks_about_commands(self) -> None:
        events, host, _ = await self.run_session([
            *START,
            *agy.tool(2, "edit_file", {"Path": str(self.work / "a.py")}, args={"Path": str(self.work / "a.py"),
                                                                               "Target": "x", "Replacement": "y"}),
            *agy.tool(3, "run_command", {"CommandLine": "rm -rf build"}, decision="deny"),
            *agy.text(4, "ok"), *agy.end("ok")],
            spec={"permissions": c.PermissionPolicy(mode="accept_edits")},
            host=ScriptedHost(decide=lambda req: c.PermissionDecision(allow=False, message="not that")))
        self.assertEqual([(r.kind, r.command) for r in host.permission_requests], [("command", "rm -rf build")])
        self.assertEqual([e.status for e in events if isinstance(e, c.ToolFinished)], ["ok", "denied"])

    async def test_a_question_reaches_the_person_and_their_answer_the_model(self) -> None:
        events, host, _ = await self.run_session([
            *START,
            {**agy.pre_tool(2, "ask_question", {"questions": [{"question": "Which color?", "options": ["Blue", "Green"],
                                                              "is_multi_select": False}]}, decision="deny")},
            agy.result("$reason")],
            host=ScriptedHost(answers={"0": "Green"}), drive=self.until_result)
        self.assertEqual(host.questions[0].questions[0]["question"], "Which color?")
        said = next(e for e in events if isinstance(e, c.TurnFinished)).text
        self.assertEqual(said, "XO asked the person. The person answered: Which color?: Green")

    async def until_result(self, session, events) -> None:
        await session.send(c.UserInput(text="hello"))
        await wait_for(events, c.TurnFinished)

    async def test_the_role_prompt_starts_the_conversation_and_context_rides_each_turn(self) -> None:
        events, _, _ = await self.run_session([
            agy.init(), agy.user_message(), agy.step(0, "user_input"), agy.session_start(),
            agy.pre_invocation("hello"), *agy.text(1, "$prompt"), agy.result("$context")],
            spec={"system_prompt": c.SystemPrompt(text="You are XO's thread."),
                  "orchestration": "coordinator", "permissions": c.PermissionPolicy(mode="deny_unasked"),
                  "tools": c.ToolPolicy(preset="read_only")},
            drive=lambda s, ev: self.send_with_context(s, ev))
        prompt = next(e for e in events if isinstance(e, c.TextDone)).text
        context = next(e for e in events if isinstance(e, c.TurnFinished)).text
        self.assertTrue(prompt.startswith("You are XO's thread."))
        self.assertIn("XO refuses edits", prompt)              # the coordinator addendum
        self.assertEqual(context, "XO: the wake reason")

    async def send_with_context(self, session, events) -> None:
        await session.send(c.UserInput(text="hello", context="XO: the wake reason"))
        await wait_for(events, c.TurnFinished)

    async def test_the_session_is_idle_only_once_agys_loop_has_nothing_running(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="hello"))
            await wait_for(events, c.TurnFinished)
            await asyncio.sleep(0.5)
            self.assertFalse(any(isinstance(e, c.SessionIdle) for e in events))   # a sub-agent still works
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *START, *agy.text(1, "started it"), agy.stop(fully_idle=False), agy.result("started it"),
            {"sleep": 1}, agy.step(5, "system_message"), *agy.text(6, "it reported"), *agy.end("it reported")],
            drive=drive)
        self.assertEqual([e.origin for e in events if isinstance(e, c.TurnStarted)], ["person", "injected"])
        self.assertEqual(sum(isinstance(e, c.SessionIdle) for e in events), 1)


class LifecycleTests(SessionCase):
    async def test_a_conversation_agy_doesnt_have_is_not_replaced_silently(self) -> None:
        fake = install_fake("agy", self.root / "bin", scenario([agy.init("99999999-9999-4999-8999-999999999999")],
                                                               argv=agy.probes()))
        env = {"AGY_CLI_PATH": str(fake.path), "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.root / "state")}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(c.RuntimeFailure) as caught:
                await AgyRuntime().open(c.SessionSpec(cwd=str(self.work), resume=agy.CONV), ScriptedHost(),
                                        session_id="cse_agy_missing")
        self.assertEqual(caught.exception.code, "session_not_found")
        self.assertEqual(fake.invocations()[-1]["argv"][-3:], ["--conversation", agy.CONV, "--print="])

    async def test_an_update_starts_agy_again_on_the_conversation(self) -> None:
        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="one"))
            await wait_for(events, c.SessionIdle)
            report = await session.update(model="gemini-3.8-flash-high", effort="high")
            self.assertEqual({i.support for i in report.items}, {"emulated"})
            await session.send(c.UserInput(text="two"))
            await wait_for(events, c.SessionIdle, count=2)
        _, _, fake = await self.run_session(runs=[
            [*START, *agy.text(1, "one"), *agy.end("one")],
            [agy.init(), agy.user_message(), *agy.text_turn("two", first=3)]], drive=drive)
        argvs = [run["argv"] for run in fake.invocations() if "--input-format" in run["argv"]]
        self.assertEqual(len(argvs), 2)
        self.assertIn("gemini-3.8-flash-high", argvs[1])
        self.assertEqual(argvs[1][argvs[1].index("--conversation") + 1], agy.CONV)

    async def test_an_approved_plan_leaves_plan_mode_and_is_carried_out(self) -> None:
        plan = "1. write hello.txt\n2. test it"

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="plan it"))
            await wait_for(events, c.SessionIdle)
        events, host, fake = await self.run_session(runs=[
            [*START, agy.pre_tool(2, "write_file", {"Path": f"/nowhere/brain/{agy.CONV}/plan.md", "Content": plan,
                                                    "ArtifactMetadata": {"RequestFeedback": True}}),
             *agy.tool(3, "write_file", {"Path": str(self.work / "hello.txt")}, decision="deny",
                       args={"Path": str(self.work / "hello.txt"), "Content": "hi"}),
             *agy.text(4, "Here is the plan."), *agy.end("Here is the plan.")],
            [agy.init(), expect_text("The person approved your plan. Carry it out now."),
             *agy.text_turn("done", first=6)]],
            spec={"permissions": c.PermissionPolicy(mode="plan")}, drive=drive)
        asked = [r for r in host.permission_requests if r.kind == "plan"]
        self.assertEqual([r.plan for r in asked], [plan])
        argvs = [run["argv"] for run in fake.invocations() if "--input-format" in run["argv"]]
        self.assertIn("--mode", argvs[0])
        self.assertNotIn("--mode", argvs[1])
        self.assertEqual([e.origin for e in events if isinstance(e, c.TurnStarted)], ["person", "system"])

    async def test_a_sub_agents_steps_come_from_its_transcript(self) -> None:
        logs = self.brain / CHILD / ".system_generated" / "logs"
        child = [
            {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "content": "go"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "run_command", "args": {"CommandLine": "make"}}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "built"}]
        spawn = agy.step(2, "subagent", tool_name="invoke_subagent", subagent_info={"subagents": [{
            "type_name": "self", "role": "Builder", "conversation_id": CHILD, "initial_prompt": "make"}]})
        written = {"write": {"path": str(logs / "transcript_full.jsonl"),
                             "text": "".join(json.dumps(s) + "\n" for s in child)}}

        async def drive(session, events) -> None:
            await session.send(c.UserInput(text="go"))
            await wait_for(events, c.RunFinished)
            await wait_for(events, c.SessionIdle)
        events, _, _ = await self.run_session([
            *START, agy.step(2, "tool", state="ACTIVE", tool_name="invoke_subagent",
                             tool_info={"name": "invoke_subagent", "parameters": {"Role": "Builder"}}),
            agy.pre_tool(2, "invoke_subagent", {"Role": "Builder", "TypeName": "self", "Prompt": "make"}),
            spawn, written, {"sleep": 1.5}, *agy.text(4, "it built"), *agy.end("it built")], drive=drive)
        runs = [e for e in events if e.run == CHILD]
        self.assertEqual([type(e).__name__ for e in runs if not isinstance(e, c.RunProgress)],
                         ["RunStarted", "ToolStarted", "ToolFinished", "RunFinished"])
        self.assertEqual(next(e for e in runs if isinstance(e, c.RunFinished)).status, "completed")


def expect_text(words: str) -> dict:
    from tests.fakes import expect
    return expect({"event": "user", "message": {"content": words}})


class _McpServer:
    """A Streamable-HTTP MCP server with one tool, in a thread; it records
    each request's bearer token."""

    def __init__(self) -> None:
        import http.server
        import threading
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.headers.get("Authorization"), body.get("method")))
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                         "serverInfo": {"name": "t", "version": "1"}},
                          "tools/list": {"tools": [{"name": "reply", "inputSchema": {"type": "object"}}]},
                          "tools/call": {"content": [{"type": "text", "text": json.dumps({"ok": True})}],
                                         "isError": False}}.get(body["method"], {})
                data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RelayTests(SessionCase):
    async def test_comms_go_through_xos_relay_with_the_token_kept_in_xo_and_a_terminal_tool_ends_the_turn(self):
        server = _McpServer()
        self.addCleanup(server.close)
        comms = c.McpServer(name="xo_comms", transport="http", url=server.url, bearer_env="XO_COMMS_TOKEN",
                            env={"XO_COMMS_TOKEN": "tok-secret-123"}, auto_approve=True, terminal_tools=("reply",))
        events, host, fake = await self.run_session([
            *START,
            agy.pre_tool(2, "call_mcp_tool", {"ServerName": "xo_xo_comms", "ToolName": "reply",
                                              "Arguments": {"text": "hi"}}),
            {"mcp": {"server": "xo_comms", "tool": "reply", "arguments": {"text": "hi"}, "conversation": agy.CONV},
             "bind": {"ok": "ok"}},
            agy.hook("PostInvocation", {"conversationId": agy.CONV, "invocationNum": 1},
                     bind={"term": "terminationBehavior"}),
            *agy.text(3, "$term"), *agy.end("replied")],
            spec={"permissions": c.PermissionPolicy(mode="auto"), "mcp_servers": (comms,)})
        self.assertEqual(host.permission_requests, [])
        self.assertIn(("Bearer tok-secret-123", "tools/call"), server.seen)
        self.assertEqual(next(e for e in events if isinstance(e, c.TextDone)).text, "terminate")
        run = next(r for r in fake.invocations() if "--input-format" in r["argv"])
        self.assertNotIn("tok-secret-123", json.dumps(run["env"]))        # never in agy's env
        self.assertEqual(run["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertTrue(run["env"]["XO_AGY_SOCKET"].endswith("/agy.sock"))
        installed = json.loads((self.home / ".gemini/config/plugins/xo/mcp_config.json").read_text())
        self.assertEqual(list(installed["mcpServers"]), ["xo_comms"])


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_imports_no_fastapi_no_storm_no_other_agent(self) -> None:
        code = (
            "import sys\n"
            "import services.cowork_agent.adapters.antigravity.runtime\n"
            "import services.cowork_agent.adapters.antigravity.oneshot_prompt\n"
            "ours = ('antigravity', 'contract', 'loader', 'env_whitelist', 'cli_status', 'base')\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('fastapi', 'starlette')\n"
            "       or m.startswith('services.cowork_agent.storm')\n"
            "       or (m.startswith('services.cowork_agent.adapters.') and m.split('.')[3] not in ours)]\n"
            "print(bad)\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
