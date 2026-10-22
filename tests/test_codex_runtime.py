"""The codex runtime against its fake app-server (storm ch 03; C1).

Proves what the conformance suite can't say in neutral terms: the
app-server's parameters per mode and role (no sandbox, ``untrusted``, never
a skip flag), the env whitelist (codex's own variables, the session's MCP
token by name only), the answers to codex's approval requests per role (a
thread's own edits accepted here, everything else to the host, a deny's
reason steered into the turn, never an exec-policy amendment), questions,
steer and interrupt, the idle that waits for the turn's end, usage as the
turn's own tokens, the rate-limit guard, reconnect detection, sub-agent runs
and stopping one, the failure paths, and codex's own transcript files. No
real CLI runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.codex.runtime import params as p
from services.cowork_agent.adapters.codex.runtime import support
from services.cowork_agent.adapters.codex.runtime.account import UsageGuard, account_fingerprint, login_fingerprint
from services.cowork_agent.adapters.codex.runtime.env import credential_vars, session_env
from services.cowork_agent.adapters.codex.runtime.rpc import classify, version_of
from services.cowork_agent.adapters.codex.runtime.core import CodexRuntime, models_from
from services.cowork_agent.adapters.codex.runtime.tools import describe_change, own_edit, protected, unwrap
from services.cowork_agent.adapters.codex.runtime.wire import Wire, thinking_steps
from tests.fakes import codex as cx
from tests.fakes import expect, install, scenario
from tests.fakes.host import ScriptedHost
from tests.test_runtime_conformance import wait_for_event
from utils.commands import live_processes

T = "01a165ad-83f1-72e2-a03d-5bfa9b662c93"
CHILD = "01a165ae-2158-7443-8244-800f405be19e"
ALLOW = c.PermissionDecision(allow=True)
SKIP_FLAGS = ("--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--yolo", "bypass_hook_trust")


def app_server_for(spec: c.SessionSpec) -> list[str]:
    """What one session's codex runs with: its app-server's argv, then its
    thread's parameters (every session setting is there, none in argv)."""
    return ["codex", "app-server", "--listen", "stdio://",
            json.dumps(p.thread_params(spec, mode=spec.permissions.mode), sort_keys=True)]


class ParamsTests(unittest.TestCase):
    def test_every_session_runs_without_a_sandbox_and_asks_about_everything(self) -> None:
        for mode in ("ask", "accept_edits", "plan", "deny_unasked", "auto"):
            with self.subTest(mode):
                spec = c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode=mode))
                params = p.thread_params(spec, mode=mode)
                self.assertEqual((params["sandbox"], params["approvalPolicy"], params["approvalsReviewer"]),
                                 ("danger-full-access", "untrusted", "user"))
                self.assertFalse(any(flag in json.dumps(params) for flag in SKIP_FLAGS))
        self.assertEqual(p.thread_params(c.SessionSpec(cwd="/w"), mode="bypass")["approvalPolicy"], "never")

    def test_a_threads_parameters_name_its_comms_token_never_hold_it(self) -> None:
        comms = c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1:5002/mcp/space-comms",
                            bearer_env="XO_COMMS_TOKEN", env={"XO_COMMS_TOKEN": "secret-token"}, auto_approve=True,
                            terminal_tools=("reply", "no_reply_needed"))
        composio = c.McpServer(name="composio", transport="http", url="http://127.0.0.1:9/mcp/composio-proxy/u/t")
        spec = c.SessionSpec(cwd="/w", model="gpt-6-luna", system_prompt=c.SystemPrompt(text="You are a thread."),
                             mcp_servers=(comms, composio), persist=True)
        params = p.thread_params(spec, mode="accept_edits")
        self.assertNotIn("secret-token", json.dumps(params))
        self.assertEqual(params["config"]["mcp_servers"], {
            "xo_comms": {"url": "http://127.0.0.1:5002/mcp/space-comms", "bearer_token_env_var": "XO_COMMS_TOKEN",
                         "default_tools_approval_mode": "approve"},
            "composio": {"url": "http://127.0.0.1:9/mcp/composio-proxy/u/t"}})
        self.assertEqual(params["model"], "gpt-6-luna")
        self.assertFalse(params["ephemeral"])
        self.assertTrue(params["developerInstructions"].startswith("You are a thread."))
        self.assertIn(p.EDIT_NOTE, params["developerInstructions"])
        self.assertIn("`reply` on `xo_comms`", params["developerInstructions"])
        self.assertEqual(params["config"]["model_reasoning_summary"], "detailed")
        self.assertNotIn("web_search", params["config"])

    def test_a_coordinator_gets_its_addendum_and_no_web_search(self) -> None:
        spec = c.SessionSpec(cwd="/w", orchestration="coordinator", permissions=c.PermissionPolicy(mode="deny_unasked"),
                             system_prompt=c.SystemPrompt(text="role"))
        params = p.thread_params(spec, mode="deny_unasked")
        self.assertIn(p.COORDINATOR_ADDENDUM, params["developerInstructions"])
        self.assertEqual(params["config"]["web_search"], "disabled")

    def test_a_replaced_prompt_and_thinking_off(self) -> None:
        spec = c.SessionSpec(cwd="/w", system_prompt=c.SystemPrompt(text="only this", mode="replace"), thinking="off")
        params = p.thread_params(spec, mode="ask")
        self.assertEqual(params["baseInstructions"], "only this")
        self.assertNotIn("developerInstructions", params)
        self.assertEqual(params["config"]["model_reasoning_summary"], "none")

    def test_a_turn_carries_effort_context_and_images_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "shot.png"
            image.write_bytes(b"\x89PNG")
            spec = c.SessionSpec(cwd="/w", effort="high", output_schema={"type": "object"})
            body = p.turn_params(T, c.UserInput(text="look", images=(str(image),), context="[envelope]"), spec=spec,
                                 mode="ask", model="gpt-6-luna")
        self.assertEqual(body["input"], [{"type": "text", "text": "look"}, {"type": "localImage", "path": str(image)}])
        self.assertEqual(body["additionalContext"], {"xo": {"kind": "application", "value": "[envelope]"}})
        self.assertEqual((body["effort"], body["outputSchema"], body["approvalPolicy"]),
                         ("high", {"type": "object"}, "untrusted"))
        self.assertNotIn("collaborationMode", body)
        plan = p.turn_params(T, c.UserInput(text="plan it"), spec=spec, mode="plan", model="gpt-6-luna",
                             collaboration="plan")
        self.assertEqual(plan["collaborationMode"], {"mode": "plan", "settings": {"model": "gpt-6-luna",
                                                                                  "reasoning_effort": "high"}})

    def test_an_unknown_image_type_is_unsupported(self) -> None:
        with self.assertRaises(c.RuntimeFailure) as caught:
            p.user_input(c.UserInput(text="x", images=("/tmp/a.tiff",)))
        self.assertEqual(caught.exception.code, "unsupported")


class CheckAndFeaturesTests(unittest.TestCase):
    def test_the_presets_specs_pass_check(self) -> None:
        coordinator = c.SessionSpec(cwd="/w", orchestration="coordinator", tools=c.ToolPolicy(
            preset="read_only", allow=("read", "search", "list", "shell:git log", "mcp:xo_comms/*"),
            deny=("edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule")),
            permissions=c.PermissionPolicy(mode="deny_unasked"))
        report = support.check(coordinator)
        for feature in ("orchestration", "permissions.mode", "tools.deny"):
            self.assertIn(report.weakest(feature).enforced_by, ("cli", "host"), feature)
        support.check(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode="accept_edits")))
        plain = support.check(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode="auto")))
        self.assertEqual(plain.weakest("permissions.mode").support, "emulated")

    def test_check_refuses_what_codex_cant_run(self) -> None:
        refused = [
            c.SessionSpec(cwd="/w", isolation="isolated"),
            c.SessionSpec(cwd="/w", fork_at="turn-1", resume=T),
            c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(sandbox="read_only")),
            c.SessionSpec(cwd="/w", host_tools=(c.HostTool(name="x", description="x", input_schema={}),)),
            c.SessionSpec(cwd="/w", orchestration="coordinator", permissions=c.PermissionPolicy(mode="auto")),
            c.SessionSpec(cwd="/w", tools=c.ToolPolicy(deny=("subagents",))),
        ]
        for spec in refused:
            with self.subTest(spec):
                with self.assertRaises(c.SpecRefused):
                    support.check(spec)
        with self.assertRaises(c.SpecRefused):
            support.check(c.SessionSpec(cwd="/w", strict=True, permissions=c.PermissionPolicy(mode="auto")))

    def test_feature_list(self) -> None:
        listed = support.features("0.162.1", auth_env=("OPENAI_API_KEY",)).features
        self.assertEqual(listed["permissions.auto"].support, "emulated")
        self.assertIn("auto", listed["permissions.mode"].values)
        self.assertEqual(listed["send_while_running"].behaviour, "steered")
        self.assertIsNone(listed["interrupt"].behaviour)
        self.assertEqual(listed["project_config_paths"].values, (".codex/**",))
        self.assertEqual(listed["effort"].values, ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual(listed["runs.message"].support, "unsupported")
        self.assertEqual(listed["tool_output_delta"].support, "native")


class EnvTests(unittest.TestCase):
    def test_codex_variables_pass_server_secrets_dont_and_the_token_comes_from_the_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                '[model_providers.gateway]\nenv_key = "GATEWAY_KEY"\n'
                '[mcp_servers.mine]\nurl = "http://x"\nbearer_token_env_var = "MINE_TOKEN"\n', encoding="utf-8")
            environ = {"PATH": "/bin", "HOME": str(home), "OPENAI_API_KEY": "sk-x", "GATEWAY_KEY": "g",
                       "MINE_TOKEN": "m", "CODEX_SANDBOX_NETWORK_DISABLED": "1", "XO_COMMS_TOKEN": "server-copy",
                       "AWS_SECRET_ACCESS_KEY": "nope", "CODEX_HOME": str(home / ".codex")}
            env = session_env(spec_env={"EXTRA": "1"}, server_env=[{"XO_COMMS_TOKEN": "session-token"}],
                              label="test", environ=environ, store={})
            vars_ = credential_vars(environ)
        self.assertEqual((env["OPENAI_API_KEY"], env["GATEWAY_KEY"], env["MINE_TOKEN"]), ("sk-x", "g", "m"))
        self.assertEqual(env["CODEX_HOME"], str(home / ".codex"))
        self.assertEqual(env["XO_COMMS_TOKEN"], "session-token")
        self.assertEqual(env["EXTRA"], "1")
        for name in ("CODEX_SANDBOX_NETWORK_DISABLED", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(name, env)
        self.assertEqual(vars_, ("CODEX_API_KEY", "GATEWAY_KEY", "OPENAI_API_KEY"))


class ToolsTests(unittest.TestCase):
    def test_unwrap_the_login_shell(self) -> None:
        self.assertEqual(unwrap("/bin/bash -lc 'ls -a'"), "ls -a")
        self.assertEqual(unwrap(["/bin/bash", "-lc", "git status"]), "git status")
        self.assertEqual(unwrap("python3 x.py"), "python3 x.py")

    def test_a_patch_is_one_call_of_its_strongest_kind(self) -> None:
        call = describe_change({"changes": [cx.add("/w/a.txt", "a\n"), cx.update("/w/README.md", "+x\n")]})
        self.assertEqual((call.kind, call.title, call.paths),
                         ("edit_file", "Edit 2 files", ("/w/a.txt", "/w/README.md")))
        self.assertEqual(describe_change({"changes": [cx.add("/w/a.txt", "a")]}).kind, "create_file")
        self.assertEqual(describe_change({"changes": [{"path": "/w/a", "kind": {"type": "delete"}, "diff": ""}]}).kind,
                         "delete_file")
        self.assertIsNotNone(call.content_sha256)

    def test_own_edits_are_inside_the_folder_and_never_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            (work / "src").mkdir(parents=True)
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            (work / "link").symlink_to(outside)
            self.assertTrue(own_edit([str(work / "src" / "a.py"), str(work / "new.txt")], str(work)))
            for path in (outside / "x", work / "link" / "x", work / ".git" / "hooks" / "pre-commit", work / ".git",
                         work / ".xo" / "state.json", work / ".codex" / "config.toml", work / ".codex"):
                with self.subTest(str(path)):
                    self.assertFalse(own_edit([str(path)], str(work)))
            self.assertEqual(protected(str(work / ".codex" / "hooks.json"), str(work)), ".codex/**")
            # Every agent's project config, not only codex's: a codex thread must not arm claude or agy.
            for other in (work / ".claude" / "settings.json", work / ".mcp.json", work / ".agents" / "hooks.json",
                          work / "sub" / ".git" / "config.worktree"):
                with self.subTest(str(other)):
                    self.assertFalse(own_edit([str(other)], str(work)))
            self.assertFalse(own_edit([], str(work)))


class RpcTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify(b'{"id": 1, "result": {}}').kind, "response")
        self.assertEqual(classify(b'{"id": 0, "method": "item/fileChange/requestApproval", "params": {}}').kind,
                         "request")
        self.assertEqual(classify(b'{"method": "turn/started", "params": {}}').kind, "notification")
        self.assertEqual(classify(b"not json").kind, "unknown")
        self.assertEqual(version_of(cx.USER_AGENT), "0.162.1")


class WireTests(unittest.TestCase):
    def wire(self, **kw) -> Wire:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False, **kw)
        wire.thread_id = T
        return wire

    def feed(self, wire: Wire, *steps: dict) -> list:
        outs = []
        for step in steps:
            emit = step["emit"]
            outs += wire.notification(emit["method"], emit["params"])
        return outs

    def test_idle_waits_for_the_turns_end(self) -> None:
        wire = self.wire()
        outs = self.feed(wire, cx.status(T, "active"), cx.notify("turn/started", {"threadId": T,
                                                                                    "turn": cx.turn_obj("tu")}))
        self.assertEqual([o.cls.__name__ for o in outs], ["TurnStarted"])
        self.assertEqual(outs[0].fields["origin"], "injected")      # nobody here started it
        self.assertEqual(self.feed(wire, cx.status(T, "idle")), [])
        outs = self.feed(wire, cx.turn_completed(T, "tu"))
        self.assertEqual([o.cls.__name__ for o in outs], ["TurnFinished", "SessionIdle"])

    def test_usage_is_the_turns_own_tokens(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu-1")
        self.feed(wire, cx.token_usage(T, "tu-1", total={"inputTokens": 100, "cachedInputTokens": 60,
                                                          "outputTokens": 10}))
        self.feed(wire, cx.turn_completed(T, "tu-1"))
        wire.expect_turn("person")
        wire.start_turn("tu-2")
        outs = self.feed(wire, cx.token_usage(T, "tu-2", total={"inputTokens": 250, "cachedInputTokens": 160,
                                                                 "outputTokens": 25, "reasoningOutputTokens": 5},
                                              last={"inputTokens": 150, "cachedInputTokens": 100,
                                                    "outputTokens": 15}))
        self.assertEqual(outs, [])                                  # reported once, as the turn ends
        outs = self.feed(wire, cx.turn_completed(T, "tu-2"))
        usage = outs[0].fields["usage"]
        self.assertEqual(outs[0].cls.__name__, "Usage")
        self.assertEqual((usage.input, usage.cache_read, usage.output, usage.reasoning, usage.context_tokens),
                         (50, 100, 15, 5, 150))
        self.assertEqual(outs[1].fields["usage"], usage)

    def test_an_interrupt_cancels_open_calls_and_keeps_the_words_so_far(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        self.feed(wire, cx.command_started(T, "tu", "ex", "sleep 9"),
                  cx.item("item/started", T, "tu", {"type": "agentMessage", "id": "m", "text": "", "phase": "x"}),
                  cx.notify("item/agentMessage/delta", {"threadId": T, "turnId": "tu", "itemId": "m",
                                                        "delta": "Half a"}))
        outs = self.feed(wire, cx.turn_completed(T, "tu", status="interrupted"))
        names = [o.cls.__name__ for o in outs]
        self.assertEqual(names, ["ToolFinished", "TextDone", "TurnFinished"])
        self.assertEqual(outs[0].fields["status"], "cancelled")
        self.assertEqual(outs[1].fields["text"], "Half a")
        self.assertEqual(outs[2].fields["status"], "interrupted")

    def test_thinking_steps_take_codexs_bold_titles(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        outs = self.feed(wire, *cx.reasoning(T, "tu", ["**Reading the config** I look at it.",
                                                       "**Deciding** then I pick."]))
        deltas = [o for o in outs if o.cls.__name__ == "ThinkingDelta"]
        self.assertEqual([d.fields["step_title"] for d in deltas], ["Reading the config", "Deciding"])
        done = [o for o in outs if o.cls.__name__ == "ThinkingDone"][0]
        self.assertEqual(done.fields["steps"], ("Reading the config", "Deciding"))
        self.assertEqual(thinking_steps("Plain words. More."), ("Plain words.",))
        off = self.wire(thinking=False)
        off.expect_turn("person")
        off.start_turn("tu")
        self.assertEqual(self.feed(off, *cx.reasoning(T, "tu", ["**x** y"])), [])

    def test_a_failed_turn_on_the_apis_side_is_retryable(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        outs = self.feed(wire, cx.turn_completed(T, "tu", status="failed",
                                                 error={"message": "overloaded", "codexErrorInfo": "serverOverloaded"}))
        self.assertEqual([o.cls.__name__ for o in outs], ["Error", "TurnFinished"])
        self.assertTrue(outs[0].fields["retryable"])
        self.assertEqual(outs[1].fields["error"], "overloaded")
        other = self.wire()
        other.expect_turn("person")
        other.start_turn("tu")
        too_long = {"message": "too long", "codexErrorInfo": "contextWindowExceeded"}
        outs = self.feed(other, cx.turn_completed(T, "tu", status="failed", error=too_long))
        self.assertEqual([o.cls.__name__ for o in outs], ["TurnFinished"])

    def test_a_sub_agent_is_a_run_and_its_items_carry_it(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        outs = self.feed(wire, *cx.spawned(T, "tu", CHILD, "/root/list_files"),
                         *cx.child_turn(CHILD, "ct", "tu"), cx.command_started(CHILD, "ct", "ex-c", "ls"),
                         cx.command_completed(CHILD, "ct", "ex-c", "ls", "README.md\n"),
                         cx.token_usage(CHILD, "ct", total={"inputTokens": 10, "outputTokens": 2}),
                         *cx.child_done(T, "tu", CHILD, "/root/list_files", "ct"))
        names = [o.cls.__name__ for o in outs]
        self.assertEqual(names, ["RunStarted", "ToolStarted", "ToolFinished", "RunProgress", "RunFinished"])
        info = outs[0].fields["info"]
        self.assertEqual((info.run_id, info.title, info.depth, info.kind, info.spawn_call_id, info.background),
                         (CHILD, "list files", 1, "subagent", "call_spawn", False))
        self.assertTrue(all(o.fields["run"] == CHILD for o in outs))
        self.assertEqual(outs[-1].fields["status"], "completed")

    def test_a_run_alive_when_its_turn_ends_moves_to_the_background_once(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        self.feed(wire, *cx.spawned(T, "tu", CHILD, "/root/sleeper"), *cx.child_turn(CHILD, "ct", "tu"))
        outs = self.feed(wire, cx.status(T, "idle"), cx.turn_completed(T, "tu", status="interrupted"))
        names = [o.cls.__name__ for o in outs]
        self.assertEqual(names, ["TurnFinished", "RunProgress", "SessionIdle"])
        self.assertTrue(outs[1].fields["background"])
        wire.expect_turn("person")
        wire.start_turn("tu2")
        outs = self.feed(wire, cx.status(T, "idle"), cx.turn_completed(T, "tu2"))
        self.assertEqual([o.cls.__name__ for o in outs], ["TurnFinished", "SessionIdle"])

    def test_unknown_notifications_are_reported_once_and_known_noise_is_dropped(self) -> None:
        wire = self.wire()
        first = self.feed(wire, cx.notify("brand/new", {"threadId": T}))
        again = self.feed(wire, cx.notify("brand/new", {"threadId": T}))
        self.assertEqual(([o.fields["code"] for o in first], again), (["unknown_wire"], []))
        quiet = self.feed(wire, cx.notify("turn/moderationMetadata", {"threadId": T}),
                          cx.notify("configWarning", {"summary": "Codex could not find bubblewrap on PATH."}),
                          cx.notify("remoteControl/status/changed", {"status": "disabled"}))
        self.assertEqual(quiet, [])
        self.assertEqual(self.feed(wire, cx.notify("item/started", {"threadId": "someone-else", "item": {}})), [])

    def test_one_rate_limit_per_snapshot_its_worst_window(self) -> None:
        wire = self.wire()

        def two(weekly: float, five_hour: float, *, credits: bool = False) -> dict:
            snapshot = {**cx.limits(weekly, has_credits=credits),
                        "secondary": {"usedPercent": five_hour, "windowDurationMins": 300, "resetsAt": 1_793_000_000}}
            return cx.notify("account/rateLimits/updated", {"rateLimits": snapshot})
        outs = self.feed(wire, two(100, 5, credits=True))
        self.assertEqual([(o.fields["window"], o.fields["status"], o.fields["overage"]) for o in outs],
                         [("weekly", "ok", True)])                  # a calm window never follows it
        outs = self.feed(wire, two(40, 92))
        self.assertEqual([(o.fields["window"], o.fields["status"]) for o in outs], [("five_hour", "warning")])
        other = cx.notify("account/rateLimits/updated", {"rateLimits": {**cx.limits(100), "limitId": "gpt-6-sol"}})
        self.assertEqual(self.feed(wire, other), [])                # another limit never closes the account's pool

    def test_a_failure_retries_only_where_trying_again_can_help(self) -> None:
        cases = [("usageLimitExceeded", True), ({"httpConnectionFailed": {"httpStatusCode": 503}}, True),
                 ({"httpConnectionFailed": {"httpStatusCode": 403}}, False), ("badRequest", False)]
        for info, retried in cases:
            with self.subTest(info):
                wire = self.wire()
                wire.expect_turn("person")
                wire.start_turn("tu")
                outs = self.feed(wire, cx.turn_completed(T, "tu", status="failed",
                                                         error={"message": "x", "codexErrorInfo": info}))
                self.assertEqual(outs[0].cls.__name__ == "Error", retried)

    def test_a_finished_sub_agent_given_more_work_runs_again(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        self.feed(wire, *cx.spawned(T, "tu", CHILD, "/root/helper"), *cx.child_turn(CHILD, "ct", "tu"),
                  *cx.child_done(T, "tu", CHILD, "/root/helper", "ct"))
        self.assertEqual(wire.live_runs(), [])
        outs = self.feed(wire, *cx.child_turn(CHILD, "ct2", "tu"))
        self.assertEqual([o.cls.__name__ for o in outs], ["RunStarted"])
        self.assertEqual(wire.live_runs(), [CHILD])

    def test_a_system_error_ends_the_input(self) -> None:
        wire = self.wire()
        wire.expect_turn("person")
        wire.start_turn("tu")
        self.feed(wire, cx.turn_completed(T, "tu", status="failed"))
        outs = self.feed(wire, cx.notify("thread/status/changed", {"threadId": T, "status": {"type": "systemError"}}))
        self.assertEqual([o.cls.__name__ for o in outs], ["Error", "SessionIdle"])

    def test_rate_limits(self) -> None:
        wire = self.wire()
        warn = self.feed(wire, cx.rate_limits(85))[0].fields
        self.assertEqual((warn["status"], warn["window"], warn["utilization"]), ("warning", "weekly", 0.85))
        full = self.feed(wire, cx.rate_limits(100))[0].fields
        self.assertEqual((full["status"], full["overage"]), ("limited", False))
        paid = self.feed(wire, cx.rate_limits(100, has_credits=True))[0].fields
        self.assertEqual((paid["status"], paid["overage"]), ("ok", True))
        self.assertEqual(self.feed(wire, cx.rate_limits(100, has_credits=True)), [])     # unchanged: said once


class FakeCodex:
    """A runtime session on the fake app-server, in a temp state root and home."""

    def __init__(self, steps: list[dict], *, argv: list[dict] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = install("codex", self.root / "bin", scenario(steps, argv=argv or []))
        self.work = self.root / "work"
        self.work.mkdir()
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.fake.path), "HOME": str(self.home),
                                                "QUIRQ_STATE_ROOT": str(self.root / "state")})
        self.env.start()
        os.environ.pop("CODEX_HOME", None)
        self.runtime = CodexRuntime()
        self.events: list[c.Event] = []
        self.reader: asyncio.Task | None = None

    async def open(self, host: ScriptedHost | None = None, **spec) -> c.Session:
        self.host = host or ScriptedHost(decide=lambda req: ALLOW)
        self.session = await self.runtime.open(c.SessionSpec(cwd=str(self.work), **spec), self.host,
                                               session_id="cse_test")
        self.reader = asyncio.create_task(self._consume())
        return self.session

    async def _consume(self) -> None:
        async for ev in self.session.events():
            self.events.append(ev)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    async def finish(self) -> None:
        await asyncio.wait_for(self.session.close(), 20)
        if self.reader is not None:
            await asyncio.wait_for(self.reader, 20)
        self.env.stop()
        self.tmp.cleanup()

    def stdin(self, run: int = 0) -> list:
        return self.fake.invocations()[run]["stdin"]

    def mismatches(self) -> list:
        return [run.get("mismatch") for run in self.fake.invocations() if run.get("mismatch")]


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        if getattr(self, "cli", None) is not None:
            await self.cli.finish()

    async def turn(self, text: str = "go") -> None:
        await self.cli.session.send(c.UserInput(text=text))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)

    async def test_the_process_and_its_handshake(self) -> None:
        self.cli = FakeCodex([*cx.started(T)])
        await self.cli.open()
        run = self.cli.fake.invocations()[0]
        self.assertEqual(run["argv"], ["app-server", "--listen", "stdio://"])
        self.assertEqual(run["cwd"], str(self.cli.work))
        self.assertEqual(self.cli.session.native_id, T)
        self.assertEqual(self.cli.stdin()[0]["params"]["capabilities"], {"experimentalApi": True})
        start = self.cli.stdin()[2]
        self.assertEqual((start["method"], start["params"]["cwd"], start["params"]["sandbox"]),
                         ("thread/start", str(self.cli.work), "danger-full-access"))
        self.assertEqual(self.cli.mismatches(), [])

    async def test_resume_and_fork_name_the_thread(self) -> None:
        for method, extra in (("thread/resume", {}), ("thread/fork", {"fork": True})):
            with self.subTest(method):
                cli = FakeCodex([*cx.handshake(), *cx.thread_start(T, method=method, threadId=T)])
                await cli.open(resume=T, **extra)
                self.assertEqual(cli.session.native_id, T)
                self.assertEqual(cli.mismatches(), [])
                await cli.finish()

    async def test_a_threads_own_edit_is_accepted_here_and_a_command_goes_to_the_host(self) -> None:
        work_file = "/placeholder"
        self.cli = FakeCodex([])
        work_file = str(self.cli.work / "hello.txt")
        steps = [*cx.started(T), *cx.turn_start(T, "tu"),
                 cx.change_started(T, "tu", "fc", [cx.add(work_file, "hi\n")]),
                 cx.change_approval(0, T, "tu", "fc"), cx.approval_answer(0, "accept"),
                 cx.change_completed(T, "tu", "fc", [cx.add(work_file, "hi\n")]),
                 cx.command_started(T, "tu", "ex", "git push"), cx.command_approval(1, T, "tu", "ex", "git push"),
                 cx.approval_answer(1, "accept"), cx.command_completed(T, "tu", "ex", "git push", "ok\n"),
                 *cx.agent_message(T, "tu", "done"), *cx.turn_end(T, "tu")]
        self.cli.fake = install("codex", self.cli.root / "bin", scenario(steps))
        await self.cli.open(permissions=c.PermissionPolicy(mode="accept_edits"))
        await self.turn()
        self.assertEqual([(r.kind, r.command) for r in self.cli.host.permission_requests], [("command", "git push")])
        self.assertEqual(self.cli.mismatches(), [])
        self.assertNotIn("acceptWithExecpolicyAmendment", json.dumps(self.cli.stdin()))

    async def test_an_edit_outside_the_folder_and_any_coordinator_change_go_to_the_host(self) -> None:
        for spec, path in (({"permissions": c.PermissionPolicy(mode="accept_edits")}, "/etc/x"),
                           ({"orchestration": "coordinator", "permissions": c.PermissionPolicy(mode="deny_unasked"),
                             "tools": c.ToolPolicy(preset="read_only")}, None)):
            with self.subTest(spec):
                cli = FakeCodex([])
                target = path or str(cli.work / "x.txt")
                steps = [*cx.started(T), *cx.turn_start(T, "tu"),
                         cx.change_started(T, "tu", "fc", [cx.add(target, "x")]),
                         cx.change_approval(0, T, "tu", "fc"), cx.approval_answer(0, "decline"),
                         *cx.steer(T, "tu"),
                         cx.change_completed(T, "tu", "fc", [cx.add(target, "x")], status="declined"),
                         *cx.agent_message(T, "tu", "ok"), *cx.turn_end(T, "tu")]
                cli.fake = install("codex", cli.root / "bin", scenario(steps))
                host = ScriptedHost(decide=lambda req: c.PermissionDecision(allow=False, message="not here"))
                await cli.open(host, **spec)
                await cli.session.send(c.UserInput(text="write"))
                await wait_for_event(cli.events, c.SessionIdle, after=c.TurnFinished)
                self.assertEqual([(r.kind, r.paths) for r in host.permission_requests], [("create_file", (target,))])
                finished = [e for e in cli.of(c.ToolFinished) if e.call_id == "fc"]
                self.assertEqual([e.status for e in finished], ["denied"])
                note = [line for line in cli.stdin() if line.get("method") == "turn/steer"][0]
                self.assertIn("not here", note["params"]["input"][0]["text"])
                self.assertIn("XO system note", note["params"]["input"][0]["text"])
                self.assertEqual(cli.mismatches(), [])
                await cli.finish()

    async def test_codex_confirming_an_answer_never_cancels_the_deny_note(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "git push"),
                 cx.command_approval(0, T, "tu", "e1", "git push"), cx.approval_answer(0, "decline"),
                 cx.resolved(T, 0), *cx.steer(T, "tu"),
                 cx.command_completed(T, "tu", "e1", "git push", status="declined", exit_code=None),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=lambda r: c.PermissionDecision(allow=False, message="Ask first.")))
        await self.turn()
        self.assertEqual([e.decision.message for e in self.cli.of(c.PermissionResolved)], ["Ask first."])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_new_message_withdraws_a_plan_still_waiting(self) -> None:
        release = asyncio.Event()

        async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            await release.wait()
            return ALLOW

        plan_item = {"type": "plan", "id": "tu-plan", "text": "# Plan"}
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.item("item/completed", T, "tu", plan_item),
                 *cx.turn_end(T, "tu"), *cx.text_turn(T, "tu2", "fine")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=decide), permissions=c.PermissionPolicy(mode="plan"), model="m")
        await self.cli.session.send(c.UserInput(text="plan it"))
        await wait_for_event(self.cli.events, c.PermissionRequested)
        await self.cli.session.send(c.UserInput(text="actually, something else"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        resolved = self.cli.of(c.PermissionResolved)
        self.assertEqual([(e.decision.allow, e.decision.message) for e in resolved],
                         [(False, "codex withdrew the request")])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_held_approval_waits_then_session_scope_is_kept_here(self) -> None:
        release = asyncio.Event()
        answers = iter([c.PermissionDecision(allow=True, scope="session")])

        async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            await release.wait()
            return next(answers)

        steps = [*cx.started(T), *cx.turn_start(T, "tu"),
                 cx.command_started(T, "tu", "e1", "npm test"), cx.command_approval(0, T, "tu", "e1", "npm test"),
                 cx.approval_answer(0, "accept"), cx.command_completed(T, "tu", "e1", "npm test"),
                 cx.command_started(T, "tu", "e2", "npm test"), cx.command_approval(1, T, "tu", "e2", "npm test"),
                 cx.approval_answer(1, "accept"), cx.command_completed(T, "tu", "e2", "npm test"),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=decide))
        await self.cli.session.send(c.UserInput(text="test"))
        await wait_for_event(self.cli.events, c.PermissionRequested)
        await asyncio.sleep(0.5)
        self.assertEqual(self.cli.session.state, "waiting_host")
        self.assertFalse(self.cli.of(c.PermissionResolved))
        release.set()
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertEqual(len(self.cli.host.permission_requests), 1)
        self.assertEqual([e.decision.scope for e in self.cli.of(c.PermissionResolved)], ["session", "session"])

    async def test_a_host_that_fails_is_a_decline(self) -> None:
        def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            raise RuntimeError("boom")

        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "rm -rf x"),
                 cx.command_approval(0, T, "tu", "e1", "rm -rf x"), cx.approval_answer(0, "decline"),
                 *cx.steer(T, "tu"), cx.command_completed(T, "tu", "e1", "rm -rf x", status="declined", exit_code=None),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=decide))
        await self.turn()
        self.assertEqual([e.decision.message for e in self.cli.of(c.PermissionResolved)], ["host error"])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_stop_turn_deny_cancels(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "curl x"),
                 cx.command_approval(0, T, "tu", "e1", "curl x"), cx.approval_answer(0, "cancel"),
                 cx.status(T, "idle"), cx.turn_completed(T, "tu", status="interrupted")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=lambda r: c.PermissionDecision(allow=False, stop_turn=True)))
        await self.turn()
        self.assertEqual([e.status for e in self.cli.of(c.TurnFinished)], ["interrupted"])

    async def test_codex_settling_a_held_request_withdraws_it(self) -> None:
        async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            await asyncio.sleep(60)
            return ALLOW

        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "sleep 1"),
                 cx.command_approval(0, T, "tu", "e1", "sleep 1"), {"sleep": 0.3}, cx.resolved(T, 0),
                 cx.status(T, "idle"), cx.turn_completed(T, "tu", status="interrupted")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=decide))
        await self.turn()
        resolved = self.cli.of(c.PermissionResolved)
        self.assertEqual([(e.decision.allow, e.decision.message) for e in resolved],
                         [(False, "codex withdrew the request")])

    async def test_a_message_mid_turn_steers_it(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "sleep 5"),
                 *cx.steer(T, "tu", input=[{"type": "text", "text": "also this"}]),
                 cx.command_completed(T, "tu", "e1", "sleep 5"), *cx.agent_message(T, "tu", "both"),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost())
        first = await self.cli.session.send(c.UserInput(text="sleep"))
        await wait_for_event(self.cli.events, c.ToolStarted)
        second = await self.cli.session.send(c.UserInput(text="also this"))
        self.assertEqual(first, second)
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertEqual(self.cli.mismatches(), [])

    async def test_interrupt_keeps_the_thread_and_hidden_context_rides_additional_context(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "sleep 60"),
                 *cx.interrupted(T, "tu"),
                 *cx.text_turn(T, "tu2", "still here",
                               additionalContext={"xo": {"kind": "application", "value": "wake: a reply"}})]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="sleep"))
        await wait_for_event(self.cli.events, c.ToolStarted)
        await self.cli.session.interrupt(grace_s=5)
        await self.cli.session.send(c.UserInput(text="there?", context="wake: a reply", origin="wake"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished, count=2)
        self.assertEqual([e.status for e in self.cli.of(c.TurnFinished)], ["interrupted", "completed"])
        self.assertEqual([e.origin for e in self.cli.of(c.TurnStarted)], ["person", "wake"])
        self.assertEqual([e.status for e in self.cli.of(c.ToolFinished)], ["cancelled"])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_interrupt_that_gets_no_answer_terminates_the_group(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), expect({"method": "turn/interrupt"}), {"sleep": 30}]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="hang"))
        await self.cli.session.interrupt(grace_s=0.5)
        await wait_for_event(self.cli.events, c.SessionClosed)
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["interrupt_escalated"])
        self.assertEqual([e.status for e in self.cli.of(c.TurnFinished)], ["interrupted"])

    async def test_a_crash_mid_turn_is_events_not_an_exception(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "make"),
                 {"stderr": "panic: boom"}, {"exit": 101}]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.SessionClosed)
        names = [type(e).__name__ for e in self.cli.events]
        self.assertEqual(names[-4:], ["Error", "ToolFinished", "TurnFinished", "SessionClosed"])
        error = self.cli.of(c.Error)[0]
        self.assertEqual((error.code, error.fatal), ("process_died", True))
        self.assertIn("boom", error.message)
        with self.assertRaises(c.RuntimeFailure):
            await self.cli.session.send(c.UserInput(text="again"))

    async def test_open_fails_cleanly_when_codex_dies_or_the_folder_is_missing(self) -> None:
        self.cli = FakeCodex([expect({"method": "initialize"}), {"exit": 1}])
        host = ScriptedHost()
        with self.assertRaises(c.RuntimeFailure):
            await self.cli.open(host)
        live = {p.pid for p in live_processes()}
        self.assertFalse([h.pid for h in host.launcher.spawned if h.pid in live])
        self.cli.env.stop()
        self.cli.tmp.cleanup()
        self.cli = None
        missing = FakeCodex([])
        try:
            with self.assertRaises(c.RuntimeFailure) as caught:
                await missing.runtime.open(c.SessionSpec(cwd=str(missing.root / "gone")), ScriptedHost(),
                                           session_id="cse_x")
            self.assertEqual(caught.exception.code, "spec_refused")
        finally:
            missing.env.stop()
            missing.tmp.cleanup()

    async def test_questions_go_to_answer_question(self) -> None:
        question = {"emit": {"id": 5, "method": "item/tool/requestUserInput", "params": {
            "threadId": T, "turnId": "tu", "itemId": "q1", "isBlocking": True,
            "questions": [{"id": "colour", "header": "Colour", "question": "Which colour?",
                           "options": [{"label": "blue", "description": "calm"}]}]}}}
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), question,
                 expect({"id": 5, "result": {"answers": {"colour": {"answers": ["blue"]}}}}), *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(answers={"colour": "blue"}))
        await self.turn()
        self.assertEqual(self.cli.host.questions[0].questions[0]["question"], "Which colour?")
        self.assertEqual([e.answer for e in self.cli.of(c.QuestionAnswered)], [{"colour": "blue"}])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_requests_xo_doesnt_serve_get_an_error_never_silence(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"),
                 {"emit": {"id": 7, "method": "attestation/generate", "params": {}}},
                 expect({"id": 7, "error": {"code": -32601}}),
                 {"emit": {"id": 8, "method": "brand/newRequest", "params": {}}},
                 expect({"id": 8, "error": {"code": -32601}}),
                 {"emit": {"id": 9, "method": "currentTime/read", "params": {}}}, expect({"id": 9}),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.turn()
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["unknown_wire"])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_spec_deny_declines_without_asking(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.command_started(T, "tu", "e1", "ls"),
                 cx.command_approval(0, T, "tu", "e1", "ls"), cx.approval_answer(0, "decline"), *cx.steer(T, "tu"),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(tools=c.ToolPolicy(deny=("shell",)))
        await self.turn()
        self.assertEqual(self.cli.host.permission_requests, [])
        self.assertFalse(self.cli.of(c.PermissionResolved)[0].decision.allow)

    async def test_rate_limit_guard_refuses_send_until_the_reset(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.rate_limits(100, resets_at=4_000_000_000),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.turn()
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.session.send(c.UserInput(text="more"))
        self.assertEqual(caught.exception.code, "rate_limited")
        usage = await self.cli.runtime.usage()
        self.assertEqual([(w.name, w.status) for w in usage.windows], [("weekly", "limited")])

    async def test_a_new_login_is_reported_at_the_turn_boundary(self) -> None:
        steps = [*cx.started(T), *cx.text_turn(T, "tu", "one")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        (self.cli.home / ".codex" / "auth.json").write_text(json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"account_id": "acct-2", "refresh_token": "r"}}), encoding="utf-8")
        await self.turn()
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["credentials_changed"])

    async def test_update_applies_from_the_next_turn(self) -> None:
        steps = [*cx.started(T), *cx.text_turn(T, "tu", "x", model="gpt-6-luna", effort="low",
                                               approvalPolicy="untrusted")]
        self.cli = FakeCodex(steps)
        await self.cli.open(permissions=c.PermissionPolicy(mode="ask"))
        report = await self.cli.session.update(model="gpt-6-luna", effort="low", permission_mode="accept_edits")
        self.assertEqual({i.feature: i.support for i in report.items},
                         {"update.model": "native", "update.effort": "native", "update.permission_mode": "native"})
        refused = await self.cli.session.update(permission_mode="bypass")
        self.assertEqual(refused.items[0].support, "refused")
        await self.turn()
        self.assertEqual(self.cli.mismatches(), [])

    async def test_runs_and_stopping_one(self) -> None:
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), *cx.spawned(T, "tu", CHILD, "/root/sleeper"),
                 *cx.child_turn(CHILD, "ct", "tu"), cx.command_started(CHILD, "ct", "e1", "sleep 60"),
                 *cx.call("turn/interrupt", {}, threadId=CHILD, turnId="ct"),
                 cx.status(CHILD, "idle"),
                 cx.notify("turn/completed", {"threadId": CHILD, "turn": cx.turn_obj("ct", status="interrupted")}),
                 *cx.steer(T, "tu"), *cx.agent_message(T, "tu", "stopped"), *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="spawn"))
        await wait_for_event(self.cli.events, c.ToolStarted)
        await self.cli.session.stop_run(CHILD)
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        finished = self.cli.of(c.RunFinished)
        self.assertEqual([(e.run, e.status) for e in finished], [(CHILD, "stopped")])
        note = [line for line in self.cli.stdin() if line.get("method") == "turn/steer"][0]
        self.assertIn("stopped sub-agent sleeper", note["params"]["input"][0]["text"])
        with self.assertRaises(c.RuntimeFailure):
            await self.cli.session.stop_run(CHILD)
        with self.assertRaises(c.RuntimeFailure):
            await self.cli.session.send_to_run(CHILD, c.UserInput(text="x"))
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_plan_is_put_to_the_host_and_carried_out_once_approved(self) -> None:
        plan_item = {"type": "plan", "id": "tu-plan", "text": "# Plan\n1. Add CONTRIBUTING.md"}
        steps = [*cx.started(T), *cx.turn_start(T, "tu", collaborationMode={"mode": "plan"}),
                 cx.item("item/started", T, "tu", {**plan_item, "text": ""}),
                 cx.notify("item/plan/delta", {"threadId": T, "turnId": "tu", "itemId": "tu-plan",
                                               "delta": plan_item["text"]}),
                 cx.item("item/completed", T, "tu", plan_item), *cx.turn_end(T, "tu"),
                 *cx.text_turn(T, "tu2", "done", collaborationMode={"mode": "default"},
                               input=[{"type": "text", "text": "The person approved your plan. Carry it out now."}])]
        self.cli = FakeCodex(steps)
        await self.cli.open(permissions=c.PermissionPolicy(mode="plan"), model="gpt-6-luna")
        await self.cli.session.send(c.UserInput(text="plan it"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        finished = self.cli.of(c.TurnFinished)
        self.assertEqual([e.text for e in finished], ["# Plan\n1. Add CONTRIBUTING.md", "done"])
        asked = self.cli.host.permission_requests
        self.assertEqual([(r.kind, r.plan) for r in asked], [("plan", "# Plan\n1. Add CONTRIBUTING.md")])
        self.assertEqual([e.origin for e in self.cli.of(c.TurnStarted)], ["person", "system"])
        self.assertEqual(len(self.cli.of(c.SessionIdle)), 1)          # one input: the plan and its carrying out
        self.assertEqual(self.cli.mismatches(), [])

    async def test_a_plan_turned_down_ends_the_input_and_the_reason_goes_with_the_next(self) -> None:
        plan_item = {"type": "plan", "id": "tu-plan", "text": "# Plan"}
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.item("item/completed", T, "tu", plan_item),
                 *cx.turn_end(T, "tu"),
                 *cx.text_turn(T, "tu2", "revised", additionalContext={"xo": {
                     "kind": "application", "value": "XO denied the plan: Smaller, please."}})]
        self.cli = FakeCodex(steps)
        smaller = c.PermissionDecision(allow=False, message="Smaller, please.")
        await self.cli.open(ScriptedHost(decide=lambda r: smaller), permissions=c.PermissionPolicy(mode="plan"),
                            model="gpt-6-luna")
        await self.turn("plan it")
        await self.cli.session.send(c.UserInput(text="again"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished, count=2)
        self.assertEqual(self.cli.mismatches(), [])

    async def test_stop_while_a_plan_waits_withdraws_it_and_ends_the_input(self) -> None:
        async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            await asyncio.sleep(60)
            return ALLOW

        plan_item = {"type": "plan", "id": "tu-plan", "text": "# Plan"}
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.item("item/completed", T, "tu", plan_item),
                 *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open(ScriptedHost(decide=decide), permissions=c.PermissionPolicy(mode="plan"), model="m")
        await self.cli.session.send(c.UserInput(text="plan it"))
        await wait_for_event(self.cli.events, c.PermissionRequested)
        await self.cli.session.interrupt(grace_s=2)
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        self.assertEqual([e.decision.message for e in self.cli.of(c.PermissionResolved)],
                         ["codex withdrew the request"])

    async def test_a_turn_codex_began_itself_takes_a_message_its_start_was_refused_for(self) -> None:
        steps = [*cx.started(T),
                 expect({"method": "turn/start"}, id="id"),
                 cx.status(T, "active"), cx.notify("turn/started", {"threadId": T, "turn": cx.turn_obj("own")}),
                 {"emit": {"id": "$id", "error": {"code": -32600, "message": "a turn is already running"}}},
                 *cx.steer(T, "own"), *cx.agent_message(T, "own", "joined"), *cx.turn_end(T, "own")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        turn = await self.cli.session.send(c.UserInput(text="hello"))
        self.assertEqual(turn, "own")
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        # The message joined codex's own turn, so the turn counts as the person's.
        self.assertEqual([e.origin for e in self.cli.of(c.TurnStarted)], ["person"])
        self.assertEqual(self.cli.mismatches(), [])

    async def test_session_started_is_always_the_first_event(self) -> None:
        failed = cx.notify("mcpServer/startupStatus/updated", {"threadId": T, "name": "x", "status": "failed",
                                                                "error": "connection refused"})
        steps = [*cx.handshake(), expect({"method": "thread/start"}, id="id"), failed,
                 {"emit": {"id": "$id", "result": {"thread": cx.thread(T), "model": cx.MODEL}}}]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.cli.session.close()
        await asyncio.wait_for(self.cli.reader, 20)
        self.assertEqual([type(e).__name__ for e in self.cli.events][:2], ["SessionStarted", "Notice"])

    async def test_an_mcp_tool_codex_asks_about_goes_to_the_host_by_name(self) -> None:
        ask = {"emit": {"id": 3, "method": "mcpServer/elicitation/request", "params": {
            "threadId": T, "turnId": "tu", "serverName": "composio", "mode": "form",
            "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"],
                      "tool_params": {"query": "x"}},
            "message": 'Allow the composio MCP server to run tool "SEARCH"?',
            "requestedSchema": {"type": "object", "properties": {}}}}}
        mcp_item = {"type": "mcpToolCall", "id": "mc1", "server": "composio", "tool": "SEARCH",
                    "arguments": {"query": "x"}, "status": "inProgress"}
        steps = [*cx.started(T), *cx.turn_start(T, "tu"), cx.item("item/started", T, "tu", mcp_item), ask,
                 expect({"id": 3, "result": {"action": "accept"}}),
                 cx.item("item/completed", T, "tu", {**mcp_item, "status": "completed"}), *cx.turn_end(T, "tu")]
        self.cli = FakeCodex(steps)
        await self.cli.open()
        await self.turn()
        req = self.cli.host.permission_requests[0]
        self.assertEqual((req.kind, req.mcp, req.summary, req.call_id),
                         ("mcp", ("composio", "SEARCH"), "call `SEARCH` on `composio`", "mc1"))
        self.assertEqual(self.cli.mismatches(), [])

    async def test_close_leaves_no_process_and_is_idempotent(self) -> None:
        self.cli = FakeCodex(cx.started(T))
        await self.cli.open()
        pid = self.cli.host.launcher.spawned[0].pid
        await self.cli.session.close()
        await self.cli.session.close()
        self.assertNotIn(pid, {p.pid for p in live_processes()})
        self.assertEqual([e.reason for e in self.cli.of(c.SessionClosed)], ["closed"])


class AccountTests(unittest.TestCase):
    def test_fingerprints_follow_the_login_not_its_token_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            auth = home / "auth.json"
            self.assertIsNone(account_fingerprint(home, {}))
            auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "a1", "refresh_token": "r1",
                                                                            "access_token": "x1"}}))
            first, account = login_fingerprint(home, {}), account_fingerprint(home, {})
            auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "a1", "refresh_token": "r2",
                                                                            "access_token": "x2"}}))
            self.assertEqual((login_fingerprint(home, {}), account_fingerprint(home, {})), (first, account))
            auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "a2"}}))
            self.assertNotEqual(login_fingerprint(home, {}), first)
            self.assertNotEqual(account_fingerprint(home, {}), account)
            self.assertNotIn("r1", account + first)

    def test_the_guard_keeps_the_plan_and_each_window(self) -> None:
        guard = UsageGuard()
        guard.update({**cx.limits(40), "secondary": {"usedPercent": 90, "windowDurationMins": 300, "resetsAt": 1}})
        snapshot = guard.snapshot()
        self.assertEqual(snapshot.plan, "pro")
        self.assertEqual([(w.name, w.status) for w in snapshot.windows], [("five_hour", "warning"), ("weekly", "ok")])


class ModelsTests(unittest.IsolatedAsyncioTestCase):
    def test_model_list_rows(self) -> None:
        models = models_from(cx.LIVE_MODELS)
        self.assertEqual([m.id for m in models], ["gpt-6.1-sol", "gpt-6-luna"])
        sol = models[0]
        self.assertEqual((sol.is_default, sol.efforts, sol.default_effort, sol.takes_effort, sol.family),
                         (True, ("low", "medium", "high", "xhigh", "max"), "medium", True, None))
        self.assertEqual(sol.description, "Latest workhorse model for coding and everyday work.")

    async def test_the_probe_starts_no_thread_and_reads_the_rate_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = install("codex", root / "bin", scenario(cx.models_probe(used=85), argv=[cx.version()]))
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(fake.path), "HOME": str(root)}):
                os.environ.pop("CODEX_HOME", None)
                runtime = CodexRuntime()
                models = await runtime.models()
                again = await runtime.models()
                usage = await runtime.usage()
                info = await runtime.describe()
            sent = [line.get("method") for run in fake.invocations() for line in run["stdin"]]
        self.assertEqual([m.id for m in models], [m.id for m in again])
        self.assertNotIn("thread/start", sent)
        self.assertEqual(sent.count("model/list"), 1)
        self.assertEqual([w.status for w in usage.windows], ["warning"])
        self.assertEqual((info.cli_version, info.logged_in), ("0.162.1", False))


class ModelsRouteTests(unittest.TestCase):
    def test_api_models_rows_keep_todays_shape(self) -> None:
        from services.cowork_agent.adapters.codex import models
        from services.cowork_agent.adapters.codex.runtime import core as runtime_mod

        with tempfile.TemporaryDirectory() as tmp:
            fake = install("codex", Path(tmp) / "bin", scenario(cx.models_probe()))
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(fake.path), "HOME": tmp}), \
                    mock.patch.object(runtime_mod, "_RUNTIME", CodexRuntime(home=Path(tmp) / ".codex")):
                rows = models.list_models()        # called the way the sync route calls it
        self.assertEqual([r["id"] for r in rows], ["codex/gpt-6.1-sol", "codex/gpt-6-luna"])
        for row in rows:
            self.assertEqual(set(row), {"id", "name", "provider_id", "capabilities", "pricing", "metadata"})
            self.assertEqual((row["provider_id"], row["pricing"]), ("codex", {"prompt": 0, "completion": 0}))
            self.assertIn("max_context", row["capabilities"])
        self.assertEqual(rows[0]["metadata"], {"model": "gpt-6.1-sol", "efforts": ["low", "medium", "high", "xhigh",
                                                                                   "max"], "default": True})

    def test_api_models_lists_nothing_when_the_cli_is_missing(self) -> None:
        from services.cowork_agent.adapters.codex import models
        from services.cowork_agent.adapters.codex.runtime import core as runtime_mod

        with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": "/nonexistent/codex"}), \
                mock.patch.object(runtime_mod, "_RUNTIME", CodexRuntime()), \
                self.assertLogs("services.cowork_agent.adapters.codex.models", "WARNING") as logs:
            self.assertEqual(models.list_models(), [])
        self.assertIn("cli_missing", logs.output[0])

    def test_three_threads_at_once_get_the_models_from_one_spawn(self) -> None:
        from services.cowork_agent.adapters.codex import models
        from services.cowork_agent.adapters.codex.runtime import core as runtime_mod

        with tempfile.TemporaryDirectory() as tmp:
            fake = install("codex", Path(tmp) / "bin", scenario([{"sleep": 0.5}, *cx.models_probe()]))
            out: list = [None] * 3

            def call(i: int) -> None:
                out[i] = models.list_models()

            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(fake.path), "HOME": tmp}), \
                    mock.patch.object(runtime_mod, "_RUNTIME", CodexRuntime(home=Path(tmp) / ".codex")):
                threads = [threading.Thread(target=call, args=(i,)) for i in range(3)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
            spawns = len(fake.invocations())
        self.assertEqual([[r["id"] for r in rows] for rows in out], [["codex/gpt-6.1-sol", "codex/gpt-6-luna"]] * 3)
        self.assertEqual(spawns, 1)


class OneshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_oneshot_is_isolated_and_reads_its_answer_from_the_json_events(self) -> None:
        from services.cowork_agent.adapters.codex.oneshot_prompt import oneshot

        events = [{"emit": {"type": "thread.started", "thread_id": "t"}}, {"emit": {"type": "turn.started"}},
                  {"emit": {"type": "item.completed", "item": {"id": "i", "type": "agent_message",
                                                                "text": '{"title": "Fix the build"}'}}},
                  {"emit": {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                                "output_tokens": 9}}}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = install("codex", Path(tmp) / "bin", scenario(events))
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(fake.path), "HOME": tmp}):
                one = oneshot()
                self.assertEqual(one.check().weakest("isolation").support, "native")
                result = await one.complete(c.OneshotRequest(prompt="Name this chat", model="gpt-6-luna",
                                                             effort="low", output_schema={"type": "object"}),
                                            ScriptedHost().launcher)
            run = fake.invocations()[0]
        self.assertEqual(result.structured, {"title": "Fix the build"})
        self.assertEqual((result.usage.input, result.usage.cache_read, result.usage.output), (30, 20, 9))
        argv = run["argv"]
        for flag in ("exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertLessEqual({"shell_tool", "unified_exec", "code_mode_host", "apps", "plugins"}, disabled)
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("Name this chat", " ".join(argv))
        self.assertEqual(run["stdin"], ["Name this chat"])
        self.assertFalse(any(flag in " ".join(argv) for flag in SKIP_FLAGS))

    async def test_a_failed_oneshot_is_a_runtime_failure(self) -> None:
        from services.cowork_agent.adapters.codex.oneshot_prompt import oneshot

        events = [{"emit": {"type": "turn.failed", "error": {"message": "usage limit"}}}, {"exit": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = install("codex", Path(tmp) / "bin", scenario(events))
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(fake.path), "HOME": tmp}):
                with self.assertRaises(c.RuntimeFailure) as caught:
                    await oneshot().complete(c.OneshotRequest(prompt="x"), ScriptedHost().launcher)
        self.assertIn("usage limit", caught.exception.message)


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_session_and_its_sub_agent_read_from_codexs_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            native, run = write_rollouts(home / ".codex")
            runtime = CodexRuntime(home=home / ".codex")
            runs = await runtime.list_runs(native)
            page = await runtime.read_run(native, run, limit=100)
            session = await runtime.read_session(native, limit=100)
            listed = await runtime.list_sessions()
            with self.assertRaises(c.RuntimeFailure) as caught:
                await runtime.read_run(native, "01a165ae-0000-7000-8000-000000000000")
            self.assertEqual(caught.exception.code, "session_not_found")
            with self.assertRaises(c.RuntimeFailure) as caught:
                await runtime.read_run(native, run, after="not-a-cursor")
            self.assertEqual(caught.exception.code, "protocol")
        self.assertEqual([(r.run_id, r.title, r.depth, r.parent_run_id) for r in runs], [(run, "list files", 1, None)])
        self.assertEqual([i.type for i in page.items], ["input", "tool_call", "tool_result", "text"])
        self.assertEqual(page.items[1].kind, "command")
        self.assertEqual((page.items[2].status, page.items[2].text), ("ok", "README.md\n"))
        self.assertEqual([i.type for i in session.items], ["input", "text"])
        self.assertEqual([s.native_id for s in listed], [native])


def write_rollouts(codex_home: Path) -> tuple[str, str]:
    """A session (``T``) with one sub-agent run (``CHILD``), as codex 0.162.1
    writes them."""
    day = codex_home / "sessions" / "2026" / "10" / "22"
    day.mkdir(parents=True)

    def record(ordinal: int, kind: str, payload: dict, ts: str = "2026-10-22T22:14:33.500Z") -> str:
        return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": kind, "payload": payload}) + "\n"

    root = [
        record(0, "session_meta", {"id": T, "session_id": T, "cwd": "/w", "cli_version": "0.162.1",
                                   "source": "vscode"}),
        record(1, "event_msg", {"type": "item_completed", "thread_id": T, "item": {
            "type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "List the files"}]}}),
        record(2, "event_msg", {"type": "item_completed", "thread_id": T, "item": {
            "type": "SubAgentActivity", "id": "call_1", "kind": "started", "agent_thread_id": CHILD,
            "agent_path": "/root/list_files"}}),
        record(3, "event_msg", {"type": "item_completed", "thread_id": T, "item": {
            "type": "AgentMessage", "id": "m1", "content": [{"type": "Text", "text": "README.md"}],
            "phase": "final_answer"}}),
    ]
    child = [
        record(0, "session_meta", {"id": CHILD, "session_id": T, "parent_thread_id": T, "cwd": "/w",
                                   "source": {"subagent": {"thread_spawn": {
                                       "parent_thread_id": T, "depth": 1, "agent_path": "/root/list_files",
                                       "agent_nickname": "Kant", "agent_role": None}}},
                                   "agent_path": "/root/list_files"}),
        record(1, "response_item", {"type": "agent_message", "author": "/root", "recipient": "/root/list_files",
                                    "content": [{"type": "input_text", "text": "Task: run ls"}]}),
        record(2, "event_msg", {"type": "item_completed", "thread_id": CHILD, "item": {
            "type": "CommandExecution", "id": "exec-1", "command": ["/bin/bash", "-lc", "ls"], "cwd": "file:///w",
            "status": "completed", "aggregated_output": "README.md\n", "exit_code": 0}}),
        record(3, "event_msg", {"type": "item_completed", "thread_id": CHILD, "item": {
            "type": "AgentMessage", "id": "m2", "content": [{"type": "Text", "text": "README.md"}],
            "phase": "final_answer"}}),
    ]
    (day / f"rollout-2026-10-22T22-14-29-{T}.jsonl").write_text("".join(root), encoding="utf-8")
    (day / f"rollout-2026-10-22T22-14-33-{CHILD}.jsonl").write_text("".join(child), encoding="utf-8")
    return T, CHILD


if __name__ == "__main__":
    unittest.main()
