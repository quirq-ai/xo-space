"""Decision models for storm's router (design ch 11 §3, §14.3; §0.81): Sage
(``decision_models/levanto``) and Jev (``decision_models/jev``) behind one
interface, keys only in ``router.env``, and the router's decision from their
answers (the PoC's tests, on storm's key store)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import services.decision_models as dm
from services.cowork_agent.storm.chats import router
from services.decision_models import jev, keys, levanto, transport
from services.decision_models.jev import client as jev_client
from services.decision_models.levanto import client as levanto_client

ROOT = Path(__file__).resolve().parents[1]
KEY = "lv_test_not_a_real_key"
QUESTION = {"id": "q", "kind": "yesno", "instructions": "Is this a question?"}
JEV_BODY = {"state": "content", "model": "jev-latest", "questions": {}}


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


class ClientTests(_StateRoot):
    """Both clients make the one call in ``transport.py``; each keeps only its
    URL, key name and error statuses."""

    SAGE = (lambda: levanto_client.decide("content", QUESTION, timeout=5), "https://sage.levanto.ai/decide",
            {"content": "content", "question": QUESTION}, levanto_client.ENV_API_KEY,
            {400: "invalid", 401: "auth", 402: "balance", 422: "invalid", 429: "rate", 503: "server",
             529: "server", 404: "http"})
    JEV = (lambda: jev_client.systemone(JEV_BODY, timeout=5), "https://api.typesafe.ai/v1/systemone",
           JEV_BODY, jev_client.ENV_API_KEY,
           {400: "http", 401: "auth", 402: "http", 422: "invalid", 429: "rate", 503: "server",
            529: "overloaded", 404: "http"})

    def answer(self, call, handler) -> transport.Reply:
        real = httpx.AsyncClient
        self.requests: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recording)
            return real(*args, **kwargs)
        with mock.patch.object(transport.httpx, "AsyncClient", factory):
            return asyncio.run(call())

    def test_each_client_sends_its_request_and_names_its_failures(self) -> None:
        for name, (call, url, body, env_key, kinds) in {"sage": self.SAGE, "jev": self.JEV}.items():
            with self.subTest(provider=name):
                keys.write(env_key, KEY)
                reply = self.answer(call, lambda r: httpx.Response(200, json={"answers": {}}))
                self.assertEqual((reply.ok, reply.kind, reply.data), (True, None, {"answers": {}}))
                [request] = self.requests
                self.assertEqual((str(request.url), request.headers["authorization"], request.headers["user-agent"],
                                  json.loads(request.content)), (url, f"Bearer {KEY}", "xo-space", body))
                for status, kind in kinds.items():
                    self.assertEqual(self.answer(call, lambda r, s=status: httpx.Response(s, json={})).kind, kind,
                                     f"HTTP {status}")

    def test_transport_failures_are_replies(self) -> None:
        call = self.SAGE[0]
        keys.write(levanto_client.ENV_API_KEY, KEY)

        def refused(request):
            raise httpx.ConnectError(f"refused {KEY}", request=request)

        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)
        for handler, kind in ((refused, "offline"), (slow, "timeout"),
                              (lambda r: httpx.Response(200, text="[]"), "bad_response"),
                              (lambda r: httpx.Response(200, text="not json"), "bad_response")):
            reply = self.answer(call, handler)
            self.assertEqual((reply.kind, reply.data), (kind, None))
            self.assertNotIn(KEY, repr(reply))

    def test_a_bug_is_not_disguised_as_offline(self) -> None:
        keys.write(levanto_client.ENV_API_KEY, KEY)

        def broken(request):
            raise RuntimeError("a bug")
        with self.assertRaises(RuntimeError):
            self.answer(self.SAGE[0], broken)

    def test_without_a_key_nothing_is_sent(self) -> None:
        for call, *_ in (self.SAGE, self.JEV):
            reply = self.answer(call, lambda r: httpx.Response(200, json={}))
            self.assertEqual((self.requests, reply.kind), ([], "not_configured"))


class ProviderTests(_StateRoot):
    def test_each_providers_host_is_named_only_by_its_client(self) -> None:
        tracked = subprocess.run(["git", "ls-files", "-z", "--", "*.py", ":!tests/"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60, check=True).stdout
        sources = {path: (ROOT / path).read_text(encoding="utf-8", errors="replace")
                   for path in tracked.split("\0") if path}
        self.assertIn("services/decision_models/__init__.py", sources)
        for host, client in ((r"sage\.levanto\.ai", "services/decision_models/levanto/client.py"),
                             (r"api\.typesafe\.ai", "services/decision_models/jev/client.py")):
            with self.subTest(host=host):
                self.assertEqual([path for path, text in sources.items() if re.search(host, text)], [client])

    def test_every_provider_is_registered_under_its_public_id(self) -> None:
        self.assertEqual(list(dm.PROVIDERS), ["sage", "jev"])
        for provider_id, provider in dm.PROVIDERS.items():
            self.assertIsInstance(provider, dm.Provider)
            self.assertEqual(provider.id, provider_id)
        self.assertEqual((dm.provider("sage").env_key, dm.provider("jev").env_key),
                         ("LEVANTO_API_KEY", "TYPESAFE_API_KEY"))
        with self.assertRaises(ValueError):
            dm.provider("nope")

    def test_ask_dispatches_to_the_named_provider(self) -> None:
        q = [dm.Choice("c", "?", {"a": "a"})]
        with mock.patch.object(httpx.AsyncClient, "post") as post:
            results = [asyncio.run(dm.ask(p, "x", q, model="m", timeout=1)) for p in ("sage", "jev")]
        post.assert_not_called()
        self.assertEqual([(r.provider, r.answers["c"].error) for r in results],
                         [("sage", "not_configured"), ("jev", "not_configured")])

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
        async def fake(content, question, *, timeout):
            if question["kind"] == "choice":
                return transport.Reply(data={"result": {"chosen": None, "probabilities": [
                    {"option": "a", "probability": 0.51}]}, "meta": {"usage": {"billed_input_tokens": 300}}})
            return transport.Reply(data={"result": {"tags": [
                {"id": "calc", "probability": 0.9, "applies": True}, {"id": "site", "probability": 0.4,
                                                                      "applies": None}]}})
        with mock.patch.object(levanto_client, "decide", fake):
            result = asyncio.run(levanto.ask("x", [dm.Choice("c", "?", {"a": "A", "b": "B"}),
                                                dm.Tags("t", "?", {"calc": "c", "site": "s"})], model="sage",
                                          timeout=1))
        self.assertTrue(result.answers["c"].abstains(0.5))
        self.assertEqual(result.answers["t"].applies, {"calc": True, "site": None})


class DecideTests(unittest.TestCase):
    def result(self, **answers) -> dm.Result:
        return dm.Result("jev", answers, input_tokens=400)

    def test_the_project_level(self) -> None:
        new = router.decide("project", self.result(intent=dm.Answer("intent", "choice", "new_task", 0.9)), 0.6,
                            catalog=None)
        self.assertEqual(new["actions"], [{"kind": "start_thread", "unchosen": router.NOT_ASKED}])
        follow = router.decide("project", self.result(
            intent=dm.Answer("intent", "choice", "follow_up", 0.9),
            thread=dm.Answer("thread", "choice", "cmsg_1", 0.8)), 0.6, catalog=None)
        self.assertEqual((follow["actions"], follow["confidence"]),
                         ([{"kind": "message_thread", "thread_id": "cmsg_1"}], 0.8))
        for answers, why in (({"intent": dm.Answer("intent", "choice", "new_task", 0.3)}, "unsure"),
                             ({"intent": dm.Answer("intent", "choice", "question", 0.9)}, "needs words"),
                             ({"intent": dm.Answer("intent", "choice", "follow_up", 0.9),
                               "thread": dm.Answer("thread", "choice", "none", 0.9)}, "which thread"),
                             ({"intent": dm.Answer("intent", "choice", error="timeout")}, "timeout")):
            with self.subTest(why):
                route = router.decide("project", self.result(**answers), 0.6, catalog=None)
                self.assertEqual(route["actions"], [])
                self.assertIn(why, route["reasons"][0])

    def test_the_workspace_level_forwards_to_every_project_that_clearly_applies(self) -> None:
        route = router.decide("workspace", self.result(projects=dm.Answer(
            "projects", "tags", probabilities={"calc": 0.9, "site": 0.85, "blog": 0.5},
            applies={"calc": True, "site": True, "blog": None})), 0.6, catalog=None)
        self.assertEqual(route["actions"], [{"kind": "forward", "project": "calc"},
                                            {"kind": "forward", "project": "site"}])
        empty = router.decide("workspace", self.result(projects=dm.Answer("projects", "tags", applies={})), 0.6,
                              catalog=None)
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
