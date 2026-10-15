"""The review queue end to end on the fake claude CLI through the engine and
the decision route (design ch 15 §5–§7; ch 18 §2's proof for PR 9): a held
call's item opens in one transaction and the route's decision goes into the
call; ``decided_by`` is the server's; a decision is single-use and bound to
its input's hash; a call that is gone gets a grant the retry spends once;
scope limits by tier; "make it a rule" needs the server's sentence; revoking
a default rule changes what the policy allows; a sub-agent's item names it."""

from __future__ import annotations

import unittest

from tests.fakes import claude, expect
from tests.storm_engine_kit import SID, message
from tests.storm_review_kit import PUSH, ReviewTestCase


def input_sha256(raw: dict) -> str:
    """The raw-input hash this commit's edits bind (the next commits refuse edits)."""
    import hashlib
    import json
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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

    async def test_an_edited_input_goes_into_the_call_and_is_what_the_decision_is_bound_to(self) -> None:
        kit = self.kit
        asked, edited = {"command": "npm install p-retry@6"}, {"command": "npm install p-retry@5"}
        kit.scenario(self.started(
            claude.tool_call(SID, "t1", "Bash", asked), claude.can_use_tool("p1", "Bash", asked, tool_use_id="t1"),
            expect({"type": "control_response", "response": {"request_id": "p1", "response": {
                "behavior": "allow", "updatedInput": edited}}}),
            claude.tool_result(SID, "t1", "installed"), *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        item = await self.item()
        self.assertRefused(await self.decide(item["id"], decision="allow", scope="thread", edited_input=edited),
                           400, "scope_not_allowed")
        decided = (await self.decide(item["id"], decision="allow", edited_input=edited)).json()
        self.assertEqual((decided["decision"]["edited"], decided["decision"]["edited_input"]), (True, edited))
        self.assertEqual(decided["decision"]["input_sha256"], input_sha256(edited))
        self.assertNotEqual(decided["decision"]["input_sha256"], item["detail"]["input_sha256"])
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
        # The run's start is recorded by the engine's pump while the CLI's
        # check reaches the host on its own: a real sub-agent asks only after
        # its model has worked, which the pause stands for.
        kit.scenario(self.started(claude.task_started(SID, "task1", description="Check migrations"), {"sleep": 0.3},
                                  claude.can_use_tool("p1", "Bash", PUSH, agent_id="task1"),
                                  claude.permission_answer("p1", "deny"), *claude.text_turn(SID, "ok"),
                                  claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        item = await self.item()
        self.assertEqual(item["title"], "A sub-agent ('Check migrations') wants to run `git push origin main`")
        self.assertEqual((item["run_id"], item["detail"]["run"]), ("task1", {"id": "task1", "title": "Check migrations"}))
        await self.decide(item["id"], decision="deny", note="not from a sub-agent")
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.answers("p1")[0]["message"], "not from a sub-agent")

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
                      claude.can_use_tool("p2", "Bash", PUSH, tool_use_id="t2"), claude.permission_answer("p2", "allow"),
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


class ScopeTests(ReviewTestCase):
    async def test_scope_limits_follow_the_tier_and_a_rule_needs_the_servers_sentence(self) -> None:
        kit = self.kit
        install = {"command": "npm install left-pad"}
        status = {"command": "git status"}
        kit.scenario(self.started(
            claude.can_use_tool("p1", "Bash", PUSH), claude.permission_answer("p1", "allow"),
            claude.can_use_tool("p2", "Bash", install), claude.permission_answer("p2", "allow"),
            claude.can_use_tool("p3", "Bash", status), claude.permission_answer("p3", "allow"),
            claude.can_use_tool("p4", "Bash", install), claude.permission_answer("p4", "allow"),
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
                                             rule_sentence="In every project, run `npm install` without asking"),
                           400, "scope_not_allowed")
        wrong = self.assertRefused(await self.decide(npm["id"], decision="allow", scope="rule",
                                                     option="rule:project:prefix", rule_sentence="always npm"),
                                   400, "rule_sentence_mismatch")
        self.assertEqual(wrong["sentence"], "In demo, run `npm install` without asking")
        made = (await self.decide(npm["id"], decision="allow", scope="rule", option="rule:project:prefix",
                                  rule_sentence=wrong["sentence"])).json()
        self.assertEqual(made["decision"]["option"], "rule:project:prefix")

        # Routine (once its default rule is revoked): a workspace rule.
        git = await self.item(count=3, session_id=sid)
        self.assertEqual(git["risk"], "routine")
        workspace = next(o for o in git["options"] if o["id"] == "rule:workspace")
        self.assertEqual((await self.decide(git["id"], decision="allow", scope="rule", option="rule:workspace",
                                            rule_sentence=workspace["sentence"])).status_code, 200)

        # The project rule answers the next install: nobody is asked.
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(len(kit.recorder.of("review.opened")), 3)
        rules = (await self.client.get("/api/chat/rules")).json()["rules"]
        self.assertEqual([(r["scope"], r["tier"], r["match"], r["from_item"], r["uses"]) for r in rules], [
            ("thread", "destructive", {"command": ["git", "push", "origin", "main"]}, push["id"], 0),
            ("project", "notable", {"prefix": ["npm", "install"]}, npm["id"], 1),
            ("workspace", "routine", {"command": ["git", "status"]}, git["id"], 0)])
        self.assertEqual(rules[0]["thread_ts"], "1760518860.000100")


if __name__ == "__main__":
    unittest.main()
