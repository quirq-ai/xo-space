"""Claude's usage, as the space advisory needs it (storm plan W4, "Claude's
usage in Settings › Runtimes"): every window claude reports, its paid-usage
facts, a report with no number keeping the last, the read through claude's
own ``get_usage`` (no model call, no token in XO's hands), and each turn's own
cost and context size.

The fixtures are recorded: one of each distinct ``rate_limit_event`` shape in
the turn logs of 11-11, and the usage endpoint's answer the same day.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime, probe, usage_read
from services.cowork_agent.adapters.claude_code.runtime.account import TOKEN_VARS, UsageGuard
from services.cowork_agent.adapters.claude_code.runtime.wire import Wire
from tests.fakes import claude, expect, install, scenario

FIXTURES = Path(__file__).parent / "fixtures" / "advisory"
EVENTS = json.loads((FIXTURES / "claude_rate_limit_events.json").read_text())["events"]
ENDPOINT = json.loads((FIXTURES / "claude_oauth_usage.json").read_text())
NOT_POSSIBLE = c.PaidUsage(possible=False, in_use=False, status="rejected", disabled_reason="org_level_disabled")


def windows(outs: list) -> list[tuple]:
    return [(o.fields["window"], o.fields["status"], o.fields["utilization"], o.fields["resets_at"],
             o.fields["overage"]) for o in outs if o.cls is c.RateLimit]


class RateLimitTests(unittest.TestCase):
    def wire(self) -> Wire:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=False)
        return wire

    def test_both_windows_with_their_numbers(self) -> None:
        outs = self.wire().line(EVENTS[1])
        self.assertEqual(windows(outs), [("seven_day", "ok", 0.03, "2026-11-17T18:00:00Z", False),
                                         ("five_hour", "ok", 0.41, "2026-11-11T17:40:00Z", False)])
        self.assertEqual({o.fields["paid"] for o in outs}, {NOT_POSSIBLE})

    def test_the_limiting_window_comes_last_and_takes_the_worse_status(self) -> None:
        # The top level warns on the weekly window while its own entry still says allowed.
        outs = self.wire().line(EVENTS[5])
        self.assertEqual(windows(outs), [("five_hour", "ok", 0.33, "2026-10-28T21:00:00Z", False),
                                         ("seven_day", "warning", 0.38, "2026-11-03T18:00:00Z", False)])

    def test_a_rejection_with_no_top_level_number_takes_the_windows(self) -> None:
        outs = self.wire().line(EVENTS[4])
        self.assertEqual(windows(outs)[-1], ("five_hour", "limited", 1.0, "2026-10-28T21:00:00Z", False))

    def test_another_window_rejected_while_requests_are_allowed_is_a_warning(self) -> None:
        info = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1793221200,
                "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1793221200, "status": "allowed"},
                                   "seven_day_overage_included": {"utilization": 1.0, "resetsAt": 1793728800,
                                                                  "status": "rejected"}}}
        outs = self.wire().line({"type": "rate_limit_event", "rate_limit_info": info})
        self.assertEqual([(w[0], w[1]) for w in windows(outs)],
                         [("seven_day_overage_included", "warning"), ("five_hour", "ok")])
        self.assertEqual(outs[0].fields["paid"], c.PaidUsage())     # nothing said about paid usage: unknown

    def test_an_older_claude_without_unified_windows_reports_the_top_level(self) -> None:
        info = {k: v for k, v in EVENTS[2]["rate_limit_info"].items() if k != "unifiedWindows"}
        outs = self.wire().line({"type": "rate_limit_event", "rate_limit_info": info})
        self.assertEqual(windows(outs), [("seven_day", "warning", 0.84, "2026-11-12T23:00:00Z", False)])

    def test_paid_usage_in_use_rides_on_the_limiting_window_only(self) -> None:
        info = {**EVENTS[1]["rate_limit_info"], "overageStatus": "allowed", "isUsingOverage": True}
        outs = self.wire().line({"type": "rate_limit_event", "rate_limit_info": info})
        self.assertEqual([(w[0], w[4]) for w in windows(outs)], [("seven_day", False), ("five_hour", True)])
        self.assertEqual(outs[0].fields["paid"], c.PaidUsage(possible=True, in_use=True, status="allowed",
                                                            disabled_reason="org_level_disabled"))


class GuardTests(unittest.TestCase):
    def test_a_report_with_no_number_keeps_the_last_for_the_same_window(self) -> None:
        guard = UsageGuard()
        guard.update(status="ok", window="five_hour", resets_at="2026-11-12T01:40:00Z", utilization=0.42,
                     overage=False, paid=NOT_POSSIBLE)
        guard.update(status="warning", window="five_hour", resets_at="2026-11-12T01:40:00Z", utilization=None,
                     overage=False, paid=c.PaidUsage(in_use=False))
        (window,) = guard.snapshot().windows
        self.assertEqual((window.status, window.utilization), ("warning", 0.42))
        self.assertEqual(guard.snapshot().paid, NOT_POSSIBLE)        # each fact kept until claude says another
        guard.update(status="ok", window="five_hour", resets_at="2026-11-12T06:40:00Z", utilization=None,
                     overage=False)
        self.assertIsNone(guard.snapshot().windows[0].utilization)   # a new window's number isn't the old one's


class TurnUsageTests(unittest.TestCase):
    @staticmethod
    def turn(wire: Wire, total: float, *, context: dict | None = None) -> c.TokenUsage:
        wire.start_turn("person")
        if context is not None:
            wire.line({"type": "assistant", "parent_tool_use_id": None,
                       "message": {"id": f"msg_{total}", "content": [], "usage": context}})
        outs = wire.line({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                          "total_cost_usd": total, "usage": {"input_tokens": 2, "output_tokens": 30}})
        (end,) = [o.fields for o in outs if o.cls is c.TurnFinished]
        return end["usage"]

    def test_a_new_session_reports_each_turns_own_cost(self) -> None:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=False)
        first, second = self.turn(wire, 0.25), self.turn(wire, 0.4)
        self.assertEqual((first.cost_usd, first.session_cost_usd), (0.25, 0.25))
        self.assertEqual((second.cost_usd, second.session_cost_usd), (0.15, 0.4))

    def test_a_resumed_session_never_reports_its_running_total_as_a_turns_cost(self) -> None:
        # Recorded: a coordinator's totals 785.40 then 789.15 then 789.47 across turns.
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=True)
        first, second = self.turn(wire, 785.397443), self.turn(wire, 789.1506966)
        self.assertEqual((first.cost_usd, first.session_cost_usd), (None, 785.397443))
        self.assertEqual(second.cost_usd, 3.753254)

    def test_a_restart_forgets_the_total_and_a_drop_is_unknown(self) -> None:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=False)
        self.turn(wire, 2.0)
        wire.process_started(resumed=True)                  # restart with --resume (a new effort)
        self.assertIsNone(self.turn(wire, 2.5).cost_usd)
        self.assertIsNone(self.turn(wire, 0.1).cost_usd)    # a total that fell: not a turn's spend
        self.assertEqual(self.turn(wire, 0.3).cost_usd, 0.2)

    def test_an_interrupted_turns_zero_total_never_becomes_the_next_turns_base(self) -> None:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=False)
        self.turn(wire, 5.0)
        wire.start_turn("person")
        outs = wire.line({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "terminal_reason": "aborted_streaming", "total_cost_usd": 0,
                          "usage": {"input_tokens": 0, "output_tokens": 0}})
        (end,) = [o.fields for o in outs if o.cls is c.TurnFinished]
        self.assertEqual((end["status"], end["usage"]), ("interrupted", None))
        self.assertEqual(self.turn(wire, 5.3).cost_usd, 0.3)            # not 5.3: the session's total

    def test_the_context_size_is_the_last_root_calls(self) -> None:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.process_started(resumed=False)
        # Recorded: the turn's last call read 140,707 from the cache, wrote 263, sent 2 and wrote 390.
        usage = self.turn(wire, 1.0, context={"input_tokens": 2, "cache_creation_input_tokens": 263,
                                              "cache_read_input_tokens": 140707, "output_tokens": 390})
        self.assertEqual(usage.context_tokens, 141362)
        self.assertIsNone(self.turn(wire, 1.1).context_tokens)      # a turn with no call: not known


class UsageReadTests(unittest.IsolatedAsyncioTestCase):
    def test_the_endpoints_answer_as_windows_and_paid_facts(self) -> None:
        snapshot = usage_read.snapshot(ENDPOINT, account="acct-70c53cc54fd1", plan="max")
        self.assertEqual([(w.name, w.status, w.utilization, w.resets_at) for w in snapshot.windows],
                         [("five_hour", None, 0.51, "2026-11-12T01:40:00Z"),
                          ("seven_day", None, 0.08, "2026-11-17T18:00:00Z")])     # no status said: none guessed
        self.assertEqual((snapshot.account, snapshot.plan, snapshot.source), ("acct-70c53cc54fd1", "max", "measured"))
        self.assertEqual(snapshot.paid, c.PaidUsage(possible=False))

    def test_paid_is_possible_when_any_channel_is_on_and_not_possible_only_when_all_are_off(self) -> None:
        off_extra = {"is_enabled": False, "spend_limit_reached": False}
        off_spend = {"enabled": False, "balance": None, "auto_reload": None}

        def possible(extra=None, spend=None):
            return usage_read._paid({"extra_usage": {**off_extra, **(extra or {})},
                                     "spend": {**off_spend, **(spend or {})}}).possible
        self.assertIs(possible(), False)                                       # every channel off
        self.assertIs(possible(extra={"is_enabled": True}), True)
        self.assertIs(possible(extra={"is_enabled": True, "spend_limit_reached": True}), False)
        self.assertIs(possible(spend={"enabled": True}), True)                   # usage credits on
        self.assertIs(possible(spend={"balance": 50}), True)                     # credits left
        self.assertIs(possible(spend={"balance": {"amount_minor": 1250, "exponent": 2}}), True)
        self.assertIs(possible(spend={"auto_reload": {"enabled": True}}), True)
        self.assertIsNone(usage_read._paid({"extra_usage": off_extra}).possible)   # spend not said: unknown
        self.assertEqual(usage_read._paid({"spend": {"balance": {"amount_minor": 1250, "exponent": 2}}}).credits,
                         12.5)

    def test_a_login_by_api_key_is_paid_usage_with_no_window(self) -> None:
        from services.cowork_agent.adapters.claude_code.runtime.account import api_key_login
        self.assertTrue(api_key_login({"ANTHROPIC_API_KEY": "sk-test"}))
        self.assertFalse(api_key_login({}))
        snapshot = usage_read.api_key_usage("acct-1")
        self.assertEqual((snapshot.windows, snapshot.paid, snapshot.source),
                         ((), c.PaidUsage(possible=True, in_use=True), "measured"))

    def test_a_used_up_or_locked_window_is_limited(self) -> None:
        body = {"five_hour": {"utilization": 100.0, "resets_at": "2026-11-12T01:40:00+00:00"},
                "seven_day": {"utilization": 40.0, "resets_at": "2026-11-17T18:00:00+00:00",
                              "locked_reason": "admin"}}
        snapshot = usage_read.snapshot(body, account="a", plan=None)
        self.assertEqual([w.status for w in snapshot.windows], ["limited", "limited"])
        self.assertEqual(snapshot.paid, c.PaidUsage())

    def test_claudes_get_usage_answer_is_the_snapshot(self) -> None:
        body = {"rate_limits_available": True, "rate_limits": ENDPOINT, "subscription_type": "max",
                "session": {}}
        snapshot = usage_read.from_cli(body, account="acct-1")
        self.assertEqual([(w.name, w.utilization) for w in snapshot.windows],
                         [("five_hour", 0.51), ("seven_day", 0.08)])
        self.assertEqual((snapshot.account, snapshot.plan), ("acct-1", "max"))

    def test_no_claude_ai_login_is_auth_required(self) -> None:
        for body in ({"rate_limits_available": False, "rate_limits": ENDPOINT}, {"session": {}},
                     {"rate_limits": None}):
            with self.subTest(body=body), self.assertRaises(c.RuntimeFailure) as failed:
                usage_read.from_cli(body, account=None)
            self.assertEqual(failed.exception.code, "auth_required")

    def test_xo_never_reads_the_token_nor_calls_anthropic(self) -> None:
        source = Path(usage_read.__file__).read_text(encoding="utf-8")
        for needle in ("httpx", "api.anthropic.com", "accessToken", ".credentials.json", "Authorization"):
            self.assertNotIn(needle, source)


class UsageThroughClaudeTests(unittest.IsolatedAsyncioTestCase):
    """The read asks claude itself (``get_usage`` on a prompt-less process):
    claude makes the request with its own login; XO holds no token."""

    def runtime(self, steps: list) -> tuple[ClaudeRuntime, object]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        fake = install("claude", root / "bin", scenario(steps))
        (root / ".claude").mkdir()
        # An unreadable credentials file: the read must not need it.
        (root / ".claude" / ".credentials.json").write_text("not json")
        environ = {k: v for k, v in os.environ.items() if k not in TOKEN_VARS}
        patcher = mock.patch.dict(os.environ, {**environ, "CLAUDE_CLI_PATH": str(fake.path), "HOME": str(root)},
                                  clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        timeout = mock.patch.object(probe, "INITIALIZE_PROBE_TIMEOUT_S", 30.0)
        timeout.start()
        self.addCleanup(timeout.stop)
        return ClaudeRuntime(claude_home=root / ".claude"), fake

    @staticmethod
    def get_usage(reply: dict) -> list:
        return [expect({"type": "control_request", "request": {"subtype": "get_usage"}}, rid="request_id"),
                {"emit": {"type": "control_response", "response": {"request_id": "$rid", **reply}}}]

    async def test_the_read_is_initialize_then_get_usage_and_no_prompt(self) -> None:
        answer = {"rate_limits_available": True, "rate_limits": ENDPOINT, "subscription_type": "pro"}
        runtime, fake = self.runtime([*claude.initialize(),
                                      *self.get_usage({"subtype": "success", "response": answer})])
        snapshot = await runtime.read_usage()
        self.assertEqual([w.name for w in snapshot.windows], ["five_hour", "seven_day"])
        self.assertEqual(snapshot.plan, "pro")
        (run,) = fake.invocations()
        self.assertEqual([line["request"]["subtype"] for line in run["stdin"]], ["initialize", "get_usage"])
        self.assertIs(run["stdin"][1]["request"]["skip_behaviors"], True)
        self.assertNotIn("user", [line["type"] for line in run["stdin"]])       # no model call
        self.assertIn("--no-session-persistence", run["argv"])

    async def test_a_claude_without_get_usage_is_unsupported_and_other_errors_are_protocol(self) -> None:
        for error, code in (("Unsupported control request subtype: get_usage", "unsupported"),
                            ("Unknown request subtype get_usage", "unsupported"),
                            ("rate limits fetch failed", "protocol")):
            with self.subTest(error=error):
                runtime, _ = self.runtime([*claude.initialize(),
                                           *self.get_usage({"subtype": "error", "error": error})])
                with self.assertRaises(c.RuntimeFailure) as failed:
                    await runtime.read_usage()
                self.assertEqual(failed.exception.code, code)

    async def test_a_logged_out_claude_is_auth_required(self) -> None:
        runtime, _ = self.runtime([*claude.initialize(),
                                   *self.get_usage({"subtype": "success",
                                                    "response": {"rate_limits_available": False}})])
        with self.assertRaises(c.RuntimeFailure) as failed:
            await runtime.read_usage()
        self.assertEqual(failed.exception.code, "auth_required")

if __name__ == "__main__":
    unittest.main()
