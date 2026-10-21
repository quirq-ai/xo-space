"""The review queue end to end on the fake claude CLI through the engine and
the decision route (design ch 15 §5–§7; ch 18 §2's proof for PR 9): a held
call's item opens in one transaction and the route's decision goes into the
call; ``decided_by`` is the server's; a decision is single-use and bound to
its input's hash; a call that is gone gets a grant the retry spends once;
scope limits by tier; "make it a rule" needs the server's sentence; revoking
a default rule changes what the policy allows; a sub-agent's item names it."""

from __future__ import annotations

import dataclasses
import json
import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import CallRef
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.review.tiers import Tier
from tests.fakes import claude, expect
from tests.storm_engine_kit import SID, message
from tests.storm_review_kit import PUSH, ReviewTestCase

#: The input of plan mode's end, as claude 2.1.306 asks about it (the B4 live run).
PLAN_INPUT = {"plan": "# Plan\n1. Write plan.txt\n2. Reply `written`", "planFilePath": "/home/u/.claude/plans/p.md"}
PLAN_KEY = "sk-ant-api03-" + "Pl5" * 10      # a key the plan quotes


class HeldCallTests(ReviewTestCase):
    async def test_a_held_call_is_decided_through_the_route_straight_into_the_call(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"),
                                  claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "pushed"),
                                  *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "push it"))

        # One transaction: the item, its review.opened, the session waiting.
        opened = await kit.recorder.seen("review.opened", session_id=sid)
        waiting = await kit.recorder.seen("session", session_id=sid, status="waiting_on_person")
        self.assertEqual(waiting.seq, opened.seq + 1)
        listed = (await self.client.get("/api/chat/review", params={"state": "open"})).json()
        (item,) = listed["items"]
        self.assertEqual(item["seq"], opened.seq)
        self.assertEqual((item["kind"], item["state"], item["risk"], item["blocking"], item["title"]),
                         ("permission", "open", "destructive", True, "Run `git push origin main`"))
        self.assertTrue(item["risk_reasons"])                        # computed by the policy
        self.assertEqual((item["detail"]["command"], item["detail"]["input"]), (PUSH["command"], PUSH))
        # A plain session has no thread and no project: destructive is allowed once or denied.
        self.assertEqual([o["id"] for o in item["options"]], ["once", "deny"])
        self.assertIsNone(item["expires_at"])                         # held: never expires

        # Only the server names who decided: a body that tries is refused.
        forged = await self.decide(item["id"], decision="allow", decided_by={"kind": "owner", "via": "ui"})
        self.assertEqual(forged.status_code, 422)
        self.assertEqual((await self.client.get(f"/api/chat/review/{item['id']}")).json()["state"], "open")

        decided = await self.decide(item["id"], decision="allow", idempotency_key="k1")
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()
        self.assertEqual((body["state"], body["delivery"], body["decided_by"]),
                         ("decided", "in_call", {"kind": "owner", "via": "ui"}))
        self.assertEqual((body["decision"]["decision"], body["decision"]["scope"], body["decision"]["input_sha256"]),
                         ("allow", "once", item["detail"]["input_sha256"]))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.answers("p1")[0]["behavior"], "allow")

        # Single use: the same key again is the same result and no new write;
        # another decision is refused.
        again = await self.decide(item["id"], decision="allow", idempotency_key="k1")
        self.assertEqual((again.status_code, again.json()), (200, body))
        self.assertEqual(len(kit.recorder.of("review.decided")), 1)
        self.assertRefused(await self.decide(item["id"], decision="deny"), 409, "already_decided")
        self.assertRefused(await self.decide(item["id"], decision="deny", idempotency_key="k1"), 409,
                           "idempotency_key_reused")
        self.assertEqual(kit.rows("SELECT count(*) FROM grants"), [(0,)])

    async def test_an_edited_input_is_refused_and_an_empty_edit_is_no_edit(self) -> None:
        kit = self.kit
        asked = {"command": "npm install left-pad"}
        edited = {"command": "echo '{\"hooks\":{}}' > .claude/settings.json && git push origin main"}
        kit.scenario(self.started(
            claude.tool_call(SID, "t1", "Bash", asked), claude.can_use_tool("p1", "Bash", asked, tool_use_id="t1"),
            expect({"type": "control_response", "response": {"request_id": "p1", "response": {
                "behavior": "allow", "updatedInput": asked}}}),
            claude.tool_result(SID, "t1", "installed"), *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        item = await self.item()
        # XO can't re-check an edit yet, so none gets past the person (§0.91).
        refused = self.assertRefused(await self.decide(item["id"], decision="allow", edited_input=edited), 400,
                                     "edit_not_supported")
        self.assertIn("Deny", refused["message"])
        self.assertEqual((await self.client.get(f"/api/chat/review/{item['id']}")).json()["state"], "open")
        decided = (await self.decide(item["id"], decision="allow", edited_input={})).json()
        self.assertEqual((decided["delivery"], decided["decision"]["input_sha256"]),
                         ("in_call", item["detail"]["input_sha256"]))
        self.assertNotIn("edited_input", decided["decision"])
        await kit.recorder.seen("turn.finished", session_id=sid)

    async def test_a_native_question_is_answered_by_picking_an_option(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.ask_user_question("q1", "Which branch?", ["main", "dev"]),
                                  claude.permission_answer("q1", "allow"), *claude.text_turn(SID, "ok"),
                                  claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        item = await self.item("question")
        self.assertEqual((item["title"], item["blocking"], item["expires_at"]), ("Which branch?", True, None))
        self.assertRefused(await self.decide(item["id"], decision="answer", option="staging"), 400, "bad_answer")
        self.assertRefused(await self.decide(item["id"], decision="allow"), 400, "bad_decision")
        decided = (await self.decide(item["id"], decision="answer", option="dev")).json()
        self.assertEqual((decided["delivery"], decided["decision"]["answer"]), ("in_call", {"Which branch?": "dev"}))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.answers("q1")[0]["updatedInput"]["answers"], {"Which branch?": "dev"})

    async def test_a_sub_agents_item_names_it(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.task_started(SID, "task1", description="Check migrations"),
                                  claude.can_use_tool("p1", "Bash", PUSH, agent_id="task1"),
                                  claude.permission_answer("p1", "deny"), *claude.text_turn(SID, "ok"),
                                  claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        item = await self.item()
        self.assertEqual(item["title"], "A sub-agent ('Check migrations') wants to run `git push origin main`")
        self.assertEqual((item["run_id"], item["detail"]["run"]),
                         ("task1", {"id": "task1", "title": "Check migrations"}))
        await self.decide(item["id"], decision="deny", note="not from a sub-agent")
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.answers("p1")[0]["message"], "not from a sub-agent")

    async def test_a_plan_is_put_to_the_person_to_approve(self) -> None:
        # The B4 live run: plan mode's end was an item that XO "doesn't know this kind of action (plan)".
        kit = self.kit
        planned = {**PLAN_INPUT, "plan": PLAN_INPUT["plan"] + f"\n3. Call the API with {PLAN_KEY}"}
        kit.scenario(self.started(claude.tool_call(SID, "t1", "ExitPlanMode", planned),
                                  claude.can_use_tool("p1", "ExitPlanMode", planned, tool_use_id="t1"),
                                  claude.permission_answer("p1", "allow"), claude.tool_result(SID, "t1", "approved"),
                                  *claude.text_turn(SID, "writing it"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session(permissions=c.PermissionPolicy(mode="plan"))
        await engine.deliver(sid, message(1, "plan it"))
        item = await self.item()
        self.assertEqual((item["title"], item["risk_reasons"]),
                         ("Approve this plan?", ["the agent asks to go ahead with this plan"]))
        self.assertTrue(item["detail"]["plan"].startswith(PLAN_INPUT["plan"]))       # the plan, to read
        self.assertNotIn(PLAN_KEY, json.dumps(item))                                  # redacted, as all detail is
        await self.decide(item["id"], decision="allow")
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.answers("p1")[0]["behavior"], "allow")

    async def test_a_call_the_cli_withdraws_cannot_be_decided(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 0.3},
                                  {"emit": {"type": "control_cancel_request", "request_id": "p1"}},
                                  *claude.text_turn(SID, "never mind"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        withdrawn = await kit.recorder.seen("review.withdrawn", session_id=sid)
        self.assertRefused(await self.decide(withdrawn.data["id"], decision="allow"), 409, "withdrawn")


class GoneCallTests(ReviewTestCase):
    async def test_a_dead_epochs_allow_is_a_grant_the_retry_spends_once(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSH),
                                  claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t1"), {"sleep": 0.3},
                                  {"exit": 1}))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "push it"))
        await kit.recorder.seen("session", session_id=sid, status="parked")
        parked = (await self.client.get("/api/chat/review", params={"session": sid})).json()["items"][0]
        self.assertTrue(parked["parked"])
        self.assertIsNotNone(parked["expires_at"])                   # 7 days from parking

        # The next process (a new epoch): the retry passes on the grant, the
        # same retry again asks, and other arguments ask too.
        dev = {"command": "git push origin dev"}
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      claude.tool_call(SID, "t2", "Bash", PUSH, message_id="m2"),
                      claude.can_use_tool("p2", "Bash", PUSH, tool_use_id="t2"),
                      claude.permission_answer("p2", "allow"),
                      claude.tool_result(SID, "t2", "pushed"),
                      claude.can_use_tool("p3", "Bash", PUSH, tool_use_id="t3"), claude.permission_answer("p3", "deny"),
                      claude.can_use_tool("p4", "Bash", dev, tool_use_id="t4"), claude.permission_answer("p4", "deny"),
                      *claude.text_turn(SID, "done", message_id="m3"), claude.idle(SID)])
        decided = (await self.decide(parked["id"], decision="allow")).json()
        self.assertEqual(decided["delivery"], "grant")
        self.assertEqual(kit.rows("SELECT tool, input_sha256 FROM grants"),
                         [("Bash", parked["detail"]["input_sha256"])])
        kit.clock.advance(2)                                         # past the crash's backoff

        again = await self.item(count=2, session_id=sid)
        self.assertEqual(again["detail"]["command"], PUSH["command"])
        self.assertEqual(kit.rows("SELECT used_at IS NOT NULL FROM grants"), [(1,)])     # spent by p2
        await self.decide(again["id"], decision="deny")
        other = await self.item(count=3, session_id=sid)
        self.assertEqual(other["detail"]["command"], dev["command"])
        await self.decide(other["id"], decision="deny")
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        decided_seq = kit.recorder.of("review.decided")[0].seq
        self.assertIn(f"review:{parked['id']}:{decided_seq}", kit.recorder.of("turn.finished", sid)[1].data["wakes"])
        self.assertIn("The person allowed `git push origin main`. Run it again.", self.user_texts()[0])


class SupersedeTests(ReviewTestCase):
    """One action, one item (the live run's D1, ch 07 §6): the engine has the
    queue withdraw a session's parked items it has moved on from."""

    async def items(self, engine, sid: str) -> dict[str, str]:
        def ask(digest: str, kind: str = "permission", request: str = "r") -> ItemSpec:
            call = {"session_id": sid, "epoch": 1, "kind": kind, "tool": "Bash", "input_sha256": digest,
                    "summary": "`x`"}
            return ItemSpec(kind=kind, title="Run `x`", origin="cli", session_id=sid, risk=Tier.NOTABLE,
                            blocking=True, detail={"input_sha256": digest},
                            native={"request_id": request, "call": call})
        made = {}
        for name, spec, parked in (("same", ask("a" * 64, request="r1"), True),
                                   ("other", ask("b" * 64, request="r2"), True),
                                   ("question", ask("a" * 64, kind="question", request="r3"), True),
                                   ("held", ask("a" * 64, request="r4"), False)):
            made[name] = await engine.write(lambda w, spec=spec: self.queue.open_item(w, spec))
            if parked:
                await engine.write(lambda w, item=made[name]: self.queue.park(w, item, "restart"))
        return made

    async def test_a_repeated_ask_supersedes_only_the_parked_items_of_the_same_call(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        made = await self.items(engine, sid)
        call = CallRef(session_id=sid, epoch=2, kind="permission", tool="Bash", input_sha256="a" * 64, summary="`x`")
        withdrawn = await engine.write(lambda w: self.queue.supersede(w, sid, by="rv_new", call=call))
        self.assertEqual(withdrawn, [made["same"]])
        self.assertEqual([e.data for e in self.kit.recorder.of("review.withdrawn")],
                         [{"id": made["same"], "reason": "superseded", "by": "rv_new"}])
        states = dict(self.kit.rows("SELECT id, state FROM review_items"))
        self.assertEqual({n: states[i] for n, i in made.items()},
                         {"same": "withdrawn", "other": "open", "question": "open", "held": "open"})
        # The new item never withdraws itself.
        self.assertEqual(await engine.write(lambda w: self.queue.supersede(w, sid, by=made["other"], call=dataclasses
                                                                           .replace(call, input_sha256="b" * 64))), [])

    async def test_a_persons_message_supersedes_every_parked_item_and_a_late_decision_mints_nothing(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        made = await self.items(engine, sid)
        withdrawn = await engine.write(lambda w: self.queue.supersede(w, sid, by="person_message"))
        self.assertEqual(sorted(withdrawn), sorted([made["same"], made["other"], made["question"]]))
        self.assertEqual(self.kit.rows("SELECT id FROM review_items WHERE state = 'open'"), [(made["held"],)])
        for item in withdrawn:
            self.assertRefused(await self.decide(item, decision="allow"), 409, "withdrawn")
        self.assertEqual(self.kit.rows("SELECT count(*) FROM grants"), [(0,)])
        self.assertEqual(self.kit.rows("SELECT count(*) FROM wakes"), [(0,)])

    async def test_after_a_restart_the_persons_message_moves_on_and_the_action_runs_once(self) -> None:
        kit = self.kit
        touch = {"command": "touch parked.txt"}
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", touch),
                                  claude.can_use_tool("p1", "Bash", touch, tool_use_id="t1"), {"sleep": 30}))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "Run touch parked.txt"))
        old = await self.item(session_id=sid)
        await self.queue.stop()
        await storm_engine.shutdown()                                   # the server restarts
        parked, expires = kit.rows("SELECT parked, expires_at FROM review_items WHERE id = ?", (old["id"],))[0]
        self.assertEqual(parked, 1)
        self.assertRegex(expires, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")         # what the engine's hold reads

        kit.scenario(self.started(claude.tool_call(SID, "t2", "Bash", touch),
                                  claude.can_use_tool("p2", "Bash", touch, tool_use_id="t2"),
                                  claude.permission_answer("p2", "allow"), claude.tool_result(SID, "t2", "done"),
                                  *claude.text_turn(SID, "created it", message_id="m2"), claude.idle(SID)))
        self.queue = ReviewQueue()
        engine = await self.boot()
        await engine.deliver(sid, message(2, "go ahead"))               # the person has moved on
        async with kit.pacing():
            new = await self.item(count=2, session_id=sid)
        (withdrawn,) = kit.recorder.of("review.withdrawn", sid)
        self.assertEqual(withdrawn.data, {"id": old["id"], "reason": "superseded", "by": "person_message"})
        self.assertEqual(kit.rows("SELECT id FROM review_items WHERE state = 'open'"), [(new["id"],)])   # one item
        self.assertEqual((await self.decide(new["id"], decision="allow")).json()["delivery"], "in_call")
        async with kit.pacing():
            await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertRefused(await self.decide(old["id"], decision="allow"), 409, "withdrawn")
        self.assertEqual(kit.rows("SELECT count(*) FROM grants"), [(0,)])
        allows = [line["response"].get("request_id") for run in kit.runs() for line in run["stdin"]
                  if line.get("type") == "control_response"
                  and (line["response"].get("response") or {}).get("behavior") == "allow"]
        self.assertEqual(allows, ["p2"])                                # it ran once


class ScopeTests(ReviewTestCase):
    async def test_scope_limits_follow_the_tier_and_a_rule_needs_the_servers_sentence(self) -> None:
        kit = self.kit
        make, again = {"command": "make test V=1"}, {"command": "make test V=2"}
        status = {"command": "git status"}
        kit.scenario(self.started(
            claude.can_use_tool("p1", "Bash", PUSH), claude.permission_answer("p1", "allow"),
            claude.can_use_tool("p2", "Bash", make), claude.permission_answer("p2", "allow"),
            claude.can_use_tool("p3", "Bash", status), claude.permission_answer("p3", "allow"),
            claude.can_use_tool("p4", "Bash", again), claude.permission_answer("p4", "allow"),
            *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await self.boot()
        self.assertEqual((await self.client.delete("/api/chat/rules/default:git-read")).status_code, 200)
        sid = await self.thread_session()
        await engine.deliver(sid, message(1))

        # Destructive: once, or this thread as this exact command; never a rule.
        push = await self.item(session_id=sid)
        self.assertEqual([o["id"] for o in push["options"]], ["once", "thread", "deny"])
        self.assertRefused(await self.decide(push["id"], decision="allow", scope="rule", option="rule:project"),
                           400, "scope_not_allowed")
        self.assertEqual((await self.decide(push["id"], decision="allow", scope="thread")).status_code, 200)

        # Notable: at most the project, and the sentence must be the server's.
        npm = await self.item(count=2, session_id=sid)
        self.assertEqual([o["id"] for o in npm["options"]],
                         ["once", "thread", "rule:project", "rule:project:prefix", "deny"])
        self.assertRefused(await self.decide(npm["id"], decision="allow", scope="rule", option="rule:workspace",
                                             rule_sentence="In every project, run `make test` without asking"),
                           400, "scope_not_allowed")
        wrong = self.assertRefused(await self.decide(npm["id"], decision="allow", scope="rule",
                                                     option="rule:project:prefix", rule_sentence="always make"),
                                   400, "rule_sentence_mismatch")
        self.assertEqual(wrong["sentence"], "In demo, run `make test` without asking")
        # The option names its scope: sending only the option is enough; a scope that disagrees is refused.
        self.assertRefused(await self.decide(npm["id"], decision="allow", scope="thread",
                                             option="rule:project:prefix", rule_sentence=wrong["sentence"]),
                           400, "bad_option")
        made = (await self.decide(npm["id"], decision="allow", option="rule:project:prefix",
                                  rule_sentence=wrong["sentence"])).json()
        self.assertEqual((made["decision"]["option"], made["decision"]["scope"]), ("rule:project:prefix", "rule"))

        # Routine (once its default rule is revoked): a workspace rule.
        git = await self.item(count=3, session_id=sid)
        self.assertEqual(git["risk"], "routine")
        workspace = next(o for o in git["options"] if o["id"] == "rule:workspace")
        self.assertEqual((await self.decide(git["id"], decision="allow", scope="rule", option="rule:workspace",
                                            rule_sentence=workspace["sentence"])).status_code, 200)

        # The project rule answers the next `make test`: nobody is asked.
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(len(kit.recorder.of("review.opened")), 3)
        rules = (await self.client.get("/api/chat/rules")).json()["rules"]
        self.assertEqual([(r["scope"], r["tier"], r["match"], r["from_item"], r["uses"]) for r in rules], [
            ("thread", "destructive", {"command": ["git", "push", "origin", "main"]}, push["id"], 0),
            ("project", "notable", {"prefix": ["make", "test"]}, npm["id"], 1),
            ("workspace", "routine", {"command": ["git", "status"]}, git["id"], 0)])
        self.assertEqual(rules[0]["thread_ts"], "1760518860.000100")


if __name__ == "__main__":
    unittest.main()
