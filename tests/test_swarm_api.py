from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from services import swarm_api
from services.swarm_api import _http, auth, chat, project_sharing, usage

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fake_response(status: int, body=None, text: str | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text if text is not None else ("" if body is None else "body")
    r.headers = {}
    if body is None:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = body
    return r


class ClientPatch:
    """Patch httpx.AsyncClient so `request()` sees our canned response (or
    raises) without any network."""

    def __init__(self, response=None, raises=None):
        self.calls = []
        self.response, self.raises = response, raises

    def __enter__(self):
        outer = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kw):
                outer.calls.append((method, url, kw))
                if outer.raises:
                    raise outer.raises
                return outer.response

        self._p = patch.object(_http.httpx, "AsyncClient", _Client)
        self._p.start()
        return self

    def __exit__(self, *a):
        self._p.stop()


class TransportTests(unittest.TestCase):
    def test_base_url_reads_env_and_strips_slash(self) -> None:
        with patch.dict(os.environ, {"CHAT_API_BASE_URL": "https://swarm.example/"}):
            self.assertEqual(_http.base_url(), "https://swarm.example")
        with patch.dict(os.environ, {"CHAT_API_BASE_URL": ""}):
            self.assertEqual(_http.base_url(), _http.DEFAULT_BASE_URL)

    def test_authenticated_request_is_not_sent_without_a_token(self) -> None:
        with patch.object(_http, "auth_token", return_value=None), ClientPatch() as c:
            res = run(_http.request("POST", "/commits/poll", json={}))
        self.assertFalse(res.ok)
        self.assertTrue(res.unauthenticated)
        self.assertEqual(c.calls, [])

    def test_bearer_header_and_url_composition(self) -> None:
        with patch.dict(os.environ, {"CHAT_API_BASE_URL": "https://swarm.example"}), \
             patch.object(_http, "auth_token", return_value="ak_x"), \
             ClientPatch(fake_response(200, {"repos": []})) as c:
            res = run(_http.request("POST", "/commits/poll", json={"a": 1}))
        self.assertTrue(res.ok)
        method, url, kw = c.calls[0]
        self.assertEqual((method, url), ("POST", "https://swarm.example/commits/poll"))
        self.assertEqual(kw["headers"], {"Authorization": "Bearer ak_x"})
        self.assertEqual(res.data, {"repos": []})

    def test_error_detail_is_extracted_from_the_body(self) -> None:
        with patch.object(_http, "auth_token", return_value="ak_x"), \
             ClientPatch(fake_response(403, {"detail": "this project is owned by another user"})):
            res = run(_http.request("POST", "/commits/share", json={}))
        self.assertEqual((res.ok, res.status, res.detail), (False, 403, "this project is owned by another user"))
        with patch.object(_http, "auth_token", return_value="ak_x"), \
             ClientPatch(fake_response(500, None, text="boom")):
            res = run(_http.request("GET", "/x"))
        self.assertEqual(res.detail, "swarm returned 500")
        self.assertEqual(res.text, "boom")

    def test_network_failure_is_offline_not_an_exception(self) -> None:
        with patch.object(_http, "auth_token", return_value="ak_x"), \
             ClientPatch(raises=httpx.ConnectError("refused")):
            res = run(_http.request("GET", "/x"))
        self.assertFalse(res.ok)
        self.assertTrue(res.offline)
        self.assertEqual(res.status, 0)

    def test_unauthenticated_handshake_sends_without_a_token(self) -> None:
        with patch.object(_http, "auth_token", return_value=None), \
             ClientPatch(fake_response(200, {"authorize_url": "u"})) as c:
            res = run(auth.browser_auth_start(None, None))
        self.assertTrue(res.ok)
        self.assertEqual(c.calls[0][2]["headers"], {})


class FeatureModuleTests(unittest.TestCase):
    def test_project_sharing_shapes(self) -> None:
        with patch.object(project_sharing, "request", new=AsyncMock(return_value=_http.SwarmResult(ok=True, status=200, data={"repos": []}))):
            self.assertEqual(run(project_sharing.poll("ws", {})), {"repos": []})
            self.assertTrue(run(project_sharing.report_commits("r", "ws", ["a" * 40])))
            self.assertTrue(run(project_sharing.report_commits("r", "ws", [])))
        with patch.object(project_sharing, "request", new=AsyncMock(return_value=_http.SwarmResult(ok=False, status=403, detail="not the owner"))):
            self.assertEqual(run(project_sharing.share("r", "a", "b")), (False, 403, "not the owner"))
            self.assertEqual(run(project_sharing.revoke("r", "b")), (False, 403, "not the owner"))
            self.assertEqual(run(project_sharing.members("r")), (False, 403, "not the owner"))
            self.assertIsNone(run(project_sharing.poll("ws", {})))

    def test_usage_probe_and_report_hit_the_same_path(self) -> None:
        with patch.object(usage, "request", new=AsyncMock(return_value=_http.SwarmResult(ok=True, status=200, data={"upserted": 1}))) as req:
            run(usage.probe_key())
            run(usage.report([{"x": 1}]))
        paths = [c.args[1] for c in req.await_args_list]
        self.assertEqual(paths, ["/usage/report", "/usage/report"])
        self.assertEqual(req.await_args_list[0].kwargs["json"], {"records": []})

    def test_chat_client_returns_none_on_failure(self) -> None:
        with patch.object(chat, "request", new=AsyncMock(return_value=_http.SwarmResult(ok=False, status=500, text="x", detail="swarm returned 500"))):
            self.assertIsNone(run(chat.ChatAPIClient().push_message("p", "u", "m")))
            self.assertEqual(run(chat.ChatAPIClient().get_message_count("p")), 0)


class OneDoorTests(unittest.TestCase):
    """Architecture guard: nothing outside services/swarm_api may read the
    swarm base URL or build a swarm request. If this fails, the new call
    belongs in a swarm_api module."""

    SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "tests", "tests2", "docs", ".claude"}

    def _py_files(self):
        for p in ROOT.rglob("*.py"):
            if any(part in self.SKIP_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if "swarm_api" in p.parts:
                continue
            yield p

    def test_only_swarm_api_reads_the_base_url(self) -> None:
        offenders = [str(p.relative_to(ROOT)) for p in self._py_files()
                     if re.search(r"CHAT_API_BASE_URL", p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(offenders, [], "read the swarm base URL via services.swarm_api.base_url()")

    def test_only_swarm_api_names_swarm_paths(self) -> None:
        swarm_paths = ("/commits/poll", "/commits/share", "/usage/report", "/auth/browser/", "/chat/add_message", "/get-user-id")
        offenders = []
        for p in self._py_files():
            txt = p.read_text(encoding="utf-8", errors="replace")
            for sp in swarm_paths:
                if ("\"" + sp) in txt or ("'" + sp) in txt:
                    offenders.append(f"{p.relative_to(ROOT)} ({sp})")
        self.assertEqual(offenders, [])

    def test_public_surface(self) -> None:
        for name in ("SwarmResult", "base_url", "auth_token", "auth_headers", "request"):
            self.assertTrue(hasattr(swarm_api, name))
