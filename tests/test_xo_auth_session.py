"""``GET /xo-auth/session/self``: the pass-through to xo-swarm-api's mint.

Minting moved to the swarm (``POST /auth/session/self``). This route no longer generates
an id; it presents this backend's XO credential, supplies the one thing the swarm cannot
know (this install's space id) and records what comes back so the next request can be
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
from services.cowork_agent.connectors.composio import session_identity, state

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

        env = patch.dict("os.environ", {state.SPACE_ENV: WORKSPACE})
        env.start()
        self.addCleanup(env.stop)

        # The route warms the identity cache after a successful mint. That is a second
        # round trip and never fatal; stub it so these tests only exercise the mint.
        warm = patch(
            "services.cowork_agent.connectors.composio.state.aidentity_payload",
            new=AsyncMock(return_value={"account_id": ACCOUNT,
                                        "space_id": WORKSPACE}),
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
                            "space_id": WORKSPACE, "expires_in": 3600})
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
        # Dual-sent until xo-swarm-api #41 is deployed: the deployed swarm declares
        # workspace_id, #41 declares space_id, and each ignores the other.
        self.assertEqual(
            kwargs["json"], {"workspace_id": WORKSPACE, "space_id": WORKSPACE},
        )

    async def test_xo_space_id_is_the_only_space_identity(self) -> None:
        # One flow on Coder and off: the id the swarm knows this Space by, the same
        # value project sharing and usage reporting send.
        self.assertEqual(state.SPACE_ENV, "XO_SPACE_ID")
        swarm, post = self._swarm(
            _response(200, {"session_id": MINTED, "account_id": ACCOUNT})
        )
        env = {"XO_SPACE_ID": "space-local"}
        with swarm, patch.dict("os.environ", env), \
                patch.object(composio_session, "get_auth_token", return_value="tok"):
            result = await composio_session.xo_auth_session_self()

        self.assertEqual(result["session_id"], MINTED)
        _, kwargs = post.call_args
        self.assertEqual(
            kwargs["json"], {"workspace_id": "space-local", "space_id": "space-local"},
        )

    async def test_coder_workspace_id_never_reaches_the_swarm(self) -> None:
        # XO_SPACE_ID is the identity everywhere. Coder's own id is not read, even when
        # the pod sets it, and under neither name.
        swarm, post = self._swarm(
            _response(200, {"session_id": MINTED, "account_id": ACCOUNT})
        )
        env = {"XO_SPACE_ID": "space-local", "CODER_WORKSPACE_ID": "coder-uuid-0001"}
        with swarm, patch.dict("os.environ", env), \
                patch.object(composio_session, "get_auth_token", return_value="tok"):
            await composio_session.xo_auth_session_self()

        _, kwargs = post.call_args
        self.assertEqual(set(kwargs["json"].values()), {"space-local"})
        self.assertNotIn("coder-uuid-0001", str(kwargs))

    @staticmethod
    def _swarm_reading(field: str):
        """A swarm whose mint model declares exactly one identity field.

        Pydantic ignores fields a model does not declare, so the body is accepted when
        `field` is in it and 422s naming `field` otherwise: the deployed swarm
        (workspace_id) and xo-swarm-api #41 (space_id) in turn.
        """
        def _post(url, headers=None, json=None):
            if field in (json or {}):
                return _response(200, {"session_id": MINTED, "account_id": ACCOUNT})
            return _response(422, {"detail": [{
                "type": "missing", "loc": ["body", field], "msg": "Field required",
            }]})

        client = SimpleNamespace(post=AsyncMock(side_effect=_post))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return patch.object(composio_session.httpx, "AsyncClient", return_value=ctx)

    async def test_the_dual_send_mints_on_a_swarm_that_reads_either_field(self) -> None:
        # Both shapes must work until xo-swarm-api #41 is deployed everywhere.
        for field in ("workspace_id", "space_id"):
            with self.subTest(swarm_reads=field):
                session_identity._SESSIONS.clear()
                with self._swarm_reading(field), \
                        patch.object(composio_session, "get_auth_token", return_value="tok"):
                    result = await composio_session.xo_auth_session_self()
                self.assertEqual(result["session_id"], MINTED)
                self.assertTrue(session_identity.is_valid(MINTED))

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
                    "services.cowork_agent.connectors.composio.state.aidentity_payload",
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

    async def test_no_space_id_is_a_401_and_never_an_account_wide_bucket(self) -> None:
        # Without XO_SPACE_ID the mint refuses.
        with patch.dict("os.environ", {state.SPACE_ENV: ""}):
            exc = await self._fails_with(401)
        self.assertIn(state.SPACE_ENV, exc.detail["error"])

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

    async def test_a_swarm_that_predates_space_id_is_a_503_naming_the_fix(self) -> None:
        # The pre-#41 validation error is a deploy gap and is reported as one: an
        # actionable message, not a raw pydantic dump, and never a sign-out.
        body = {"detail": [{"type": "missing", "loc": ["body", "space_id"],
                            "msg": "Field required", "input": {}}]}
        exc = await self._fails_with(503, response=_response(422, body))
        self.assertIn("xo-swarm-api #41", exc.detail["error"])
        self.assertIn("space_id", exc.detail["error"])
        # The upstream text is carried for the operator; the request headers never are.
        self.assertIn("Field required", exc.detail["upstream"])
        self.assertNotIn("Authorization", str(exc.detail))
        self.assertNotIn("Bearer", str(exc.detail))
        self.assertEqual(session_identity._SESSIONS, {})

    async def test_a_rejected_space_id_value_is_a_503_naming_the_variable(self) -> None:
        # The swarm's own validator refuses the value it read: a string detail, and the
        # only 422 a dual-sent body can draw from either swarm version. That is
        # XO_SPACE_ID being wrong on this install, so the diagnosis leads with the
        # variable and never sends the operator to deploy xo-swarm-api #41, which
        # would change nothing.
        body = {"detail": "workspace_id must contain only letters, digits, '-' and '_'"}
        exc = await self._fails_with(503, response=_response(422, body))
        self.assertIn(state.SPACE_ENV, exc.detail["error"])
        self.assertNotIn("#41", exc.detail["error"])
        self.assertIn("must contain only letters", exc.detail["upstream"])
        self.assertNotIn("Authorization", str(exc.detail))
        self.assertEqual(session_identity._SESSIONS, {})

    async def test_an_empty_session_id_is_refused_rather_than_handed_on(self) -> None:
        await self._fails_with(503, response=_response(200, {"session_id": ""}))
        self.assertEqual(session_identity._SESSIONS, {})

    async def test_an_unreadable_payload_is_refused(self) -> None:
        await self._fails_with(503, response=_response(200, text="not json"))


if __name__ == "__main__":
    unittest.main()
