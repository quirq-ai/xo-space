"""Decision models for storm's router (design ch 11 §3, §14.3; §0.81): Sage
and Jev behind one interface, keys only in ``router.env``, and the router's
decision from their answers (the PoC's tests, on storm's key store)."""

from __future__ import annotations

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
from services.cowork_agent.storm.chats import router
from services.decision_models import jev, keys, sage
from services.levanto import client as levanto

ROOT = Path(__file__).resolve().parents[1]
KEY = "lv_test_not_a_real_key"
QUESTION = {"id": "q", "kind": "yesno", "instructions": "Is this a question?"}


class _StateRoot(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": tmp.name})
        env.start()
        self.addCleanup(env.stop)


class KeyStoreTests(_StateRoot):
    def test_keys_live_in_router_env_owner_only_and_nowhere_else(self) -> None:
        keys.write("TYPESAFE_API_KEY", "a")
        keys.write("LEVANTO_API_KEY", "b")
        keys.write("TYPESAFE_API_KEY", "c")
        self.assertEqual(keys.path().read_text(), "LEVANTO_API_KEY=b\nTYPESAFE_API_KEY=c\n")
        self.assertEqual(oct(keys.path().stat().st_mode & 0o777), "0o600")
        self.assertEqual((keys.read("TYPESAFE_API_KEY"), keys.read("NOPE")), ("c", None))
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)
        keys.write("TYPESAFE_API_KEY", None)
        self.assertIsNone(keys.read("TYPESAFE_API_KEY"))

    def test_the_env_whitelist_keeps_every_router_key_from_the_agents(self) -> None:
        from services.cowork_agent.adapters.env_whitelist import decision_model_key_names
        keys.write("TYPESAFE_API_KEY", "a")
        self.assertIn("TYPESAFE_API_KEY", decision_model_key_names())


class LevantoTests(_StateRoot):
    def answer(self, handler) -> levanto.SageResult:
        real = httpx.AsyncClient
        self.requests: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recording)
            return real(*args, **kwargs)
        with mock.patch.object(levanto.httpx, "AsyncClient", factory):
            return asyncio.run(levanto.decide("content", QUESTION, timeout=5))

    def test_a_decision_and_its_failures(self) -> None:
        keys.write(levanto.ENV_API_KEY, KEY)
        result = self.answer(lambda r: httpx.Response(200, json={"id": "q", "result": {"answer": "yes"}}))
        self.assertTrue(result.ok)
        [request] = self.requests
        self.assertEqual((str(request.url), request.headers["authorization"]),
                         ("https://sage.levanto.ai/decide", f"Bearer {KEY}"))
        for status, kind in {401: "auth", 402: "balance", 400: "invalid", 429: "rate", 503: "server"}.items():
            with self.subTest(status=status):
                self.assertEqual(self.answer(lambda r, s=status: httpx.Response(s, json={"detail": "x"})).kind, kind)
        self.assertEqual(self.answer(lambda r: httpx.Response(402, text="nope")).detail, "Sage returned 402")
        self.assertEqual(self.answer(lambda r: httpx.Response(200, text="[]")).kind, "bad_response")

        def refused(request):
            raise httpx.ConnectError(f"refused {KEY}", request=request)
        failed = self.answer(refused)
        self.assertEqual(failed.kind, "offline")
        self.assertNotIn(KEY, repr(failed))

    def test_without_a_key_nothing_is_sent(self) -> None:
        result = self.answer(lambda r: httpx.Response(200, json={}))
        self.assertEqual((self.requests, result.kind), ([], "not_configured"))

    def test_only_the_client_names_the_host(self) -> None:
        offenders = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py")
                     if not set(p.relative_to(ROOT).parts) & {"venv", ".venv", "node_modules", ".git", "tests", "docs"}
                     and p.relative_to(ROOT).parts[:2] != ("services", "levanto")
                     and re.search(r"sage\.levanto\.ai", p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(offenders, [])


class ProviderTests(_StateRoot):
    def test_jev_asks_tags_as_one_noul_each(self) -> None:
        q = [dm.Choice("intent", "What?", {"a": "first", "b": "second"}),
             dm.Tags("projects", "Which?", {"calc": "a calculator", "site": "a website"})]
        body = jev.request_body("hello", q, "jev-2")
        self.assertEqual((body["model"], body["questions"]["projects__calc"]["type"]), ("jev-2", "noul"))
        answers = jev.parse(q, {"answers": {"intent": {"choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
                                            "projects__calc": {"noul": 0.93}, "projects__site": {"noul": 0.5}}})
        self.assertEqual((answers["intent"].chosen, answers["intent"].probability), ("b", 0.8))
        self.assertEqual(answers["projects"].applies, {"calc": True, "site": None})

    def test_jev_without_a_key_sends_nothing(self) -> None:
        with mock.patch.object(httpx.AsyncClient, "post") as post:
            result = asyncio.run(jev.ask("x", [dm.Choice("c", "?", {"a": "a"})], model="jev-latest", timeout=1))
        post.assert_not_called()
        self.assertEqual(result.answers["c"].error, "not_configured")

    def test_sage_keeps_no_pick_and_unsure_tags(self) -> None:
        async def fake(content, question, *, timeout, reasoning=None):
            if question["kind"] == "choice":
                return levanto.SageResult(ok=True, status=200, data={"result": {"chosen": None, "probabilities": [
                    {"option": "a", "probability": 0.51}]}, "meta": {"usage": {"billed_input_tokens": 300}}})
            return levanto.SageResult(ok=True, status=200, data={"result": {"tags": [
                {"id": "calc", "probability": 0.9, "applies": True}, {"id": "site", "probability": 0.4,
                                                                      "applies": None}]}})
        with mock.patch.object(levanto, "decide", fake):
            result = asyncio.run(sage.ask("x", [dm.Choice("c", "?", {"a": "A", "b": "B"}),
                                                dm.Tags("t", "?", {"calc": "c", "site": "s"})], model="sage",
                                          timeout=1))
        self.assertTrue(result.answers["c"].abstains(0.5))
        self.assertEqual(result.answers["t"].applies, {"calc": True, "site": None})


class DecideTests(unittest.TestCase):
    def result(self, **answers) -> dm.Result:
        return dm.Result("jev", answers, input_tokens=400)

    def test_the_project_level(self) -> None:
        new = router.decide("project", self.result(intent=dm.Answer("intent", "choice", "new_task", 0.9)), 0.6)
        self.assertEqual(new["actions"], [{"kind": "start_thread"}])
        follow = router.decide("project", self.result(
            intent=dm.Answer("intent", "choice", "follow_up", 0.9),
            thread=dm.Answer("thread", "choice", "cmsg_1", 0.8)), 0.6)
        self.assertEqual((follow["actions"], follow["confidence"]),
                         ([{"kind": "message_thread", "thread_id": "cmsg_1"}], 0.8))
        for answers, why in (({"intent": dm.Answer("intent", "choice", "new_task", 0.3)}, "unsure"),
                             ({"intent": dm.Answer("intent", "choice", "question", 0.9)}, "needs words"),
                             ({"intent": dm.Answer("intent", "choice", "follow_up", 0.9),
                               "thread": dm.Answer("thread", "choice", "none", 0.9)}, "which thread"),
                             ({"intent": dm.Answer("intent", "choice", error="timeout")}, "timeout")):
            with self.subTest(why):
                route = router.decide("project", self.result(**answers), 0.6)
                self.assertEqual(route["actions"], [])
                self.assertIn(why, route["reasons"][0])

    def test_the_workspace_level_forwards_to_every_project_that_clearly_applies(self) -> None:
        route = router.decide("workspace", self.result(projects=dm.Answer(
            "projects", "tags", probabilities={"calc": 0.9, "site": 0.85, "blog": 0.5},
            applies={"calc": True, "site": True, "blog": None})), 0.6)
        self.assertEqual(route["actions"], [{"kind": "forward", "project": "calc"},
                                            {"kind": "forward", "project": "site"}])
        empty = router.decide("workspace", self.result(projects=dm.Answer("projects", "tags", applies={})), 0.6)
        self.assertEqual(empty["actions"], [])

    def test_a_threads_title_is_the_first_sentence(self) -> None:
        self.assertEqual(router.title_from("Add a CSV export. It should stream."), "Add a CSV export")
        self.assertEqual(router.title_from("x" * 80), "x" * 60 + "…")
        # Never inside a word (live run 4): a file name or a version stays whole.
        self.assertEqual(router.title_from("Create a file todo.txt containing 'milk' and commit it."),
                         "Create a file todo.txt containing 'milk' and commit it")
        self.assertEqual(router.title_from("Bump to v1.2 now! Then deploy."), "Bump to v1.2 now")
        self.assertEqual(router.title_from("First line\nsecond line"), "First line")
        self.assertIn("follow-up", json.dumps([q.instructions for q in router.project_questions(
            [{"ts": "cmsg_1", "title": "Login", "state": "done"}])]))
