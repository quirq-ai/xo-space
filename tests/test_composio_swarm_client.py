"""Unit tests for swarm_client.py — the HTTP transport to xo-swarm-api's Composio routes.

Mirrors the retired credentials.py's CredentialsTests in style: `_send` is the one seam
patched (mimicking how those tests patched `credentials._get`), so nothing here touches
the network or Clerk. The point of this file is the classification rule everything else
in service.py relies on: an authoritative failure (no key on xo-swarm-api, or this
backend's XO credential rejected) always carries the literal string "COMPOSIO_API_KEY"
and is never confused with a merely transient one.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

from services.cowork_agent.connectors.composio import swarm_client


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    if payload is None:
        return httpx.Response(status, text="")
    return httpx.Response(status, json=payload)


class _SwarmClientBase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CHAT_API_BASE_URL"] = "https://swarm.test"

    def tearDown(self) -> None:
        os.environ.pop("CHAT_API_BASE_URL", None)


class TransportTests(_SwarmClientBase):
    def test_the_bearer_token_and_url_are_correct(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(200, {"connections": []})) as send:
            swarm_client.list_connections()

        method, url, headers = send.call_args[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://swarm.test/connectors/composio/connections")
        self.assertEqual(headers, {"Authorization": "Bearer tok"})

    def test_query_params_are_forwarded(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(200, {"connections": []})) as send:
            swarm_client.list_connections(statuses=["ACTIVE"], toolkit_slugs=["gmail"])

        self.assertEqual(
            send.call_args.kwargs["params"],
            {"statuses": ["ACTIVE"], "toolkit_slugs": ["gmail"]},
        )

    def test_json_body_is_forwarded(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(
                    swarm_client, "_send",
                    return_value=_response(200, {"auth_url": "u", "connection_request_id": "r", "alias": None}),
                ) as send:
            swarm_client.connect(
                "gmail", auth_scheme="OAUTH2", redirect_uri="https://cb.test",
                alias=None, allow_multiple=False,
            )

        self.assertEqual(send.call_args.kwargs["json"], {
            "auth_scheme": "OAUTH2",
            "redirect_uri": "https://cb.test",
            "alias": None,
            "allow_multiple": False,
        })

    def test_no_xo_credential_is_reported_against_the_key_name(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value=None):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                swarm_client.list_connections()
        self.assertTrue(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))


class ClassificationTests(_SwarmClientBase):
    def _call(self):
        return swarm_client.list_connections()

    def test_a_503_is_authoritative(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(503, {"detail": "no key"})):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertTrue(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_401_is_authoritative(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(401)):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertTrue(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_403_is_authoritative(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(403)):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertTrue(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_a_404_is_not_found_and_not_authoritative(self) -> None:
        # Ownership/not-found is a per-resource outcome, never confused with "Composio
        # is not configured" — disconnect()/set_alias() key their behavior on this.
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(404, {"detail": "no such account"})):
            with self.assertRaises(swarm_client.SwarmComposioNotFound) as raised:
                self._call()
        self.assertFalse(raised.exception.authoritative)
        self.assertIn("no such account", str(raised.exception))

    def test_a_generic_5xx_is_transient(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(502, {"detail": "upstream hiccup"})):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertFalse(raised.exception.authoritative)
        self.assertIn("upstream hiccup", str(raised.exception))

    def test_an_unreachable_swarm_is_transient_but_still_names_the_key(self) -> None:
        # Matches the retired credentials.py's identical choice: a network failure is
        # not "authoritative" (nothing answered "not configured"), but the message
        # still says COMPOSIO_API_KEY because that's what the caller was trying to use.
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", side_effect=httpx.ConnectError("refused")):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertFalse(raised.exception.authoritative)
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))

    def test_an_unreadable_body_is_transient(self) -> None:
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=httpx.Response(200, text="not json")):
            with self.assertRaises(swarm_client.SwarmComposioError) as raised:
                self._call()
        self.assertFalse(raised.exception.authoritative)

    def test_a_200_returns_the_parsed_body(self) -> None:
        rows = [{"toolkit": "GMAIL", "connected_account_id": "ca_1", "status": "ACTIVE",
                 "scheme": "OAUTH2", "alias": None, "created_at": None, "is_disabled": False}]
        with patch("routers.auth.auth.get_auth_token", return_value="tok"), \
                patch.object(swarm_client, "_send", return_value=_response(200, {"connections": rows})):
            self.assertEqual(self._call(), rows)


class NotFoundIsASwarmComposioErrorTests(_SwarmClientBase):
    def test_not_found_is_a_subclass_callers_can_catch_broadly_or_narrowly(self) -> None:
        self.assertTrue(issubclass(swarm_client.SwarmComposioNotFound, swarm_client.SwarmComposioError))
        self.assertTrue(issubclass(swarm_client.SwarmComposioError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
