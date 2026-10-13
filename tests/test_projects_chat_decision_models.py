"""Decision-model routing: Sage and Jev behind one interface, local abstain,
Jev's tags emulated with yes/no questions, and the router applying a
decision through the comms tools or handing the message to the agent."""

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import services.decision_models as dm
from services.decision_models import jev, sage
from services.levanto import client as levanto
from services.projects_chat import coordinators, store, turns, wakes, workspace  # noqa: F401
from services.projects_chat.router import model

ROOT = Path(__file__).resolve().parents[1]


class JevTests(unittest.TestCase):
    def test_tags_become_one_noul_each_and_applies_comes_from_thresholds(self) -> None:
        q = [dm.Choice("intent", "What?", {"a": "first", "b": "second"}),
             dm.Tags("projects", "Which?", {"calc": "a calculator", "site": "a website"})]
        body = jev.request_body("hello", q)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"]["intent"]["criteria"], {"a": "first", "b": "second"})
        self.assertEqual(body["questions"]["projects__calc"]["type"], "noul")
        answers = jev.parse(q, {"answers": {
            "intent": {"choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
            "projects__calc": {"noul": 0.93}, "projects__site": {"noul": 0.5}}})
        self.assertEqual((answers["intent"].chosen, answers["intent"].probability), ("b", 0.8))
        self.assertEqual(answers["projects"].applies, {"calc": True, "site": None})

    def test_without_a_key_nothing_is_sent(self) -> None:
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
             mock.patch.object(httpx.AsyncClient, "post") as post:
            result = asyncio.run(jev.ask("x", [dm.Choice("c", "?", {"a": "a"})], timeout=1))
        post.assert_not_called()
        self.assertEqual(result.answers["c"].error, "not_configured")

    def test_only_jev_reads_its_key(self) -> None:
        readers = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                   if "venv" not in p.parts and "tests" not in p.parts and "TYPESAFE_API_KEY" in p.read_text(errors="ignore")]
        self.assertEqual(readers, ["services/decision_models/jev.py"])


class SageTests(unittest.TestCase):
    def test_choice_and_tags_map_from_decide(self) -> None:
        async def fake(content, question, *, timeout, reasoning=None):
            if question["kind"] == "choice":
                return levanto.SageResult(ok=True, status=200, data={"result": {
                    "chosen": None, "probabilities": [{"option": "a", "probability": 0.51}, {"option": "b", "probability": 0.49}]},
                    "meta": {"usage": {"billed_input_tokens": 300}}})
            return levanto.SageResult(ok=True, status=200, data={"result": {"tags": [
                {"id": "calc", "probability": 0.9, "applies": True}, {"id": "site", "probability": 0.4, "applies": None}]},
                "meta": {"usage": {"billed_input_tokens": 200}}})
        with mock.patch.object(levanto, "decide", fake):
            result = asyncio.run(sage.ask("x", [dm.Choice("c", "?", {"a": "A", "b": "B"}),
                                                dm.Tags("t", "?", {"calc": "c", "site": "s"})], timeout=1))
        self.assertIsNone(result.answers["c"].chosen, "Sage's null is kept as no pick")
        self.assertTrue(result.answers["c"].abstains(0.5))
        self.assertEqual(result.answers["t"].applies, {"calc": True, "site": None})
        self.assertEqual(result.input_tokens, 500)


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        for name in ("calc", "site"):
            (base / "xo" / name / ".xo").mkdir(parents=True)
            (base / "xo" / name / ".xo" / "project.json").write_text(json.dumps({"pid": f"p-{name}"}))
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(base / "state"), "XO_PROJECTS_ROOT": str(base / "xo"),
                                           "XO_ROUTER_MODE": "decision", "XO_ROUTER_DECISION_MODEL": "jev",
                                           "XO_ROUTER_ABSTAIN_BELOW": "0.6"})
        env.start()
        self.addCleanup(env.stop)

    def answers(self, **by_id):
        return dm.Result("jev", by_id, input_tokens=400, latency_ms=210.0)

    def test_a_confident_new_task_starts_a_thread_on_the_message(self) -> None:
        msg = store.post_message("calc", author_kind="owner", author_id="owner", text="Add a --help flag. Thanks!")
        result = self.answers(intent=dm.Answer("intent", "choice", chosen="new_task", probability=0.9,
                                               probabilities={"new_task": 0.9}))
        with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=result)):
            decision = asyncio.run(model.route("project", "calc", msg))
        self.assertIsNone(decision.escalated_from)
        thread = store.get_thread("calc", msg["id"])
        self.assertEqual(thread["title"], "Add a --help flag")
        [logged] = store.read_decisions("calc")
        self.assertEqual((logged["provider"], logged["actions"][0]["kind"]), ("jev", "start_thread"))
        self.assertEqual(len(wakes.pending("calc", thread["session_id"])), 1)

    def test_a_follow_up_is_relayed_to_the_thread_the_model_picked(self) -> None:
        first = store.post_message("calc", author_kind="owner", author_id="owner", text="Add tests")
        with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=self.answers(
                intent=dm.Answer("intent", "choice", chosen="new_task", probability=0.95)))):
            asyncio.run(model.route("project", "calc", first))
        follow = store.post_message("calc", author_kind="owner", author_id="owner", text="Also cover division")
        with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=self.answers(
                intent=dm.Answer("intent", "choice", chosen="follow_up", probability=0.8),
                thread=dm.Answer("thread", "choice", chosen=first["id"], probability=0.85)))):
            decision = asyncio.run(model.route("project", "calc", follow))
        self.assertEqual([a.kind for a in decision.actions], ["message_thread"])
        session = store.get_thread("calc", first["id"])["session_id"]
        self.assertIn(follow["id"], wakes.pending("calc", session)[-1]["envelope"])

    def test_unsure_or_wordy_messages_go_to_the_agent(self) -> None:
        for answer in (dm.Answer("intent", "choice", chosen="new_task", probability=0.4),
                       dm.Answer("intent", "choice", chosen="question", probability=0.97),
                       dm.Answer("intent", "choice", error="rate")):
            msg = store.post_message("calc", author_kind="owner", author_id="owner", text="Hmm")
            with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=self.answers(intent=answer))):
                decision = asyncio.run(model.route("project", "calc", msg))
            self.assertEqual(decision.escalated_from, "jev")
            self.assertEqual(store.list_threads("calc"), [])

    def test_the_workspace_forwards_to_every_project_that_clearly_applies(self) -> None:
        msg = store.post_message(None, author_kind="owner", author_id="owner", text="Footer on site, --help in calc")
        with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=self.answers(
                projects=dm.Answer("projects", "tags", probabilities={"calc": 0.9, "site": 0.85},
                                   applies={"calc": True, "site": True})))):
            decision = asyncio.run(model.route("workspace", None, msg))
        self.assertEqual(sorted(a.target for a in decision.actions), ["proj:calc", "proj:site"])
        for project in ("calc", "site"):
            self.assertEqual(store.read_messages(project)[0]["forwarded_from"]["message_id"], msg["id"])
        self.assertEqual(store.read_messages(None)[-1]["text"], "Sent to calc, site.")

    def test_escalation_wakes_the_coordinator_with_a_note(self) -> None:
        msg = store.post_message("calc", author_kind="owner", author_id="owner", text="What does calc do?")
        with mock.patch.object(dm, "ask", mock.AsyncMock(return_value=self.answers(
                intent=dm.Answer("intent", "choice", chosen="question", probability=0.9)))):
            asyncio.run(turns._route_with_model("calc", msg))
        [wake] = wakes.pending("calc", coordinators.live("calc")["id"])
        self.assertIn("decision model handed this message to you", wake["envelope"])
        self.assertIn(msg["id"], wake["envelope"])


if __name__ == "__main__":
    unittest.main()
