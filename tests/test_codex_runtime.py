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

import json
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.adapters.codex.runtime.account import UsageGuard, account_fingerprint, login_fingerprint
from services.cowork_agent.adapters.codex.runtime.env import credential_vars, session_env
from services.cowork_agent.adapters.codex.runtime.rpc import classify, version_of
from services.cowork_agent.adapters.codex.runtime.tools import describe_change, own_edit, protected, unwrap
from services.cowork_agent.adapters.codex.runtime.wire import Wire, thinking_steps
from tests.fakes import codex as cx

T = "01a165ad-83f1-72e2-a03d-5bfa9b662c93"
CHILD = "01a165ae-2158-7443-8244-800f405be19e"
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


if __name__ == "__main__":
    unittest.main()
