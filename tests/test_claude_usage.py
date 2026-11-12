"""Claude's usage, as the space advisory needs it (storm plan W4, "Claude's
usage in Settings › Runtimes"): every window claude reports, its paid-usage
facts, a report with no number keeping the last, the read with no model
call, and each turn's own cost and context size.

The fixtures are recorded: one of each distinct ``rate_limit_event`` shape in
the turn logs of 11-11, and the usage endpoint's answer the same day.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import usage_read
from services.cowork_agent.adapters.claude_code.runtime.account import UsageGuard
from services.cowork_agent.adapters.claude_code.runtime.wire import Wire

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

    def test_a_used_up_or_locked_window_is_limited(self) -> None:
        body = {"five_hour": {"utilization": 100.0, "resets_at": "2026-11-12T01:40:00+00:00"},
                "seven_day": {"utilization": 40.0, "resets_at": "2026-11-17T18:00:00+00:00",
                              "locked_reason": "admin"}}
        snapshot = usage_read.snapshot(body, account="a", plan=None)
        self.assertEqual([w.status for w in snapshot.windows], ["limited", "limited"])
        self.assertEqual(snapshot.paid, c.PaidUsage())

    def test_the_token_is_the_logins_and_an_expired_one_is_never_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(c.RuntimeFailure) as missing:
                usage_read.access_token(home, time.time())
            self.assertEqual(missing.exception.code, "auth_required")
            login = {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "r", "subscriptionType": "max",
                                       "expiresAt": int((time.time() + 3600) * 1000)}}
            (home / ".credentials.json").write_text(json.dumps(login))
            self.assertEqual(usage_read.access_token(home, time.time()), ("tok", "max"))
            with self.assertRaises(c.RuntimeFailure) as expired:
                usage_read.access_token(home, time.time() + 3600)
            self.assertEqual(expired.exception.code, "auth_required")

    async def test_the_read_sends_the_token_and_maps_refusals(self) -> None:
        seen: list[httpx.Request] = []

        def answer(status: int, body: object):
            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(status, json=body)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async with answer(200, ENDPOINT) as client:
            body = await usage_read.fetch("tok", client=client)
        self.assertEqual(body["five_hour"]["utilization"], 51.0)
        request = seen[0]
        self.assertEqual((request.method, str(request.url)), ("GET", usage_read.USAGE_URL))
        self.assertEqual((request.headers["authorization"], request.headers["anthropic-beta"]),
                         ("Bearer tok", usage_read.OAUTH_BETA))
        for status, code in ((401, "auth_required"), (403, "auth_required"), (429, "rate_limited"),
                             (500, "protocol")):
            with self.subTest(status=status):
                async with answer(status, {}) as client:
                    with self.assertRaises(c.RuntimeFailure) as failed:
                        await usage_read.fetch("tok", client=client)
                self.assertEqual(failed.exception.code, code)


if __name__ == "__main__":
    unittest.main()
