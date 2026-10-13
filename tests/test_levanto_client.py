"""``services/levanto/client.py`` is the one door to Levanto Sage.

It sends nothing without ``LEVANTO_API_KEY``, never raises, sorts every
failure into one word (``balance`` for the 402 an empty account returns), and
is the only module that reads the key or names the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from services.levanto import client

ROOT = Path(__file__).resolve().parents[1]
KEY = "lv_test_not_a_real_key"
QUESTION = {"id": "q", "kind": "yesno", "instructions": "Is this a question?"}


class _Sage(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {client.ENV_API_KEY: KEY})
        env.start()
        self.addCleanup(env.stop)
        self.requests: list[httpx.Request] = []

    def answer(self, handler) -> client.SageResult:
        real = httpx.AsyncClient

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recording)
            return real(*args, **kwargs)

        with patch.object(client.httpx, "AsyncClient", factory):
            return asyncio.run(client.decide("content", QUESTION, timeout=5))


class DecideTests(_Sage):
    def test_a_decision(self) -> None:
        result = self.answer(lambda r: httpx.Response(200, json={"id": "q", "result": {"answer": "yes"}}))
        self.assertTrue(result.ok)
        self.assertIsNone(result.kind)
        self.assertEqual(result.data["result"]["answer"], "yes")
        [request] = self.requests
        self.assertEqual(str(request.url), "https://sage.levanto.ai/decide")
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(request.headers["user-agent"], client.USER_AGENT)
        self.assertEqual(json.loads(request.content), {"content": "content", "question": QUESTION})

    def test_without_a_key_nothing_is_sent(self) -> None:
        with patch.dict(os.environ, {client.ENV_API_KEY: "  "}):
            result = self.answer(lambda r: httpx.Response(200, json={}))
            self.assertFalse(client.configured())
        self.assertEqual(self.requests, [])
        self.assertEqual(result.kind, "not_configured")

    def test_http_failures_are_sorted(self) -> None:
        cases = {
            401: "auth", 402: "balance", 400: "invalid", 429: "rate", 503: "server", 404: "http",
        }
        for status, kind in cases.items():
            with self.subTest(status=status):
                result = self.answer(lambda r, s=status: httpx.Response(s, json={"detail": f"said {s}"}))
                self.assertFalse(result.ok)
                self.assertEqual((result.status, result.kind, result.detail), (status, kind, f"said {status}"))

    def test_an_error_without_a_detail(self) -> None:
        result = self.answer(lambda r: httpx.Response(402, text="nope"))
        self.assertEqual(result.detail, "Sage returned 402")

    def test_a_timeout(self) -> None:
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)
        result = self.answer(slow)
        self.assertEqual(result.kind, "timeout")
        self.assertTrue(result.timed_out)

    def test_unreachable(self) -> None:
        def refused(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertEqual(self.answer(refused).kind, "offline")

    def test_a_success_that_is_not_an_object(self) -> None:
        self.assertEqual(self.answer(lambda r: httpx.Response(200, text="[]")).kind, "bad_response")
        self.assertEqual(self.answer(lambda r: httpx.Response(200, text="<html>")).kind, "bad_response")

    def test_the_key_never_lands_in_a_result(self) -> None:
        for handler in (
            lambda r: httpx.Response(402, json={"detail": "balance too low"}),
            lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"refused {KEY}", request=r)),
        ):
            result = self.answer(handler)
            self.assertNotIn(KEY, repr(result))


class OneDoorTests(unittest.TestCase):
    SKIP = {"venv", ".venv", "node_modules", ".git", "tests", "docs"}

    def test_only_the_client_reads_the_key_or_names_the_host(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in self.SKIP or rel.parts[:2] == ("services", "levanto"):
                continue
            if re.search(r"LEVANTO_API_KEY|sage\.levanto\.ai", path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(rel.as_posix())
        self.assertEqual(offenders, [], "reach Levanto through services/levanto/client.py")


if __name__ == "__main__":
    unittest.main()
