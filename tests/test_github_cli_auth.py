"""
GitHub connector — `gh auth login` device flow.

Pins the behaviours a browser client relies on when it polls the login:

  - Polling is idempotent. A completed or failed login answers the same way
    on every poll until it expires or is cancelled. Consuming the result on
    first read meant any retried poll (lost response, proxy hiccup, tab
    throttling) got 404 "expired" while the token had in fact been saved.
  - A failed login is a *result* of the poll, not a transport failure, so
    the route answers 200 with ``status: failed`` — a 502 makes the client
    retry, and the retry then found no session.
  - Starting a login never logs the workspace out of `gh`. That logout
    silently broke `gh auth git-credential` for every repo while the stored
    token still reported "connected".

Hermetic: no real `gh`, git, or network — subprocess spawning and the shared
GitHub helpers are patched at the module seam.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.connectors import github_cli as github_cli_router
from services.cowork_agent.connectors.github import cli_auth

VALID = {
    "valid": True,
    "status": "connected",
    "username": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://example.invalid/a.png",
    "scopes": "repo",
    "user_id": 1,
    "email": "",
}


class _FakeProc:
    """Stand-in for an asyncio subprocess whose stdout we script."""

    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line)
        self.returncode: int | None = None
        self.killed = False

    def finish(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout.feed_eof()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)


class _CliAuthTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._active = mock.patch.object(cli_auth, "_active", {})
        self._active.start()
        # A fresh lock per test: asyncio.Lock binds to the first loop it waits on.
        self._lock = mock.patch.object(cli_auth, "_lock", asyncio.Lock())
        self._lock.start()

    async def asyncTearDown(self) -> None:
        self._lock.stop()
        self._active.stop()

    def _seed(self, status: str, *, token: str | None = None, error: str | None = None) -> str:
        proc = _FakeProc([])
        proc.finish(0 if status == "completed" else 1)
        session = cli_auth._Session(
            session_id="sid-1", process=proc, user_code="ABCD-1234"
        )
        session.status = status
        session.token = token
        session.error = error
        cli_auth._active[session.session_id] = session
        return session.session_id

    def _patch_finalizers(self):
        validate = mock.patch.object(
            cli_auth, "validate_token", mock.AsyncMock(return_value=dict(VALID))
        )
        save = mock.patch.object(cli_auth, "save_github_token")
        identity = mock.patch.object(
            cli_auth, "configure_git_identity", mock.AsyncMock()
        )
        return validate, save, identity


class ConnectIdempotencyTests(_CliAuthTestCase):
    async def test_connect_answers_connected_on_every_poll_after_success(self) -> None:
        sid = self._seed("completed", token="gho_abc")
        validate, save, identity = self._patch_finalizers()
        with validate, save as save_mock, identity:
            first = await cli_auth.connect(sid)
            second = await cli_auth.connect(sid)

        self.assertTrue(first["ok"])
        self.assertEqual(first["payload"]["status"], "connected")
        self.assertEqual(first["payload"]["username"], "octocat")
        self.assertEqual(second, first, "a retried poll must see the same answer")
        save_mock.assert_called_once_with("gho_abc", auth_method="cli")

    async def test_connect_reports_failure_on_every_poll(self) -> None:
        sid = self._seed("failed", error="`gh auth login` exited with status 1.")
        first = await cli_auth.connect(sid)
        second = await cli_auth.connect(sid)

        self.assertEqual(first["status"], "failed")
        self.assertIn("exited with status 1", first["error"])
        self.assertEqual(second, first, "a retried poll must not turn into not_found")

    async def test_concurrent_polls_finalize_once(self) -> None:
        sid = self._seed("completed", token="gho_abc")
        validate, save, identity = self._patch_finalizers()
        with validate as validate_mock, save as save_mock, identity:
            results = await asyncio.gather(cli_auth.connect(sid), cli_auth.connect(sid))

        self.assertTrue(all(r["ok"] for r in results))
        validate_mock.assert_awaited_once()
        save_mock.assert_called_once()

    async def test_unknown_session_is_not_found(self) -> None:
        result = await cli_auth.connect("nope")
        self.assertEqual(result, {"ok": False, "status": "not_found"})

    async def test_pending_session_stays_pending(self) -> None:
        sid = self._seed("pending")
        result = await cli_auth.connect(sid)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["user_code"], "ABCD-1234")


class StartLoginTests(_CliAuthTestCase):
    async def test_start_login_does_not_log_out_the_existing_gh_session(self) -> None:
        spawned: list[tuple[str, ...]] = []
        proc = _FakeProc([
            b"! First copy your one-time code: 7B79-D4F8\n",
            b"Open this URL to continue in your web browser: https://github.com/login/device\n",
        ])

        async def fake_exec(*argv, **_kwargs):
            spawned.append(tuple(argv))
            return proc

        with mock.patch.object(cli_auth.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(cli_auth.asyncio, "create_subprocess_exec", fake_exec):
            info = await cli_auth.start_login()
            proc.finish(1)
            await cli_auth._active[info["session_id"]].drain_task

        self.assertEqual(info["user_code"], "7B79-D4F8")
        self.assertEqual([a[:3] for a in spawned], [("gh", "auth", "login")])


class PollRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(github_cli_router.router)
        self.client = TestClient(app)

    def _poll(self, result: dict):
        with mock.patch.object(
            github_cli_router.github_cli_auth, "connect", mock.AsyncMock(return_value=result)
        ):
            return self.client.post(
                "/api/connectors/github/cli/poll", json={"session_id": "sid-1"}
            )

    def test_failed_login_is_a_200_result_not_a_gateway_error(self) -> None:
        resp = self._poll({"ok": False, "status": "failed", "error": "boom"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "failed", "error": "boom"})

    def test_unknown_session_is_404(self) -> None:
        resp = self._poll({"ok": False, "status": "not_found"})
        self.assertEqual(resp.status_code, 404)

    def test_connected_returns_the_payload(self) -> None:
        payload = {"status": "connected", "auth_method": "cli", "username": "octocat"}
        resp = self._poll({"ok": True, "payload": payload})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), payload)


if __name__ == "__main__":
    unittest.main()
