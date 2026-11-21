"""Decision-card data: a command's title in words, the agent's own reason,
grouped risk reasons, a recommendation only with its reason, and every
withdrawal with a one-line ``why`` (an owner's reply in the thread, a
resolved thread, a newer ask on the same subject, an unanswered
non-blocking ask)."""

from __future__ import annotations

import json
import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.questions import native_answer, options
from services.cowork_agent.storm.engine.session import REPLIED_INSTEAD
from services.cowork_agent.storm.review.items import ItemSpec, risk_groups, risk_line, suggested, view, COLUMNS
from services.cowork_agent.storm.review.queue import _permission_title, _why
from services.cowork_agent.storm.review.tiers import Reason, Tier
from tests.fakes import claude
from tests.storm_engine_kit import SID, message
from tests.storm_review_kit import PUSH, ReviewTestCase

PUSHING = {**PUSH, "description": "Push main to origin"}


def _request(**fields) -> c.PermissionRequest:
    base = {"request_id": "r1", "tool": "Bash", "kind": "command", "summary": "run `git push origin main`",
            "command": "git push origin main", "run": None, "call_id": "t1", "input": PUSH, "cwd": None}
    return c.PermissionRequest(**{**base, **fields})


class CardShapeTests(unittest.TestCase):
    def test_a_commands_title_is_its_description_and_why_is_the_agents_reason(self) -> None:
        self.assertEqual(_permission_title(_request(description="Push main to origin"), None),
                         "Push main to origin")
        self.assertEqual(_permission_title(_request(), None), "Run a command")      # never the command itself
        self.assertEqual(_why(_request(reason="The release needs it")), "The release needs it")
        self.assertIsNone(_why(_request(description="Push it", reason="Push it")))   # said once, as the title
        self.assertIsNone(_why(_request()))

    def test_risk_reasons_are_grouped_by_words_with_their_paths(self) -> None:
        reasons = [Reason(Tier.NOTABLE, "it reads outside this session's folder", p) for p in ("/a", "/b", "/c", "/a")]
        groups = risk_groups([*reasons, Reason(Tier.DESTRUCTIVE, "it deletes a protected path", "/etc"),
                              Reason(Tier.NOTABLE, "it names no file")])
        self.assertEqual(groups, [
            {"kind": "notable", "text": "it reads outside this session's folder", "count": 3, "paths": ["/a", "/b", "/c"]},
            {"kind": "destructive", "text": "it deletes a protected path", "count": 1, "paths": ["/etc"]},
            {"kind": "notable", "text": "it names no file", "count": 1, "paths": []}])
        self.assertEqual([risk_line(g) for g in groups], ["it reads outside this session's folder (3 paths)",
                                                          "it deletes a protected path (/etc)", "it names no file"])

    def test_an_item_kept_with_plain_string_reasons_still_reads(self) -> None:
        names = [n.strip() for n in COLUMNS.split(",")]
        row = {n: None for n in names} | {
            "id": "rv_1", "seq": 1, "kind": "permission", "state": "open", "risk": "notable", "blocking": 1,
            "title": "Run `x`", "detail": "{}", "options": "[]", "origin": "cli", "parked": 0, "created_at": "t",
            "annotations": "[]", "risk_reasons": json.dumps(["it reads outside this session's folder (/a)"])}
        item = view(tuple(row[n] for n in names))
        self.assertEqual(item["risk_reasons"], ["it reads outside this session's folder (/a)"])
        self.assertEqual(item["risk_groups"], [{"kind": None, "text": "it reads outside this session's folder (/a)",
                                                "count": 1, "paths": []}])

    def test_a_recommendation_is_kept_only_with_its_reason(self) -> None:
        (q,) = suggested([{"id": "answer", "options": [
            {"label": "A", "description": "fastest", "recommended": True, "cost": "one more day"},
            {"label": "B", "description": "", "recommended": True, "cost": ""},
            {"label": "C", "description": "cheap"}]}])
        self.assertEqual(q["options"], [{"label": "A", "description": "fastest", "recommended": True,
                                         "cost": "one more day"},
                                        {"label": "B", "description": ""}, {"label": "C", "description": "cheap"}])

    def test_a_clis_recommended_suffix_becomes_the_mark_and_goes_back_in_the_answer(self) -> None:
        out, originals = options([{"label": "Postgres (Recommended)", "description": "it scales"},
                                  {"label": "SQLite", "description": "simple"},
                                  {"label": "(Recommended)", "description": ""},
                                  {"label": "SQLite (recommended)", "description": "dup"}])
        self.assertEqual(out, [{"label": "Postgres", "description": "it scales", "recommended": True},
                               {"label": "SQLite", "description": "simple"},
                               {"label": "(Recommended)", "description": ""},          # nothing left to show
                               {"label": "SQLite (recommended)", "description": "dup"}])  # would answer SQLite
        self.assertEqual(native_answer("Postgres", originals), "Postgres (Recommended)")
        self.assertEqual(native_answer(["Postgres", "SQLite"], originals), ["Postgres (Recommended)", "SQLite"])
        self.assertEqual(native_answer("my own words", originals), "my own words")


class WithdrawTests(ReviewTestCase):
    async def open(self, engine, **spec) -> str:
        return await engine.write(lambda w: self.queue.open_item(w, ItemSpec(**spec)))

    def withdrawn(self) -> dict[str, dict]:
        return {e.data["id"]: e.data for e in self.kit.recorder.of("review.withdrawn")}

    async def test_the_owners_reply_denies_a_held_call_with_a_note(self) -> None:
        kit = self.kit
        kit.scenario(self.started(claude.tool_call(SID, "t1", "Bash", PUSHING),
                                  claude.can_use_tool("p1", "Bash", PUSHING, tool_use_id="t1"),
                                  claude.permission_answer("p1", "deny"), claude.tool_result(SID, "t1", "denied"),
                                  *claude.text_turn(SID, "ok"), claude.idle(SID),
                                  claude.user_message(), *claude.text_turn(SID, "will do"), claude.idle(SID)))
        engine = await self.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "push it"))
        item = await self.item()
        self.assertEqual((item["title"], item["detail"]["command"], item["why"]),
                         ("Push main to origin", "git push origin main", None))
        self.assertEqual(item["risk_groups"][0]["kind"], "destructive")
        self.assertTrue(all(isinstance(r, str) for r in item["risk_reasons"]))        # today's shape

        await engine.deliver(sid, message(2, "don't, I'll push it myself"))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(self.withdrawn()[item["id"]], {"id": item["id"], "reason": "superseded",
                                                        "why": "You answered in the thread.", "by": "person_message"})
        self.assertEqual(self.answers("p1")[0]["message"], REPLIED_INSTEAD)
        self.assertRefused(await self.decide(item["id"], decision="allow"), 409, "withdrawn")

    async def test_a_reply_closes_the_threads_asks_and_tells_their_agent(self) -> None:
        engine = await self.boot()
        sid = await self.thread_session()
        where = {"session_id": sid, "chat_id": "chat_p1", "thread_ts": "1760518860.000100", "origin": "agent"}
        ask = await self.open(engine, kind="approval", title="Deploy?", **where)
        proposal = await self.open(engine, kind="create_project", title="A project", **where)
        closed = await engine.write(lambda w: self.queue.person_replied(w, sid))
        self.assertEqual(closed, [ask])                                    # words can't make a project
        self.assertEqual(self.withdrawn()[ask]["by"], "person_message")
        ((text,),) = self.kit.rows("SELECT envelope FROM wakes WHERE id = ?", (f"review-replied:{ask}",))
        self.assertIn(ask, text)
        self.assertEqual((await self.queue.get_item(proposal))["state"], "open")

    async def test_a_coordinators_chat_answers_nothing(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        await engine.write(lambda w: w.execute("UPDATE sessions SET role = 'project-coordinator' WHERE id = ?", (sid,)))
        ask = await self.open(engine, kind="approval", title="Deploy?", session_id=sid, origin="agent")
        self.assertEqual(await engine.write(lambda w: self.queue.person_replied(w, sid)), [])
        self.assertEqual((await self.queue.get_item(ask))["state"], "open")

    async def test_resolving_a_thread_withdraws_what_it_asks(self) -> None:
        engine = await self.boot()
        sid = await self.thread_session()
        ask = await self.open(engine, kind="question", title="Which DB?", session_id=sid, chat_id="chat_p1",
                              thread_ts="1760518860.000100", origin="agent")
        self.assertEqual(await engine.write(lambda w: self.queue.thread_resolved(w, "chat_p1", "1760518860.000100")),
                         [ask])
        self.assertEqual((self.withdrawn()[ask]["reason"], self.withdrawn()[ask]["why"]),
                         ("resolved", "The thread was resolved."))

    async def test_a_newer_ask_on_the_same_subject_replaces_the_older(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        mine = {"session_id": sid, "origin": "agent"}
        old = await self.open(engine, kind="approval", title="Deploy  the site?", **mine)
        by_command = await self.open(engine, kind="approval", title="Ship it", detail={"command": "make deploy"},
                                     **mine)
        other_kind = await self.open(engine, kind="question", title="Deploy the site?", **mine)
        new = await self.open(engine, kind="approval", title="deploy the site?", detail={"command": "make deploy"},
                              **mine)
        replaced = await engine.write(lambda w: self.queue.replace_asks(w, new))
        self.assertEqual(replaced, [old, by_command])
        self.assertEqual({i: self.withdrawn()[i]["by"] for i in replaced}, {old: new, by_command: new})
        self.assertEqual(self.withdrawn()[old]["why"], "A newer ask replaced it.")
        self.assertEqual((await self.queue.get_item(other_kind))["state"], "open")

    async def test_a_withdrawal_names_its_reason_redacted(self) -> None:
        engine = await self.boot()
        sid = await self.kit.session()
        ask = await self.open(engine, kind="approval", title="Deploy?", session_id=sid, origin="agent")
        secret = "ghp_" + "a1B2" * 9
        await engine.write(lambda w: self.queue.withdraw(w, ask, "withdrawn_by_agent",
                                                         why=f"The agent withdrew it: done with {secret}"))
        why = self.withdrawn()[ask]["why"]
        self.assertTrue(why.startswith("The agent withdrew it: done with"))
        self.assertNotIn(secret, why)
        self.assertFalse(await engine.write(lambda w: self.queue.withdraw(w, ask, "withdrawn")))   # once


if __name__ == "__main__":
    unittest.main()
