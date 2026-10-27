"""The review queue's own rules (design ch 15 §3, §5, §9; ch 07 §5.1;
ch 17 §2.5): expiry, the queue's order and paging, the person's rules and
XO's default rules over the routes, paid usage, the loop breaker's counts,
and the routes' refusals (503, 404, 422, the browser write guard)."""

from __future__ import annotations

import asyncio
import unittest

from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.review import loops
from services.cowork_agent.storm.review.engine_link import iso
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.tiers import Tier
from services.cowork_agent.storm.store import EngineLock
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, message
from tests.storm_review_kit import PUSH, ReviewTestCase

DAY = 24 * 3600


class ExpiryTests(ReviewTestCase):
    async def test_asks_expire_after_seven_days_a_held_call_never_and_a_parked_one_seven_days_after(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "noted"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()

        def opened(spec: ItemSpec):
            return engine.write(lambda w: self.queue.open_item(w, spec))
        asks = [await opened(ItemSpec(kind=kind, title=f"a {kind}", origin="agent", session_id=sid))
                for kind in ("question", "approval", "action")]
        held = await opened(ItemSpec(kind="permission", title="Run `x`", origin="cli", session_id=sid,
                                     native={"request_id": "r1"}, risk=Tier.NOTABLE, blocking=True))
        later = await opened(ItemSpec(kind="permission", title="Run `y`", origin="cli", session_id=sid,
                                      native={"request_id": "r2"}, risk=Tier.NOTABLE, blocking=True))
        self.assertEqual([(await self.queue.get_item(i))["expires_at"] is None for i in [*asks, held, later]],
                         [False, False, False, True, True])

        kit.clock.advance(3 * DAY)
        await engine.write(lambda w: self.queue.park(w, later, "memory"))
        kit.clock.advance(4 * DAY + 1)
        for n in range(1, 4):
            await kit.recorder.seen("review.expired", count=n)
        self.assertEqual(sorted(e.data["id"] for e in kit.recorder.of("review.expired")), sorted(asks))
        self.assertEqual(kit.rows("SELECT id, priority, reason FROM wakes WHERE id LIKE 'review-expired:%' "
                                  "ORDER BY id"),
                         sorted((f"review-expired:{i}", "later", "review-expired") for i in asks))
        self.assertRefused(await self.decide(asks[1], decision="approve"), 410, "expired")
        self.assertEqual((await self.queue.get_item(later))["state"], "open")

        kit.clock.advance(3 * DAY)
        await kit.recorder.seen("review.expired", count=4)
        self.assertEqual((await self.queue.get_item(later))["state"], "expired")
        self.assertEqual((await self.queue.get_item(held))["state"], "open")


class OrderTests(ReviewTestCase):
    async def test_the_open_queue_puts_destructive_then_blocking_first_then_age_and_pages(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        specs = [ItemSpec(kind="approval", title="old approval", origin="agent", session_id=sid),
                 ItemSpec(kind="question", title="blocking question", origin="agent", session_id=sid, blocking=True),
                 ItemSpec(kind="permission", title="routine ask", origin="cli", session_id=sid, risk=Tier.ROUTINE,
                          blocking=True, native={"request_id": "a"}),
                 ItemSpec(kind="permission", title="destructive ask", origin="cli", session_id=sid,
                          risk=Tier.DESTRUCTIVE, blocking=True, native={"request_id": "b"}),
                 ItemSpec(kind="approval", title="new approval", origin="agent", session_id=sid)]
        for spec in specs:
            await engine.write(lambda w, spec=spec: self.queue.open_item(w, spec))

        listed = (await self.client.get("/api/chat/review", params={"state": "open"})).json()["items"]
        self.assertEqual([i["title"] for i in listed], ["destructive ask", "blocking question", "routine ask",
                                                        "old approval", "new approval"])
        pages, after = [], 0
        while True:
            page = (await self.client.get("/api/chat/review", params={"state": "open", "limit": 2,
                                                                      "after_seq": after})).json()
            pages.append([i["title"] for i in page["items"]])
            if page["next_after_seq"] is None:
                break
            after = page["next_after_seq"]
        self.assertEqual(sum(pages, []), [i["title"] for i in listed])
        everything = (await self.client.get("/api/chat/review")).json()["items"]
        self.assertEqual([i["title"] for i in everything], [s.title for s in specs])     # by seq
        approvals = (await self.client.get("/api/chat/review", params={"kind": "approval"})).json()["items"]
        self.assertEqual(len(approvals), 2)
        self.assertRefused(await self.client.get("/api/chat/review", params={"state": "open", "after_seq": 999}),
                           400, "bad_cursor")


class RuleRouteTests(ReviewTestCase):
    async def test_rules_are_made_with_the_servers_sentence_listed_and_revoked(self) -> None:
        await self.boot()
        listed = (await self.client.get("/api/chat/rules")).json()
        self.assertEqual(listed["rules"], [])
        self.assertEqual({d["id"] for d in listed["defaults"]},
                         {"default:read-inside", "default:git-read", "default:git-local", "default:comms",
                          "default:known-hosts", "default:xo-routes"})
        self.assertTrue(all(d["revoked_at"] is None for d in listed["defaults"]))

        body = {"scope": "project", "project": "pid-demo", "kind": "command", "match": {"prefix": ["npm", "test"]},
                "effect": "allow", "sentence": "npm test is fine", "client_id": "c1"}
        wrong = self.assertRefused(await self.client.post("/api/chat/rules", json=body), 400,
                                   "rule_sentence_mismatch")
        self.assertEqual(wrong["sentence"], "In pid-demo, run `npm test` without asking")
        made = (await self.client.post("/api/chat/rules", json={**body, "sentence": wrong["sentence"]})).json()
        self.assertEqual((made["scope"], made["tier"], made["project_pid"]), ("project", "notable", "pid-demo"))
        retried = (await self.client.post("/api/chat/rules", json={**body, "sentence": wrong["sentence"]})).json()
        self.assertEqual(retried["id"], made["id"])
        self.assertEqual(self.kit.rows("SELECT count(*) FROM rules"), [(1,)])
        self.assertEqual(len(self.kit.recorder.of("rule.created")), 1)

        self.assertEqual((await self.client.post("/api/chat/rules", json={
            **body, "match": {"prefix": ["npm"], "host": "x.org"}, "client_id": "c2"})).status_code, 422)
        self.assertRefused(await self.client.post("/api/chat/rules", json={**body, "project": None,
                                                                           "client_id": "c3"}), 400, "bad_rule")
        self.assertRefused(await self.client.post("/api/chat/rules", json={
            **body, "kind": "web_fetch", "client_id": "c4"}), 400, "bad_rule")
        workspace = {"scope": "workspace", "kind": "command", "match": {"command": ["git", "push"]}, "effect": "deny",
                     "sentence": "In every project, never run exactly `git push`", "client_id": "c5"}
        self.assertEqual((await self.client.post("/api/chat/rules", json=workspace)).json()["tier"], "routine")

        revoked = (await self.client.delete(f"/api/chat/rules/{made['id']}")).json()
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertEqual((await self.client.delete(f"/api/chat/rules/{made['id']}")).json(), revoked)
        self.assertEqual([r["effect"] for r in (await self.client.get("/api/chat/rules")).json()["rules"]], ["deny"])
        self.assertRefused(await self.client.delete("/api/chat/rules/rule_nope"), 404, "rule_not_found")
        default = (await self.client.delete("/api/chat/rules/default:comms")).json()
        self.assertIsNotNone(default["revoked_at"])
        self.assertEqual([e.data for e in self.kit.recorder.of("rule.revoked")],
                         [{"rule": made["id"], "default": False}, {"rule": "default:comms", "default": True}])


class UsageTests(ReviewTestCase):
    def limited(self, *rate: dict) -> list[dict]:
        return self.started(*rate, *claude.text_turn(SID, "ok"), claude.idle(SID))

    async def paid(self, resets_at: float | None) -> tuple:
        """A first session's turn puts the pool on paid usage; a second
        session's message waits for the person's choice."""
        kit = self.kit
        kit.scenario(self.limited(claude.rate_limit("allowed", overage=True, resets_at=resets_at)))
        engine = await self.boot()
        first, second = await kit.session(), await kit.session()
        await engine.deliver(first, message(1))
        item = await self.item("usage")
        await kit.recorder.seen("turn.finished", session_id=first)
        kit.pace()
        await engine.deliver(second, message(2))
        await kit.recorder.seen("session", session_id=second, status="queued")
        return engine, item, second

    async def test_continue_runs_until_the_reset_the_store_knows(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(int(kit.clock.time()) + 3600)
        self.assertEqual((item["detail"]["pool"].split(":")[0], item["blocking"], [o["id"] for o in item["options"]]),
                         (AGENT, True, ["continue", "wait"]))
        decided = (await self.decide(item["id"], decision="continue")).json()
        self.assertEqual((decided["state"], decided["delivery"]), ("decided", None))
        await kit.recorder.seen("turn.finished", session_id=second)
        (pool,) = kit.rows("SELECT state, paid_continue_until FROM pools")
        self.assertEqual(pool, ("warn", item["detail"]["resets_at"]))     # 90 % used, no longer paid

    async def test_wait_keeps_new_turns_paused_until_the_reset(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(int(kit.clock.time()) + 60)
        self.assertEqual((await self.decide(item["id"], decision="wait")).json()["state"], "decided")
        await asyncio.sleep(0.2)
        self.assertEqual(kit.recorder.of("turn.finished", second), [])
        self.assertEqual(kit.rows("SELECT state FROM pools"), [("paid",)])
        kit.clock.advance(61)
        await kit.recorder.seen("turn.finished", session_id=second)

    async def test_with_no_known_reset_continue_lasts_to_the_next_report_and_wait_asks_again(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(None)
        self.assertEqual([o["id"] for o in item["options"]], ["continue", "wait"])     # both are honoured
        self.assertEqual((await self.decide(item["id"], decision="continue")).json()["state"], "decided")
        self.assertEqual(kit.rows("SELECT state, paid_continue_until FROM pools"), [("open", None)])
        # The next report on paid usage asks again.
        await kit.recorder.seen("turn.finished", session_id=second)
        again = await self.item("usage", count=2)
        self.assertEqual((await self.decide(again["id"], decision="wait")).json()["state"], "decided")
        self.assertEqual(kit.rows("SELECT state FROM pools"), [("paid",)])
        # Waiting with no reset known: the person's next message asks again.
        await engine.deliver(second, message(3, "and now?"))
        third = await self.item("usage", count=3)
        self.assertEqual(third["state"], "open")

    async def test_an_open_usage_item_past_its_reset_is_never_reused(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(int(kit.clock.time()) + 60)
        kit.clock.advance(61)                                               # the reset: the pool reopens
        await kit.recorder.seen("pool", state="open", was="paid")
        self.assertRefused(await self.decide(item["id"], decision="continue"), 410, "expired")   # never late
        later = int(kit.clock.time()) + 3600
        kit.scenario(self.limited(claude.rate_limit("allowed", overage=True, resets_at=later)))
        third = await kit.session()
        await engine.deliver(third, message(3))
        fresh = await self.item("usage", count=2)
        self.assertEqual((await self.queue.get_item(item["id"]))["state"], "expired")
        self.assertNotEqual(fresh["detail"]["resets_at"], item["detail"]["resets_at"])
        self.assertEqual((await self.decide(fresh["id"], decision="continue")).json()["state"], "decided")
        self.assertEqual(kit.rows("SELECT paid_continue_until FROM pools"), [(fresh["detail"]["resets_at"],)])


    async def test_continue_never_reopens_a_pool_closed_meanwhile(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(int(kit.clock.time()) + 3600)
        await engine.write(lambda w: w.execute("UPDATE pools SET state = 'closed'"))     # a hard limit since
        self.assertEqual((await self.decide(item["id"], decision="continue")).json()["state"], "decided")
        self.assertEqual(kit.rows("SELECT state, paid_continue_until FROM pools"), [("closed", None)])

    async def test_a_new_paid_report_replaces_an_item_past_its_reset(self) -> None:
        kit = self.kit
        engine, item, second = await self.paid(int(kit.clock.time()) + 60)
        kit.clock.advance(61)                                               # the reset: the pool reopens
        await kit.recorder.seen("pool", state="open", was="paid")
        later = int(kit.clock.time()) + 3600
        kit.scenario(self.limited(claude.rate_limit("allowed", overage=True, resets_at=later)))
        await engine.deliver(await kit.session(), message(3))
        await kit.recorder.seen("pool", state="paid", resets_at=iso(later))
        open_usage = (await self.client.get("/api/chat/review", params={"state": "open", "kind": "usage"})).json()
        self.assertEqual([(i["detail"]["resets_at"]) for i in open_usage["items"]], [iso(later)])
        self.assertEqual((await self.queue.get_item(item["id"]))["state"], "expired")


class LoopBreakerTests(ReviewTestCase):
    async def test_three_identical_denials_in_a_row_are_stuck_and_every_hundred_suggest_a_fresh_start(self) -> None:
        engine = await self.boot()

        def count(action: str):
            return engine.write(lambda w: loops.count(w, scope="session:s", action=action))
        outcomes = [await count(a) for a in ("a", "a", "b", "a", "a", "a", "a")]
        self.assertEqual([o.pause for o in outcomes], [False, False, False, False, False, True, False])
        self.assertEqual(self.kit.rows("SELECT streak, denials FROM loop_breaker"), [(1, 7)])
        for n in range(92):
            outcome = await count("c" if n % 2 else "d")
        self.assertEqual((outcome.denials, outcome.suggest), (99, False))
        hundredth = await count("e")
        self.assertEqual((hundredth.denials, hundredth.suggest, (await count("e")).suggest), (100, True, False))

    def stuck(self, *denied: dict) -> list[dict]:
        """Three asks for the same step, each denied; then the loop breaker's
        stop; then, once resumed, one more turn."""
        return self.started(*denied, *claude.host_control("interrupt"),
                            claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
                            claude.idle(SID), claude.user_message(), *claude.text_turn(SID, "back", message_id="m2"),
                            claude.idle(SID))

    async def assert_stuck_then_resumed(self, engine, sid: str) -> None:
        kit = self.kit
        stopped = await kit.recorder.seen("turn.stopped", session_id=sid)
        self.assertEqual(stopped.data["by"], "loop_breaker")
        paused = kit.recorder.of("session", sid)
        self.assertEqual([e.data.get("why") for e in paused if e.data.get("reason") == "paused"],
                         ["stuck on a denied step"])                       # the person is told once
        self.assertEqual((await engine.get_session(sid))["paused"], "stuck on a denied step")
        # Paused: an input waits, no turn starts, until the person resumes it.
        await engine.deliver(sid, message(2, "try something else"))
        kit.clock.advance(1)
        await asyncio.sleep(0.2)
        self.assertEqual(kit.rows("SELECT state FROM wakes WHERE id = 'msg:cmsg_2'"), [("queued",)])
        await engine.resume(sid)
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)

    async def test_three_identical_policy_denials_in_a_row_pause_the_session(self) -> None:
        kit = self.kit
        kit.scenario(self.stuck(*[step for n in range(3) for step in (
            claude.can_use_tool(f"p{n}", "Bash", PUSH), claude.permission_answer(f"p{n}", "deny"))]))
        engine = await self.boot()
        self.assertEqual((await self.client.post("/api/chat/rules", json={
            "scope": "workspace", "kind": "command", "match": {"prefix": ["git", "push"]}, "effect": "deny",
            "sentence": "In every project, never run `git push`", "client_id": "deny-push"})).status_code, 200)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.assert_stuck_then_resumed(engine, sid)
        self.assertEqual(kit.recorder.of("review.opened"), [])

    async def test_the_same_step_respaced_is_the_same_denied_action(self) -> None:
        kit = self.kit
        variants = ["git push origin main", "git push  origin main", "git  push origin main "]
        kit.scenario(self.stuck(*[step for n, command in enumerate(variants) for step in (
            claude.can_use_tool(f"p{n}", "Bash", {"command": command}), claude.permission_answer(f"p{n}", "deny"))]))
        engine = await self.boot()
        self.assertEqual((await self.client.post("/api/chat/rules", json={
            "scope": "workspace", "kind": "command", "match": {"prefix": ["git", "push"]}, "effect": "deny",
            "sentence": "In every project, never run `git push`", "client_id": "deny-push"})).status_code, 200)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.assert_stuck_then_resumed(engine, sid)

    async def test_three_identical_denials_by_the_person_pause_the_session(self) -> None:
        kit = self.kit
        kit.scenario(self.stuck(*[step for n in range(3) for step in (
            claude.can_use_tool(f"p{n}", "Bash", PUSH), claude.permission_answer(f"p{n}", "deny"))]))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        for n in range(1, 4):
            item = await self.item(count=n, session_id=sid)
            self.assertEqual((await self.decide(item["id"], decision="deny")).status_code, 200)
        await self.assert_stuck_then_resumed(engine, sid)

    async def test_confirming_the_suggestion_pauses_the_session(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        item = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="suggestion", title=loops.suggestion_title(False, loops.SUGGEST_EVERY), origin="xo", session_id=sid)))
        self.assertEqual((await self.decide(item, decision="approve")).json()["state"], "decided")
        self.assertEqual((await engine.get_session(sid))["paused"], "paused by the person after repeated denials")

    async def test_the_hundredth_denial_in_a_session_opens_one_suggestion_and_dismissing_changes_nothing(self) -> None:
        kit = self.kit
        commands = [{"command": f"git push origin b{n % 2}"} for n in range(loops.SUGGEST_EVERY + 1)]
        steps = []
        for n, command in enumerate(commands):
            steps += [claude.can_use_tool(f"p{n}", "Bash", command), claude.permission_answer(f"p{n}", "deny")]
        kit.scenario(self.started(*steps, *claude.text_turn(SID, "giving up"), claude.idle(SID)))
        engine = await self.boot()
        self.assertEqual((await self.client.post("/api/chat/rules", json={
            "scope": "workspace", "kind": "command", "match": {"prefix": ["git", "push"]}, "effect": "deny",
            "sentence": "In every project, never run `git push`", "client_id": "deny-push"})).status_code, 200)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        suggestion = await self.item("suggestion")
        self.assertEqual(suggestion["title"], "This session has had 100 denials. If it isn't working well, pause it.")
        self.assertEqual((suggestion["detail"], [o["id"] for o in suggestion["options"]]),
                         ({"scope": f"session:{sid}", "denials": 100}, ["approve", "deny"]))
        self.assertTrue(suggestion["options"][0]["hold_to_confirm"])
        await kit.until(lambda: kit.rows("SELECT denials FROM loop_breaker") == [(loops.SUGGEST_EVERY + 1,)])
        self.assertEqual(len(kit.recorder.of("review.opened")), 1)          # policy denials ask no one
        self.assertEqual((await self.decide(suggestion["id"], decision="deny")).json()["state"], "decided")
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)     # the input ended
        self.assertEqual((await engine.get_session(sid))["state"], "warm")


class RouteRefusalTests(ReviewTestCase):
    async def test_review_routes_answer_503_without_an_engine_or_its_queue(self) -> None:
        held = EngineLock.acquire()                 # another server runs the engine
        try:
            self.assertIsNone(await storm_engine.boot())
            for path in ("/api/chat/review", "/api/chat/review/rv_x", "/api/chat/rules"):
                with self.subTest(path=path):
                    self.assertRefused(await self.client.get(path), 503, "engine_elsewhere")
        finally:
            held.release()
        await self.kit.boot()                       # an engine with no review queue
        self.assertRefused(await self.client.get("/api/chat/review"), 503, "review_unavailable")

    async def test_unknown_items_bad_bodies_and_cross_site_writes_are_refused(self) -> None:
        await self.boot()
        self.assertRefused(await self.client.get("/api/chat/review/rv_nope"), 404, "review_not_found")
        self.assertRefused(await self.decide("rv_nope", decision="allow"), 404, "review_not_found")
        self.assertEqual((await self.client.post("/api/chat/review/rv_nope/decision",
                                                 json={"decision": "allow"})).status_code, 422)  # no key
        self.assertEqual((await self.decide("rv_nope", decision="maybe")).status_code, 422)
        self.assertEqual((await self.client.get("/api/chat/review", params={"limit": 501})).status_code, 422)
        cross = await self.client.post("/api/chat/review/rv_nope/decision", headers={"Origin": "https://evil.example"},
                                       json={"decision": "allow", "idempotency_key": "k"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual((await self.client.delete("/api/chat/rules/default:git-read",
                                                   headers={"Origin": "https://evil.example"})).status_code, 403)

    async def test_a_decision_must_fit_its_kind(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        approval = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="approval", title="Deploy to staging", origin="agent", session_id=sid)))
        self.assertRefused(await self.decide(approval, decision="allow"), 400, "bad_decision")
        self.assertRefused(await self.decide(approval, decision="approve", scope="rule"), 400, "bad_decision")
        self.assertRefused(await self.decide(approval, decision="approve", answer="yes"), 400, "bad_decision")
        self.assertRefused(await self.decide(approval, decision="approve", edited_input=PUSH), 400, "bad_decision")


if __name__ == "__main__":
    unittest.main()


class ReviewFindingTests(ReviewTestCase):
    """What the independent review of `ad64f42` found, each fixed."""

    def test_no_prefix_rule_is_offered_for_b1s_residual_families(self) -> None:
        import tempfile
        from pathlib import Path
        from services.cowork_agent.storm.review.items import permission_options
        from services.cowork_agent.storm.review.paths import Places
        from services.cowork_agent.storm.review.tiers import assess_command
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            places = Places(session_folder=folder, home=Path.home(), state_root=folder / "state", worktree=None,
                            project_folder=folder, worktrees_root=folder / ".xo" / "worktrees", git_common_dir=None,
                            cli_homes=(), project_config_paths=())

            def offered(command: str) -> list[str]:
                found = assess_command(command, places, folder)
                return [o["id"] for o in permission_options(
                    kind="command", tier=found.tier, assessment=found, revoked=frozenset(), coordinator=False,
                    thread_ts="1.0", project_pid="pid-demo", project_name="demo")]
            for command in ("git clone https://github.com/acme/lib vendor/lib", "git fetch origin main",
                            "git pull origin main", "git ls-remote origin main", "npm install left-pad",
                            "pip install requests", "gh pr create --fill", "cp src/a.txt src/b.txt",
                            "git checkout main src/a.py", "sed s/a/b/ src/a.txt"):
                with self.subTest(command=command):
                    self.assertNotIn("rule:project:prefix", offered(command))
                    self.assertIn("rule:project", offered(command))
            self.assertIn("rule:project:prefix", offered("make test V=1"))

            # The exact rule `git fetch origin main` never covers the `--upload-pack` form: that asks.
            from services.cowork_agent.storm.review.policy import Action, PolicyContext, SessionRef, decide
            from services.cowork_agent.storm.review.rules import Match, Rule
            exact = Rule(id="r1", scope="project", effect="allow", kind="command",
                         match=Match(command=("git", "fetch", "origin", "main")), sentence="s", tier=Tier.NOTABLE,
                         project_pid="pid-demo")
            session = SessionRef(id="cse_x", role="thread", cwd=folder, project_pid="pid-demo", thread_ts="1.0")
            ctx = PolicyContext(places=places, now="2026-10-16T00:00:00Z", rules=(exact,))

            def effect(command: str) -> str:
                return decide(Action(kind="command", tool="Bash", session=session, command=command), ctx).effect
            self.assertEqual(effect("git fetch origin main"), "allow")
            self.assertEqual(effect("git fetch --upload-pack='touch .claude/pwn; git-upload-pack' origin main"), "ask")

    async def test_a_secret_answer_is_never_stored_and_never_leaves_its_call(self) -> None:
        from services.cowork_agent.storm.review.decisions import Request, plan
        question = {"id": "token", "question": "Paste the token", "options": [], "secret": True}
        item = {"kind": "question", "title": "Paste the token", "detail": {"questions": [question]}}
        held = plan(item, {"call": {"kind": "question"}}, Request(decision="answer", answer="s3cr3t",
                                                                   idempotency_key="k"))
        self.assertEqual((held.record["answer"], held.call.answers), ({"token": "[secret]"}, {"token": "s3cr3t"}))
        # The kept digest of the request never covers the secret, and a retry with it still matches.
        other = plan(item, {"call": {"kind": "question"}}, Request(decision="answer", answer="0000",
                                                                    idempotency_key="k"))
        self.assertEqual(held.record["request_sha256"], other.record["request_sha256"])
        self.assertEqual(Request(decision="answer", answer="s3cr3t", idempotency_key="k").digest(item),
                         held.record["request_sha256"])

        engine = await self.boot()
        sid = await self.kit.session()
        call = {"session_id": sid, "epoch": 0, "kind": "question", "tool": "question", "input_sha256": "0" * 64,
                "summary": "Paste the token"}
        item_id = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="question", title="Paste the token", origin="cli", session_id=sid, blocking=True,
            detail={"questions": [question]}, native={"request_id": "q1", "call": call})))
        # Its call is gone: the answer would wait in a wake, so nothing is written.
        self.assertRefused(await self.decide(item_id, decision="answer", answer="s3cr3t"), 409, "secret_needs_a_call")
        self.assertEqual((await self.queue.get_item(item_id))["state"], "open")
        self.assertEqual(self.kit.rows("SELECT count(*) FROM events WHERE data LIKE '%s3cr3t%'"), [(0,)])
        self.assertEqual(self.kit.rows("SELECT count(*) FROM wakes WHERE envelope LIKE '%s3cr3t%'"), [(0,)])

    async def test_a_thread_rule_outside_any_project_is_read_and_a_bad_deny_rule_fails_closed(self) -> None:
        from services.cowork_agent.storm.review import rule_store
        from services.cowork_agent.storm.review.rules import Match
        engine = await self.boot()
        made = await engine.write(lambda w: rule_store.create(
            w, scope="thread", kind="command", match=Match(command=("make", "test")), effect="allow",
            sentence="In this thread, run exactly `make test` without asking", tier=Tier.NOTABLE, thread_ts="1.0"))
        rules, _ = await engine.store.read(lambda conn: rule_store.for_policy(conn, None, "1.0"))
        self.assertEqual([r.id for r in rules], [made["id"]])
        self.assertEqual((await engine.store.read(lambda conn: rule_store.for_policy(conn, None, "2.0")))[0], ())
        await engine.write(lambda w: w.execute(
            "INSERT INTO rules (id, seq, scope, tool, match, effect, sentence, tier, created_at) VALUES "
            "('r_bad', 999, 'workspace', 'command', '{\"nonsense\": 1}', 'deny', 'x', 'routine', 't')"))
        with self.assertRaises(TypeError):
            await engine.store.read(lambda conn: rule_store.for_policy(conn, None, None))

    async def test_a_queue_that_cannot_start_takes_the_engine_down_with_it(self) -> None:
        from unittest import mock
        from services.cowork_agent.storm.review import queue as review_queue
        kit = self.kit
        with mock.patch.object(review_queue.ReviewQueue, "start", side_effect=RuntimeError("no timer")):
            with self.assertRaises(RuntimeError):
                await review_queue.boot(runtimes=lambda agent: None, providers=[AGENT], clock=kit.clock)
        with self.assertRaises(storm_engine.EngineUnavailable):
            storm_engine.current()
        self.assertIsNotNone(await self.boot())                   # the lock was let go

    async def test_a_failed_expiry_pass_is_retried(self) -> None:
        from unittest import mock
        engine = await self.boot()
        sid = await self.kit.session()
        item = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="approval", title="Deploy?", origin="agent", session_id=sid)))
        real = self.queue._expire
        calls = []

        def failing_once(w):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk full")
            return real(w)
        with mock.patch.object(self.queue, "_expire", side_effect=failing_once), \
                self.assertLogs("services.cowork_agent.storm.review.queue", "ERROR"):
            self.kit.clock.advance(7 * DAY + 1)
            await self.kit.until(lambda: len(calls) == 1)
            await asyncio.sleep(0.05)
            self.kit.clock.advance(2)
            await self.kit.recorder.seen("review.expired")
        self.assertEqual((await self.queue.get_item(item))["state"], "expired")

    async def test_a_decision_that_races_a_withdrawal_answers_withdrawn(self) -> None:
        from unittest import mock
        from services.cowork_agent.storm.review import engine_link
        engine = await self.boot()
        sid = await self.kit.session()
        call = {"session_id": sid, "epoch": 0, "kind": "permission", "tool": "Bash", "input_sha256": "0" * 64,
                "summary": "`x`"}
        item = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="permission", title="Run `x`", origin="cli", session_id=sid, risk=Tier.NOTABLE, blocking=True,
            detail={"input_sha256": "0" * 64}, native={"request_id": "r1", "call": call})))
        deliver = engine_link.deliver_call

        async def withdrawn_first(*args, **kwargs):
            await engine.write(lambda w: self.queue.withdraw(w, item, "withdrawn"))
            return await deliver(*args, **kwargs)
        with mock.patch.object(engine_link, "deliver_call", withdrawn_first):
            self.assertRefused(await self.decide(item, decision="allow"), 409, "withdrawn")
        self.assertEqual(self.kit.rows("SELECT count(*) FROM grants"), [(0,)])

    async def test_a_reused_client_id_with_another_rule_is_refused(self) -> None:
        await self.boot()
        body = {"scope": "workspace", "kind": "command", "match": {"command": ["git", "status"]}, "effect": "allow",
                "sentence": "In every project, run exactly `git status` without asking", "client_id": "c1"}
        first = (await self.client.post("/api/chat/rules", json=body)).json()
        self.assertEqual((await self.client.post("/api/chat/rules", json=body)).json()["id"], first["id"])
        self.assertRefused(await self.client.post("/api/chat/rules", json={
            **body, "effect": "deny", "sentence": "In every project, never run exactly `git status`"}),
            409, "client_id_reused")

    async def test_a_deny_may_name_its_option_and_sentences_quote_words(self) -> None:
        from services.cowork_agent.storm.review.rules import Match, render_sentence
        self.assertEqual(render_sentence("allow", "thread", "command", Match(command=("rm", "-f", "a b")), None),
                         "In this thread, run exactly `rm -f 'a b'` without asking")
        engine = await self.boot()
        sid = await self.kit.session()
        call = {"session_id": sid, "epoch": 0, "kind": "permission", "tool": "Bash", "input_sha256": "0" * 64,
                "summary": "`x`"}
        item = await engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="permission", title="Run `x`", origin="cli", session_id=sid, risk=Tier.NOTABLE, blocking=True,
            detail={"input_sha256": "0" * 64}, native={"request_id": "r1", "call": call})))
        self.assertRefused(await self.decide(item, decision="allow", scope="thread", option="once"), 400, "bad_option")
        self.assertEqual((await self.decide(item, decision="deny", option="deny")).json()["state"], "decided")
