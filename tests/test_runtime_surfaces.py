"""Every runtime's surfaces on every Space: remote control, usage, composed oauth and the agents set up at boot."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import runtime_scope
from routers.cowork_agent import remote_control, usage
from routers.status import providers
from services import xo_manifest
from services.advisory import RuntimeFacts
from services.cowork_agent.adapters.claude_code import remote_control as claude_rc
from services.cowork_agent.adapters.codex import remote_control as codex_rc, usage as codex_usage
from services.cowork_agent.registry import agent_registry as reg

SPACE_RC = "/api/remote-control"
UNKNOWN = {"detail": {"code": "unknown_runtime",
                      "message": "No storm runtime is named 'nope'; GET /api/chat/runtimes lists them."}}


def _installed(*names: str):
    """Only ``names``' CLIs count as installed for the per-runtime routes."""
    binaries = {reg.runtime(name).binary for name in names}
    return mock.patch.object(runtime_scope, "installed", side_effect=binaries.__contains__)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(remote_control.router)
    app.include_router(usage.router)
    return TestClient(app)


class RemoteControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def on_space(self, agent: str):
        return mock.patch.object(remote_control, "space_agent", return_value=agent)

    def test_the_space_route_calls_the_space_agents_module_per_request(self) -> None:
        with mock.patch.object(codex_rc, "status", mock.AsyncMock(return_value={"agent": "codex"})), \
                mock.patch.object(claude_rc, "status", return_value={"running": False}):
            with self.on_space("codex"):
                self.assertEqual(self.client.get(f"{SPACE_RC}/status").json(), {"agent": "codex"})
            with self.on_space("claude_code"):
                self.assertEqual(self.client.get(f"{SPACE_RC}/status").json(), {"running": False})

    def test_start_passes_the_name_with_and_without_a_body(self) -> None:
        with self.on_space("codex"), mock.patch.object(codex_rc, "start", mock.AsyncMock(return_value={})) as start:
            self.client.post(f"{SPACE_RC}/start", json={"name": "mac"})
            self.client.post(f"{SPACE_RC}/start")
        self.assertEqual(start.await_args_list, [mock.call(name="mac"), mock.call(name=None)])

    def test_pair_status_passes_the_body(self) -> None:
        with self.on_space("codex"), \
                mock.patch.object(codex_rc, "pairing_status", mock.AsyncMock(return_value={"claimed": True})) as fn:
            response = self.client.post(f"{SPACE_RC}/pair/status", json={"manual_pairing_code": "ABCD-EFGH"})
        self.assertEqual(response.json(), {"claimed": True})
        fn.assert_awaited_once_with(pairing_code=None, manual_pairing_code="ABCD-EFGH")

    def test_what_the_space_agent_lacks_answers_todays_404(self) -> None:
        not_found = (404, {"detail": "Not Found"})
        with self.on_space("claude_code"):
            self.assertEqual(self._answer(self.client.post(f"{SPACE_RC}/pair")), not_found)
            self.assertEqual(self._answer(self.client.post(f"{SPACE_RC}/pair/status")), not_found)   # before the body
        with self.on_space("openclaw"):
            self.assertEqual(self._answer(self.client.get(f"{SPACE_RC}/status")), not_found)

    def test_a_named_runtime_is_reached_whatever_the_space_agent(self) -> None:
        with self.on_space("claude_code"), _installed("codex", "claude_code"), \
                mock.patch.object(codex_rc, "pair", mock.AsyncMock(return_value={"ok": 1})):
            self.assertEqual(self.client.post("/api/chat/runtimes/codex/remote-control/pair").json(), {"ok": 1})
            missing = self.client.post("/api/chat/runtimes/claude_code/remote-control/pair")
            self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (501, "unsupported"))
            self.assertEqual(self._answer(self.client.get("/api/chat/runtimes/nope/remote-control/status")),
                             (404, UNKNOWN))

    def test_a_runtime_not_installed_answers_409(self) -> None:
        with _installed():
            response = self.client.get("/api/chat/runtimes/codex/remote-control/status")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "not_installed"))

    @staticmethod
    def _answer(response) -> tuple[int, object]:
        return response.status_code, response.json()


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_a_named_runtime_answers_with_its_own_usage_module(self) -> None:
        with _installed("codex"), mock.patch.object(codex_usage, "list_sessions", return_value=[{"id": "c"}]):
            self.assertEqual(self.client.get("/api/chat/runtimes/codex/usage/sessions").json(), [{"id": "c"}])

    def test_the_old_route_answers_for_the_space_agent(self) -> None:
        with mock.patch.object(usage, "load_usage_module", return_value=codex_usage), \
                mock.patch.object(codex_usage, "list_sessions", return_value=[{"id": "space"}]):
            self.assertEqual(self.client.get("/api/usage/sessions").json(), [{"id": "space"}])

    def test_a_runtime_without_usage_is_501_and_an_unknown_name_404(self) -> None:
        with _installed("cursor"):
            self.assertEqual(self.client.get("/api/chat/runtimes/cursor/usage/sessions").status_code, 501)
        with _installed():
            self.assertEqual(self.client.get("/api/chat/runtimes/codex/usage/sessions").status_code, 409)
        response = self.client.get("/api/chat/runtimes/nope/usage")
        self.assertEqual((response.status_code, response.json()), (404, UNKNOWN))


class OauthTests(unittest.TestCase):
    def test_xo_json_enables_every_runtimes_own_oauth_entry(self) -> None:
        oauth = xo_manifest.build_static_manifest("claude_code")["models"]["oauth"]
        expected = {key for runtime in reg.runtimes() for key in xo_manifest.own_oauth(runtime.name)}
        self.assertEqual({key for key, node in oauth.items() if node["enabled"]}, expected)

    def test_a_disabled_models_section_still_cascades(self) -> None:
        exception = next(m.name for m in reg.all_agents() if m.boot_rank is None
                         and xo_manifest._load_capabilities(m.name)["models"].get("enabled") is False)
        oauth = xo_manifest.build_static_manifest(exception)["models"]["oauth"]
        self.assertFalse(any(node["enabled"] for node in oauth.values()))

    def test_providers_status_adds_other_runtimes_enabled_missing_entries_from_the_advisory(self) -> None:
        facts = {"a": RuntimeFacts("a", installed=True, signed_in=True),
                 "b": RuntimeFacts("b", installed=True, signed_in=False),
                 "c": RuntimeFacts("c", installed=False),
                 "k": RuntimeFacts("k", installed=True, signed_in=True, via_api_key=True)}
        ranked = [mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()]
        for manifest, name in zip(ranked, ("space", "a", "c", "b", "k", "unread")):
            manifest.name = name
        owned = {"space": ["space"], "a": ["a", "x", "off"], "b": ["b", "a"], "c": ["c"], "k": ["k"],
                 "unread": ["unread"]}
        models = {"oauth": {key: {"enabled": key != "off"} for key in ("space", "a", "x", "off", "b", "c", "k", "unread")}}
        status = {"agent": "space", "oauth": {"space": {"connected": True}, "b": {"connected": True}}}
        space = mock.Mock(read_runtimes=mock.AsyncMock(return_value=facts))
        with mock.patch.object(providers.advisory, "current", return_value=space), \
                mock.patch.object(providers, "runtimes", return_value=ranked), \
                mock.patch.object(providers, "_read_xo_models", return_value=models), \
                mock.patch.object(providers, "own_oauth", side_effect=lambda name: dict.fromkeys(owned[name])):
            asyncio.run(providers._add_runtimes_oauth(status, "space"))
        self.assertEqual(status["oauth"], {"space": {"connected": True}, "b": {"connected": True},
                                           "a": {"connected": True}, "x": {"connected": True},
                                           "k": {"connected": False}})
        self.assertEqual(set(space.read_runtimes.await_args.args[1]), {"a", "b", "c", "k", "unread"})

    def test_providers_status_reads_nothing_when_no_entry_is_missing(self) -> None:
        status = {"agent": "space", "oauth": {"space": {"connected": True}}}
        manifest = mock.Mock()
        manifest.name = "space"
        with mock.patch.object(providers.advisory, "current") as current, \
                mock.patch.object(providers, "runtimes", return_value=[manifest]), \
                mock.patch.object(providers, "_read_xo_models", return_value={}), \
                mock.patch.object(providers, "own_oauth", return_value={"space": {}}):
            asyncio.run(providers._add_runtimes_oauth(status, "space"))
        current.assert_not_called()


class LocalAgentsTests(unittest.TestCase):
    def test_the_named_agent_first_then_each_other_installed_runtime(self) -> None:
        ranked = [m.name for m in reg.runtimes()]
        with mock.patch.object(reg, "named_agent", return_value=ranked[1]), \
                mock.patch.object(reg, "installed", return_value=True):
            self.assertEqual(reg.local_agents(), [ranked[1], ranked[0], *ranked[2:]])
        with mock.patch.object(reg, "named_agent", return_value="openclaw"), \
                mock.patch.object(reg, "installed", return_value=False):
            self.assertEqual(reg.local_agents(), ["openclaw"])

    def test_nothing_named_and_nothing_installed_sets_up_nothing(self) -> None:
        with mock.patch.object(reg, "named_agent", return_value=None), \
                mock.patch.object(reg, "installed", return_value=False):
            self.assertEqual(reg.local_agents(), [])


if __name__ == "__main__":
    unittest.main()
