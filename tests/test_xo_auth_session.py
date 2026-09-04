"""``GET /xo-auth/session/self`` — the pass-through to xo-swarm-api's mint.

Minting moved to the swarm (``POST /auth/session/self``). This route no longer generates
an id; it presents this backend's XO credential, supplies the one thing the swarm cannot
know — this pod's workspace id — and records what comes back so the next request can be
checked locally.

Hermetic: httpx is never allowed to leave the process, and the credential is patched, so
nothing here reaches a real swarm.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from routers.cowork_agent.connectors import composio_session
from services import tenancy
from services.cowork_agent.connectors.composio import session_identity

WORKSPACE = "ws-1234"
ACCOUNT = "user_abc"
MINTED = "s" * 43


def _response(status: int, payload: dict | None = None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", "https://swarm.example/auth/session/self")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text, request=request)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_identity._SESSIONS.clear()
        self.addCleanup(session_identity._SESSIONS.clear)

        env = patch.dict("os.environ", {tenancy.WORKSPACE_ENV: WORKSPACE})
        env.start()
        self.addCleanup(env.stop)

        # The route warms the principal cache after a successful mint. That is a second
        # round trip and never fatal; stub it so these tests only exercise the mint.
        warm = patch(
            "services.cowork_agent.connectors.composio.state.aprincipal_payload",
            new=AsyncMock(return_value={"principal": f"{ACCOUNT}__ws__{WORKSPACE}"}),
        )
        warm.start()
        self.addCleanup(warm.stop)

    @staticmethod
    def _swarm(response: httpx.Response | Exception):
        """Patch the one AsyncClient this route uses."""
        post = AsyncMock(
            side_effect=response if isinstance(response, Exception) else None,
            return_value=None if isinstance(response, Exception) else response,
        )
        client = SimpleNamespace(post=post)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return patch.object(composio_session.httpx, "AsyncClient", return_value=ctx), post


class MintTests(_Base):
    async def test_the_swarm_s_id_is_returned_and_recorded_locally(self) -> None:
        swarm, post = self._swarm(
            _response(200, {"session_id": MINTED, "account_id": ACCOUNT,
                            "workspace_id": WORKSPACE, "expires_in": 3600})
        )
        with swarm, patch.object(composio_session, "get_auth_token", return_value="tok"):
            result = await composio_session.xo_auth_session_self()

        self.assertEqual(result["session_id"], MINTED)
        self.assertEqual(result["user_id"], ACCOUNT)
        # Recorded, so the MCP hot path checks it without a round trip.
        self.assertTrue(session_identity.is_valid(MINTED))

    async def test_it_presents_the_credential_and_supplies_the_workspace(self) -> None:
        swarm, post = self._swarm(
            _response(200, {"session_id": MINTED, "account_id": ACCOUNT})
        )
        with swarm, patch.object(composio_session, "get_auth_token", return_value="tok"):
            await composio_session.xo_auth_session_self()

        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual(kwargs["json"], {"workspace_id": WORKSPACE})

    async def test_the_tenant_key_never_reaches_the_browser(self) -> None:
        swarm, _ = self._swarm(
            _response(200, {"session_id": MINTED, "account_id": ACCOUNT,
                            "principal": "leaked__ws__key"})
        )
        with swarm, patch.object(composio_session, "get_auth_token", return_value="tok"):
            result = await composio_session.xo_auth_session_self()

        self.assertNotIn("principal", result)
        self.assertNotIn("leaked__ws__key", str(result))

    async def test_a_warm_up_failure_does_not_fail_the_mint(self) -> None:
        # The mint already proved the credential and the workspace. Warming the
        # principal cache is a convenience, so its failure must not sign the user out.
        swarm, _ = self._swarm(_response(200, {"session_id": MINTED, "account_id": ACCOUNT}))
        with swarm, \
                patch.object(composio_session, "get_auth_token", return_value="tok"), \
                patch(
                    "services.cowork_agent.connectors.composio.state.aprincipal_payload",
                    new=AsyncMock(side_effect=RuntimeError("swarm hiccup")),
                ):
            result = await composio_session.xo_auth_session_self()
        self.assertEqual(result["session_id"], MINTED)


class RefusalTests(_Base):
    async def _fails_with(self, status: int, **overrides) -> HTTPException:
        response = overrides.pop("response", _response(200, {"session_id": MINTED}))
        token = overrides.pop("token", "tok")
        swarm, _ = self._swarm(response)
        with swarm, patch.object(composio_session, "get_auth_token", return_value=token):
            with self.assertRaises(HTTPException) as raised:
                await composio_session.xo_auth_session_self()
        self.assertEqual(raised.exception.status_code, status)
        return raised.exception

    async def test_no_credential_is_a_401_before_any_call(self) -> None:
        exc = await self._fails_with(401, token=None)
        self.assertIn("XO_API_KEY", exc.detail["error"])

    async def test_no_workspace_is_a_401_and_never_an_account_wide_bucket(self) -> None:
        with patch.dict("os.environ", {tenancy.WORKSPACE_ENV: ""}):
            exc = await self._fails_with(401)
        self.assertIn(tenancy.WORKSPACE_ENV, exc.detail["error"])

    async def test_a_rejected_credential_is_a_401_not_a_503(self) -> None:
        # Authoritative: XO said no. Sending the user to sign in is the right advice.
        for status in (401, 403):
            with self.subTest(status=status):
                await self._fails_with(401, response=_response(status, text="nope"))

    async def test_an_unreachable_swarm_is_a_503_not_a_401(self) -> None:
        # Transient. A 401 here would tell the user to sign in, which fixes nothing.
        exc = await self._fails_with(503, response=httpx.ConnectError("down"))
        self.assertIn("could not be reached", exc.detail["error"])

    async def test_a_swarm_without_the_route_is_a_deploy_gap_not_a_sign_out(self) -> None:
        exc = await self._fails_with(503, response=_response(404, text=""))
        self.assertIn("Deploy the swarm", exc.detail["error"])

    async def test_an_empty_session_id_is_refused_rather_than_handed_on(self) -> None:
        await self._fails_with(503, response=_response(200, {"session_id": ""}))
        self.assertEqual(session_identity._SESSIONS, {})

    async def test_an_unreadable_payload_is_refused(self) -> None:
        await self._fails_with(503, response=_response(200, text="not json"))


if __name__ == "__main__":
    unittest.main()
