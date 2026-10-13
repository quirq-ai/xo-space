"""Decision-model keys: saved in the Space's secret store (persisted, owner-only),
in effect at once, never returned, and named only by their provider module."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent import projects_chat
from services.cowork_agent.registry import agent_env
from services.decision_models import jev, keys, sage


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secrets = Path(tmp.name) / "secrets" / "secrets.env"
        self.secrets.parent.mkdir()
        self.secrets.write_text("# Space secrets\nOTHER_KEY=keep-me\n")
        for patcher in (mock.patch.object(agent_env, "ENV_FILE", self.secrets),
                        mock.patch.dict(os.environ, {"LEVANTO_API_KEY": "", "TYPESAFE_API_KEY": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)


class KeyServiceTests(_Sandbox):
    def test_a_saved_key_is_persisted_owner_only_and_in_effect_at_once(self) -> None:
        self.assertFalse(jev.configured())
        out = keys.set_key("jev", "  tsk_live_123  ")
        self.assertEqual((out["configured"], out["source"]), (True, "saved"))
        self.assertTrue(jev.configured(), "the provider reads it per call: no restart")
        text = self.secrets.read_text()
        self.assertIn("TYPESAFE_API_KEY=tsk_live_123", text)
        self.assertIn("OTHER_KEY=keep-me", text)
        self.assertIn("# Space secrets", text, "an upsert keeps the rest of the file")
        self.assertEqual(oct(self.secrets.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("tsk_live_123", repr(out))

    def test_clearing_removes_it_from_the_store_and_the_process(self) -> None:
        keys.set_key("sage", "lev_abc")
        self.assertTrue(sage.configured())
        out = keys.clear_key("sage")
        self.assertEqual((out["configured"], out["source"]), (False, None))
        self.assertNotIn("LEVANTO_API_KEY", self.secrets.read_text())

    def test_a_key_from_the_environment_is_reported_as_such(self) -> None:
        with mock.patch.dict(os.environ, {"LEVANTO_API_KEY": "from-shell"}):
            self.assertEqual(keys.status("sage")["source"], "environment")

    def test_bad_values_and_unknown_providers_are_refused(self) -> None:
        for value in ("", "   ", "two\nlines", "has space", "x" * 600, None):
            with self.assertRaises(keys.KeyError_):
                keys.set_key("jev", value)
        with self.assertRaises(keys.KeyError_) as ctx:
            keys.set_key("oracle", "k")
        self.assertEqual(ctx.exception.status, 404)


class RouteTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(projects_chat.router)
        self.client = TestClient(app)

    def test_list_save_and_clear(self) -> None:
        listed = self.client.get("/api/projects-chat/decision-models").json()
        self.assertEqual([m["provider"] for m in listed["decision_models"]], ["sage", "jev"])
        saved = self.client.put("/api/projects-chat/decision-models/sage/key", json={"api_key": "lev_secret_1"})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["configured"])
        self.assertNotIn("lev_secret_1", saved.text)
        self.assertNotIn("lev_secret_1", self.client.get("/api/projects-chat/decision-models").text)
        self.assertEqual(self.client.delete("/api/projects-chat/decision-models/sage/key").json()["configured"], False)

    def test_strict_bodies_and_errors(self) -> None:
        self.assertEqual(self.client.put("/api/projects-chat/decision-models/sage/key",
                                         json={"api_key": "k", "extra": 1}).status_code, 422)
        bad = self.client.put("/api/projects-chat/decision-models/sage/key", json={"api_key": "a b"})
        self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "invalid_key"))
        self.assertEqual(self.client.put("/api/projects-chat/decision-models/nope/key",
                                         json={"api_key": "k"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
