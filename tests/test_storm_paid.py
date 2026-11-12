"""Paid usage under the founder's intent (plan W2b; w2-w3 design §3.4, §5):
one source of usage facts (the space advisory), the paid gate at
``PAID_THRESHOLD``, the threshold watcher, the founder's paid mode, and the
paid question's Continue and Wait, each answered for its window only.

The incident replays use recorded payloads: Codex's own reports of the
11-09/10 paid switch (``tests/fixtures/advisory/codex_paid_incident.json``)
and Claude's recorded events (``claude_rate_limit_events.json``)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.browser_guard import add_browser_write_guard
from routers.cowork_agent.storm import intent as intent_routes
from services.advisory import Fact, Paid
from services.advisory.advisory import Advisory
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.adapters.codex.runtime import account as codex_account
from services.cowork_agent.storm.control import founder
from services.cowork_agent.storm.engine import admission
from services.cowork_agent.storm.engine.engine import pool_id
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import intents
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit, message
from tests.storm_review_kit import ReviewTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "advisory"
#: The fake claude names no account: its pool, and its account as the advisory files it.
POOL, ACCOUNT = pool_id(AGENT, None), (AGENT, None)
HOUR = 3600.0


def on_minute(seconds: float) -> float:
    """A reset on a whole minute, as the advisory publishes resets."""
    return float(int(seconds // 60) * 60)

#: A turn that runs until it is interrupted.
LONG_TURN = [*claude.initialize(), claude.user_message(), claude.init(SID),
             claude.tool_call(SID, "t1", "Bash", {"command": "sleep 30"}), *claude.host_control("interrupt"),
             claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
             claude.idle(SID)]


def recorded_codex() -> list[dict[str, Any]]:
    """The recorded steps, in the shape codex's app-server sends them
    (``account/rateLimits/updated``): its rollouts log the same snapshot in
    snake_case."""
    def camel(rl: dict[str, Any]) -> dict[str, Any]:
        def window(w):
            return w and {"usedPercent": w["used_percent"], "windowDurationMins": w["window_minutes"],
                          "resetsAt": w["resets_at"]}
        return {"limitId": rl["limit_id"], "primary": window(rl["primary"]), "secondary": window(rl["secondary"]),
                "credits": {"hasCredits": rl["credits"]["has_credits"], "unlimited": rl["credits"]["unlimited"],
                            "balance": rl["credits"]["balance"]},
                "planType": rl["plan_type"], "rateLimitReachedType": rl["rate_limit_reached_type"]}
    events = json.loads((FIXTURES / "codex_paid_incident.json").read_text())["events"]
    return [camel(e["rate_limits"]) for e in events]


def codex_reports(snapshot: dict[str, Any], *, resets_at: float | None = None) -> list[c.RateLimit]:
    """What codex's runtime reports for one snapshot (one ``RateLimit`` per
    window, with the account's credits), its reset moved to ``resets_at``."""
    if resets_at is not None:
        snapshot = {**snapshot, "primary": {**snapshot["primary"], "resetsAt": int(resets_at)}}
    credits = codex_account.paid(snapshot)
    return [c.RateLimit(session="cse_x", ts="t", seq=1, status=w.status, window=w.name, resets_at=w.resets_at,
                        utilization=w.utilization, overage=w.overage, paid=credits)
            for w in codex_account.windows(snapshot)]


def report(used: float | None, *, resets_at: float, possible: bool | None = True, window: str = "weekly",
           status: str = "ok", in_use: bool | None = None) -> c.RateLimit:
    return c.RateLimit(session="cse_x", ts="t", seq=1, status=status, window=window, resets_at=iso(resets_at),
                       utilization=used, paid=c.PaidUsage(possible=possible, in_use=in_use))


def seen(used: float | None, resets_at: str | None = "2026-11-16T06:52:00Z", status: str | None = None,
         ends: float | None = 1_794_811_920.0) -> admission.Seen:
    return admission.Seen(resets_at, used, status, "reported", "2026-11-12T08:00:00Z", ends)


def paid(possible: bool | None, in_use: bool | None = None) -> Paid:
    """An account's paid facts, as the advisory serves them (None: unknown)."""
    known = {k: Fact(v, "reported", "2026-11-12T08:00:00Z") for k, v in (("possible", possible), ("in_use", in_use))
             if v is not None}
    return Paid(**known)


class GateTests(unittest.TestCase):
    """``admission.standing``: the paid gate on its own."""

    NOW = 1_794_470_400.0

    def standing(self, windows: dict, account_paid, *, mode: str = "never", answers=None, waits: bool = True,
                 **kw) -> admission.Pool:
        return admission.standing("agent_a:acct-1", windows, account_paid, mode=mode, answers=answers or {},
                                  waits=waits, now_s=self.NOW, **kw)

    def test_an_account_that_can_go_paid_is_held_at_the_threshold_on_its_share_used(self) -> None:
        held = self.standing({"weekly": seen(admission.PAID_THRESHOLD)}, paid(True))
        self.assertEqual((held.state, held.reason, held.resets_at, held.halts),
                         ("paid", admission.PAID_THRESHOLD_REACHED, "2026-11-16T06:52:00Z", True))
        below = self.standing({"weekly": seen(admission.PAID_THRESHOLD - 0.01)}, paid(True))
        self.assertEqual((below.state, below.halts), ("warn", False))          # later work waits, no more
        # The share used decides, never a status: a vendor may give none, or "ok" at 99 %.
        self.assertEqual(self.standing({"weekly": seen(0.99, status="ok")}, paid(True)).state, "paid")
        # Paid usage in use holds whatever the facts say of the windows.
        self.assertEqual(self.standing({}, paid(False, in_use=True)).reason, admission.PAID_IN_USE)

    def test_an_account_that_cant_go_paid_never_meets_the_gate_only_its_limit(self) -> None:
        self.assertEqual(self.standing({"five_hour": seen(0.99, status="warning")}, paid(False)).state, "warn")
        limit = self.standing({"five_hour": seen(1.0, status="limited")}, paid(False))
        self.assertEqual((limit.state, limit.reason, limit.halts), ("closed", admission.LIMIT, False))

    def test_unknown_facts_hold_new_turns_only_while_a_fresh_read_can_settle_them(self) -> None:
        unknown = self.standing({}, paid(True))                 # it can go paid; nothing known of its usage
        self.assertEqual((unknown.state, unknown.reason, unknown.unknown, unknown.halts),
                         ("paid", admission.PAID_UNKNOWN, True, False))
        at_top = self.standing({"weekly": seen(0.97)}, paid(None))      # at the threshold; can it go paid?
        self.assertEqual((at_top.reason, at_top.unknown), (admission.PAID_UNKNOWN, True))
        waited = self.standing({"weekly": seen(0.97)}, paid(None), waits=False)
        self.assertEqual((waited.state, waited.unknown), ("warn", True))     # its one read came: no hold
        self.assertEqual(self.standing({"weekly": seen(0.3)}, paid(None)).state, "open")  # nothing to settle
        nothing = self.standing({}, paid(None))                  # nothing seen of it yet
        self.assertEqual((nothing.reason, nothing.halts), (admission.PAID_UNKNOWN, False))
        self.assertEqual(self.standing({}, paid(None), waits=False).state, "open")

    def test_under_ask_the_window_is_asked_once_and_continue_lifts_that_window_only(self) -> None:
        subject = "paid:agent_a:acct-1:2026-11-16T06:52:00Z"
        windows = {"weekly": seen(0.96)}
        asking = self.standing(windows, paid(True), mode="ask")
        self.assertEqual((asking.reason, asking.subject, asking.halts), (admission.PAID_ASKED, subject, True))
        asked = self.standing(windows, paid(True), mode="ask", answers={subject: "open"})
        self.assertEqual((asked.reason, asked.subject), (admission.PAID_ASKED, None))      # never asked twice
        self.assertEqual(self.standing(windows, paid(True), mode="ask", answers={subject: "continue"}).state, "warn")
        self.assertEqual(self.standing(windows, paid(True), mode="ask", answers={subject: "wait"}).reason,
                         admission.PAID_THRESHOLD_REACHED)
        nxt = {"weekly": seen(0.96, "2026-11-23T06:52:00Z", ends=1_795_416_720.0)}
        self.assertEqual(self.standing(nxt, paid(True), mode="ask", answers={subject: "continue"}).subject,
                         "paid:agent_a:acct-1:2026-11-23T06:52:00Z")                      # the next window asks
        self.assertEqual(self.standing(windows, paid(True), mode="never").subject, None)  # never asks nothing
        # Continue lifts the paid gate for its window, never a vendor's limit.
        limited = {"weekly": seen(0.96), "five_hour": seen(1.0, status="limited")}
        self.assertEqual(self.standing(limited, paid(True), mode="ask", answers={subject: "continue"}).state,
                         "closed")

    def test_the_last_value_of_a_window_stands_once_its_fact_ages_out_until_its_reset(self) -> None:
        clock = [self.NOW]
        advisory = Advisory(clock=lambda: clock[0])
        reset = self.NOW + 5 * HOUR
        advisory.record_usage(*ACCOUNT, report(0.97, resets_at=reset))
        facts = advisory.snapshot().account(*ACCOUNT)
        windows = admission.windows_now(facts, {}, self.NOW)
        self.assertEqual((windows["weekly"].utilization, windows["weekly"].ends), (0.97, reset))
        clock[0] = self.NOW + 16 * 60                   # past the advisory's 15 minutes for a usage fact
        self.assertFalse(facts.windows["weekly"].utilization.at(clock[0]).known)      # Fact.at: unknown now
        self.assertIsNone(facts.windows["weekly"].utilization.at(clock[0]).value)
        later = admission.windows_now(facts, windows, clock[0])
        self.assertEqual(later["weekly"].utilization, 0.97)                       # a lower bound
        self.assertEqual(admission.windows_now(facts, windows, reset), {})       # gone at its reset


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()


class IncidentTests(EngineCase):
    async def test_the_paid_incident_replayed_under_never_ends_the_turn_and_starts_nothing(self) -> None:
        """11-09/10: Codex at 99 % with credits and two turns running went on
        onto paid credits for hours. Replayed with its recorded reports
        (93 % up to the switch at 100 % and the credits spent after it),
        under the default (no intent: never): the turn is interrupted at the
        first report at the threshold, 95 %, and nothing new starts."""
        kit = self.kit
        kit.scenario(LONG_TURN)
        engine = await kit.boot()
        running, waiting = await kit.session(), await kit.session()
        await engine.deliver(running, message(1))
        turn = await kit.recorder.seen("turn.started", session_id=running)
        reset = on_minute(kit.clock.time()) + 4 * 24 * HOUR
        steps = recorded_codex()
        for snapshot in steps[:2]:                                     # 93 %, 94 %: below the threshold
            for event in codex_reports(snapshot, resets_at=reset):
                engine.advisory.record_usage(*ACCOUNT, event)
        await kit.recorder.seen("pool", state="warn")
        self.assertEqual(kit.recorder.of("session.halted"), [])
        for event in codex_reports(steps[2], resets_at=reset):        # 95 %, with credits
            engine.advisory.record_usage(*ACCOUNT, event)
        halted = await kit.recorder.seen("session.halted", session_id=running)
        self.assertEqual((halted.data["reason"], halted.data["by"], halted.data["turn"], halted.data["interrupted"]),
                         ("paid_threshold", "engine", turn.turn_id, True))
        pool = kit.recorder.of("pool")[-1].data
        self.assertEqual((pool["state"], pool["reason"], pool["resets_at"]),
                         ("paid", admission.PAID_THRESHOLD_REACHED, engine.pool(*ACCOUNT).resets_at))
        # Nothing new starts: the next message waits, saying why; the halted turn's note waits too.
        await engine.deliver(waiting, message(2))
        await kit.until(lambda: engine._queued.get(waiting, "").startswith("pool paid until"))
        for snapshot in steps[3:]:                                     # on to 100 %, credits being spent
            for event in codex_reports(snapshot, resets_at=reset):
                engine.advisory.record_usage(*ACCOUNT, event)
        await asyncio.sleep(0.2)
        self.assertEqual(len(kit.recorder.of("turn.started")), 1)
        self.assertEqual(kit.rows("SELECT id, state FROM wakes WHERE session_id = ? AND id LIKE 'paid:%'",
                                  (running,)), [(f"paid:{turn.turn_id}", "queued")])
        self.assertEqual(kit.rows("SELECT state FROM pools"), [("paid",)])
        # At the reset the account is a new window: what waited runs.
        kit.clock.advance(4 * 24 * HOUR + 1)
        await kit.recorder.seen("turn.started", session_id=waiting)

    async def test_claude_that_cant_go_paid_is_held_only_by_its_own_limit(self) -> None:
        """Claude's account reports ``overageStatus: rejected``
        (``org_level_disabled``): at 99 % the paid gate never holds it; at
        100 % its own limit closes it until the reset."""
        recorded = json.loads((FIXTURES / "claude_rate_limit_events.json").read_text())["events"]
        warning, rejected = recorded[3], recorded[4]                   # the 5-hour window at 0.9, then at 1
        kit = self.kit
        resets = int(on_minute(kit.clock.time()) + 2 * HOUR)

        def at(event: dict, used: float) -> dict:
            info = json.loads(json.dumps(event["rate_limit_info"]).replace("1793221200", str(resets)))
            info["unifiedWindows"]["five_hour"]["utilization"] = used
            info["utilization"] = used
            return {"emit": {"type": "rate_limit_event", "rate_limit_info": info}}
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID), at(warning, 0.99),
                      *claude.text_turn(SID, "ok"), claude.idle(SID), claude.user_message(), at(rejected, 1.0),
                      *claude.text_turn(SID, "ok", message_id="m2"), claude.idle(SID)])
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await kit.until(lambda: engine.pool(*ACCOUNT).state == "warn")
        self.assertIs(engine.advisory.snapshot().account(*ACCOUNT).paid.possible.value, False)
        self.assertEqual(kit.recorder.of("session.halted"), [])
        await engine.deliver(sid, message(2))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)      # not held at 99 %
        closed = await kit.recorder.seen("pool", state="closed")
        self.assertEqual((closed.data["reason"], closed.data["resets_at"]), (admission.LIMIT, iso(resets)))
        self.assertEqual(kit.recorder.of("session.halted"), [])           # a limit holds new turns, no more


def login(kit: Kit) -> None:
    """Log claude in to an account of its own, as its login writes it: an
    account the advisory can file reads under."""
    (kit.home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": "rt-a", "accessToken": "at", "expiresAt": 4102444800000}}))
    (kit.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-a"}}))


class NoUsageRead(ClaudeRuntime):
    """Claude's runtime as one that offers no model-free read."""

    def features(self) -> c.FeatureList:
        found = super().features()
        return dataclasses.replace(found, features={k: v for k, v in found.features.items() if k != "usage_read"})


TWO_TURNS = [*claude.initialize(), claude.user_message(), claude.init(SID), *claude.text_turn(SID, "ok"),
             claude.idle(SID), claude.user_message(), *claude.text_turn(SID, "ok", message_id="m2"), claude.idle(SID)]


class UnknownCase(EngineCase):
    """An engine whose claude has an account of its own, the advisory's reads
    of it answered by the test (``reads`` holds each one asked for)."""

    async def boot(self, runtime=None):
        login(self.kit)
        self.kit.scenario(TWO_TURNS)
        engine = await (self.kit.boot_with(runtime) if runtime is not None else self.kit.boot())
        self.key = (AGENT, await engine._read_account(AGENT))
        self.assertIsNotNone(self.key[1])
        self.reads: list[str] = []
        engine.advisory.request_read = self.reads.append
        return engine

    def read(self, engine, *windows: c.UsageWindow, paid: c.PaidUsage | None = None) -> None:
        """The account's model-free read lands."""
        engine.advisory._book.record_read(self.key, c.UsageSnapshot(account=self.key[1], source="measured",
                                                                    windows=windows, paid=paid),
                                          self.kit.clock.time())
        engine.advisory._publish()

    def weekly(self, used: float | None) -> c.UsageWindow:
        return c.UsageWindow(name="weekly", status=None, utilization=used,
                             resets_at=iso(on_minute(self.kit.clock.time()) + HOUR))


class UnknownTests(UnknownCase):
    def unknown(self, engine) -> None:
        """It can go paid, and its share used isn't known (a report with no number)."""
        engine.advisory.record_usage(*self.key, report(None, resets_at=on_minute(self.kit.clock.time()) + HOUR))

    async def test_an_unknown_fact_is_read_and_holds_new_turns_one_read_at_most(self) -> None:
        kit = self.kit
        engine = await self.boot()
        self.unknown(engine)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: engine._queued.get(sid) == "pool paid: waiting for a fresh usage read")
        self.assertEqual(self.reads, [AGENT])                           # one read asked for
        self.assertEqual(kit.runs(), [])
        # The read never comes: after one read's time it holds nothing.
        kit.clock.advance(admission.UNKNOWN_WAIT_S)
        await kit.recorder.seen("turn.finished", session_id=sid)
        # Still unknown: never held again, nor read again, for it.
        self.unknown(engine)
        kit.pace()
        await engine.deliver(sid, message(2))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(self.reads, [AGENT])
        self.assertTrue(engine.pool(*self.key).unknown)

    async def test_a_read_that_lands_ends_the_wait_whatever_it_says(self) -> None:
        kit = self.kit
        engine = await self.boot()
        self.unknown(engine)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: "fresh usage read" in engine._queued.get(sid, ""))
        self.read(engine, self.weekly(None))         # it lands and can't say either: waiting settles nothing more
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue(engine.pool(*self.key).unknown)

    async def test_a_runtime_that_cant_read_is_never_held_on_an_unknown(self) -> None:
        engine = await self.boot(NoUsageRead(claude_home=self.kit.home / ".claude"))
        for facts in ((), (self.unknown,)):          # nothing seen yet; then a share used not known
            for push in facts:
                push(engine)
            pool = engine.pool(*self.key)
            self.assertEqual((pool.state, pool.unknown), ("open", True))
        self.assertEqual(self.reads, [])


class BootTests(UnknownCase):
    """After a restart, or for an account storm hasn't seen, nothing is known
    of it: an account that can read starts no turn before its first read."""

    async def test_an_account_at_99_percent_with_credits_starts_nothing_before_or_after_its_first_read(self) -> None:
        kit = self.kit
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: engine._queued.get(sid) == "pool paid: waiting for a fresh usage read")
        self.assertEqual((self.reads, kit.runs()), ([AGENT], []))
        self.read(engine, self.weekly(0.99), paid=c.PaidUsage(possible=True, credits=7211.76))
        await kit.until(lambda: engine._queued.get(sid, "").startswith("pool paid until"))
        self.assertEqual(engine.pool(*self.key).reason, admission.PAID_THRESHOLD_REACHED)
        self.assertEqual(kit.runs(), [])

    async def test_an_account_whose_first_read_says_it_cant_go_paid_starts_at_once(self) -> None:
        kit = self.kit
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: "fresh usage read" in engine._queued.get(sid, ""))
        self.read(engine, self.weekly(0.99), paid=c.PaidUsage(possible=False, disabled_reason="org_level_disabled"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(engine.pool(*self.key).state, "warn")       # near its limit; its own limit holds it at 100 %


def app() -> FastAPI:
    application = FastAPI()
    application.include_router(intent_routes.router)
    add_browser_write_guard(application, [])
    return application


class AskTests(ReviewTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.routes = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.routes.aclose()
        await super().asyncTearDown()

    async def ask_on_the_space(self, engine) -> None:
        await founder.set_paid(engine, "space", "", "ask")

    def cross(self, engine, used: float, resets_at: float) -> None:
        engine.advisory.record_usage(*ACCOUNT, report(used, resets_at=resets_at))

    async def usage_items(self) -> list[dict]:
        return (await self.client.get("/api/chat/review", params={"kind": "usage"})).json()["items"]

    async def test_wait_isnt_asked_again_in_its_window_and_the_next_window_asks_once(self) -> None:
        """Wait is the answer for that window, never an intent: the
        founder's ``ask`` on this one pool stands, and the next window asks."""
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await self.boot()
        asked = await founder.set_paid(engine, "pool", POOL, "ask")
        reset = on_minute(kit.clock.time()) + HOUR
        self.cross(engine, 0.96, reset)
        item = await self.item("usage")
        self.assertEqual((item["subject"], item["detail"]["pool"], [o["id"] for o in item["options"]]),
                         (f"paid:{POOL}:{iso(reset)}", POOL, ["continue", "wait"]))
        decided = (await self.decide(item["id"], decision="wait")).json()
        self.assertEqual((decided["state"], decided["decision"]["decision"]), ("decided", "wait"))
        self.assertEqual(engine._intents, {("pool", POOL, "paid"): asked.intent})     # the founder's ask stands
        self.assertEqual(kit.rows("SELECT count(*) FROM ledger"), [(1,)])
        for used in (0.97, 0.99):                                      # the account goes on reporting
            self.cross(engine, used, reset)
        await asyncio.sleep(0.2)
        self.assertEqual(len(await self.usage_items()), 1)
        self.assertEqual(engine.pool(*ACCOUNT).reason, admission.PAID_THRESHOLD_REACHED)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: engine._queued.get(sid, "").startswith("pool paid until"))

        kit.clock.advance(HOUR + 1)                                    # the next window
        nxt = on_minute(kit.clock.time()) + HOUR
        for used in (0.96, 0.98):
            self.cross(engine, used, nxt)
        fresh = await self.item("usage", count=2)
        await asyncio.sleep(0.2)
        self.assertEqual([i["subject"] for i in await self.usage_items()],
                         [f"paid:{POOL}:{iso(reset)}", f"paid:{POOL}:{iso(nxt)}"])
        self.assertEqual(fresh["state"], "open")

    async def test_continue_admits_that_window_only(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await self.boot()
        await self.ask_on_the_space(engine)
        reset = on_minute(kit.clock.time()) + HOUR
        self.cross(engine, 0.96, reset)
        item = await self.item("usage")
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: "the founder's answer on paid usage" in engine._queued.get(sid, ""))
        decided = (await self.decide(item["id"], decision="continue")).json()
        self.assertEqual((decided["state"], decided["decision"]["decision"]), ("decided", "continue"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertNotIn(("pool", POOL, "paid"), engine._intents)      # Continue sets no intent
        self.assertEqual(engine.pool(*ACCOUNT).state, "warn")
        # The next window crosses: asked again, and held until answered.
        kit.clock.advance(HOUR + 1)
        self.cross(engine, 0.96, on_minute(kit.clock.time()) + HOUR)
        again = await self.item("usage", count=2)
        self.assertEqual(again["state"], "open")
        self.assertEqual(engine.pool(*ACCOUNT).reason, admission.PAID_ASKED)

    async def test_setting_never_past_the_threshold_halts_the_accounts_live_sessions(self) -> None:
        kit = self.kit
        kit.scenario(LONG_TURN)
        engine = await self.boot()
        await self.ask_on_the_space(engine)
        self.cross(engine, 0.96, on_minute(kit.clock.time()) + HOUR)
        item = await self.item("usage")
        await self.decide(item["id"], decision="continue")
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        turn = await kit.recorder.seen("turn.started", session_id=sid)

        refused = await self.routes.post("/api/chat/intent/paid", json={"scope": "pool", "scope_id": POOL,
                                                                        "value": "never"},
                                         headers={"Origin": "https://evil.example"})
        self.assertEqual(refused.status_code, 403)
        bad = await self.routes.post("/api/chat/intent/paid", json={"scope": "chat", "scope_id": "x",
                                                                    "value": "never"})
        self.assertEqual(bad.status_code, 422)
        answer = await self.routes.post("/api/chat/intent/paid", json={"scope": "pool", "scope_id": POOL,
                                                                       "value": "never", "cites": "cmsg_1"})
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertEqual((body["intent"]["value"], body["intent"]["cites"]), ("never", "cmsg_1"))
        self.assertEqual([(h["session_id"], h["turn_id"], h["interrupted"]) for h in body["halted"]],
                         [(sid, turn.turn_id, True)])
        self.assertEqual(kit.recorder.of("session.halted", sid)[0].data["reason"], "paid_threshold")
        listed = (await self.routes.get("/api/chat/intent")).json()["intents"]
        self.assertIn(("pool", POOL, "paid", "never"), [(i["scope"], i["scope_id"], i["key"], i["value"])
                                                        for i in listed])


class PoolTests(EngineCase):
    async def test_one_window_never_undoes_another_and_the_pool_stands_on_the_advisorys_facts(self) -> None:
        """2026-10-27/28: a 5-hour "allowed" overwrote a 7-day limit and the
        pool flipped. The advisory keeps each window; the pool reads them."""
        engine = await self.kit.boot()
        now = on_minute(self.kit.clock.time())
        weekly, five_hour = now + 7 * 24 * HOUR, now + HOUR
        engine.advisory.record_usage(*ACCOUNT, report(None, resets_at=weekly, window="seven_day", status="limited",
                                                      possible=False))
        engine.advisory.record_usage(*ACCOUNT, report(0.2, resets_at=five_hour, window="five_hour", possible=False))
        closed = await self.kit.recorder.seen("pool", state="closed")
        self.assertEqual((closed.data["resets_at"], closed.data["reason"]), (iso(weekly), admission.LIMIT))
        self.assertEqual(self.kit.rows("SELECT state, resets_at FROM pools WHERE id = ?", (POOL,)),
                         [("closed", iso(weekly))])
        status = await engine.status()
        (shown,) = status["pools"]
        self.assertEqual((shown["id"], shown["state"], sorted(shown["windows"])),
                         (POOL, "closed", ["five_hour", "seven_day"]))

    async def test_steering_never_joins_a_turn_whose_pool_is_held(self) -> None:
        """The incident's bug C: coordinators steered paid turns five times."""
        kit = self.kit
        kit.scenario(LONG_TURN)
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.started", session_id=sid)
        live = engine._live[sid]
        engine._pools[live.pool] = admission.Pool(live.pool, "paid", reason=admission.PAID_ASKED)
        entry = {"now": False, "add": True}
        with mock.patch.object(engine, "_admit") as admit:
            engine._admit_live(live, entry, None, engine._pools[live.pool])
        admit.assert_not_called()
        await engine.halt(sid, reason="test", by="test")


class SetPaidTests(EngineCase):
    async def test_set_paid_refuses_what_the_store_wouldnt_hold_and_records_the_founders_act(self) -> None:
        engine = await self.kit.boot()
        with self.assertRaises(intents.IntentInvalid):
            await founder.set_paid(engine, "chat", "chat_1", "never")
        with self.assertRaises(intents.IntentInvalid):
            await founder.set_paid(engine, "space", "", "always")
        done = await founder.set_paid(engine, "space", "", "ask", until="2999-01-01T00:00:00Z",
                                      cites=f"cmsg_{uuid.uuid4().hex[:6]}")
        self.assertEqual((done.intent.value, done.intent.until, done.halted), ("ask", "2999-01-01T00:00:00Z", ()))
        self.assertEqual(intents.paid_mode(POOL, engine._intents, self.kit.clock.time()).value, "ask")
        (entry,) = self.kit.rows("SELECT actor, action, record FROM ledger")
        self.assertEqual(entry, ("founder", "set", "intent:space::paid"))


if __name__ == "__main__":
    unittest.main()
