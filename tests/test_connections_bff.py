"""Routes for polled connections: thin over the connections service.

Hermetic: every service function is patched by name on the module the
router imports, so nothing here touches ~/.quirq or the network."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.connections import routes
from routers.errors import install_service_errors
from modules.connections import service, store

ROOT = Path(__file__).resolve().parents[1]

CONN = {
    "toolkit": "gmail", "display_name": "Gmail", "configured": True, "enabled": True,
    "interval_s": 900, "collectors": ["unread"],
    "available_collectors": [{"id": "unread", "label": "Unread mail", "default": True}],
    "connected_here": True, "last_poll_at": "2026-09-11T10:00:00Z", "last_ok_at": "2026-09-11T10:00:00Z",
    "last_error": None, "events_total": 3,
    "account_label": "ana@example.com", "account_checked_at": "2026-09-11T10:00:00Z",
}
EVENT = {"ts": "2026-09-11T10:00:00Z", "type": "unread", "key": "m1", "title": "hello", "body": "",
         "url": "https://mail.google.com/mail/u/0/#all/m1", "toolkit": "gmail"}
POLL = {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None}
ACCOUNT = {"toolkit": "gmail", "account_label": "ana@example.com", "account_checked_at": "2026-09-11T10:00:00Z",
           "error": None, "cached": False}
EXPECTED_PATHS = {
    "/api/connections",
    "/api/connections/{toolkit}",
    "/api/connections/{toolkit}/poll",
    "/api/connections/{toolkit}/account",
    "/api/connections/{toolkit}/events",
}


def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    install_service_errors(app)
    return TestClient(app)


def _err(code: str, status: int = 400) -> service.ConnectionsError:
    return service.ConnectionsError(code, f"{code} happened", status)


class ConnectionsRoutesTests(unittest.TestCase):
    def test_router_exposes_exactly_five_paths(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        self.assertEqual(set(app.openapi()["paths"]), EXPECTED_PATHS)

    def test_list_shape(self) -> None:
        with patch.object(service, "signed_in", return_value=False) as si, \
             patch.object(service, "poller_enabled", return_value=True) as pe, \
             patch.object(service, "list_connections", return_value=[CONN]) as lc:
            r = client().get("/api/connections")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"signed_in": False, "poller_enabled": True, "connections": [CONN]})
        si.assert_called_once()
        pe.assert_called_once()
        lc.assert_called_once_with()

    def test_bad_toolkit_ids_are_404_before_any_store_or_poller_call(self) -> None:
        """The router passes the path value through; the service answers a
        malformed or unknown id with its 404 before any path is built, so
        nothing below it (the store, the poller) is ever reached."""
        bad = ["Gmail", "a-b", "x" * 41, "g.mail", "gmail%20x"]
        with patch.object(service.store, "read_accounts") as ra_store, \
             patch.object(service, "_read_config_or_none") as rc, \
             patch.object(service.store, "write_config") as wc, \
             patch.object(service.store, "remove") as rm, \
             patch.object(service.store, "read_events") as ev, \
             patch.object(service.poller, "poll_connection", new=AsyncMock()) as pn, \
             patch.object(service.poller, "refresh_account", new=AsyncMock()) as ra:
            c = client()
            for tk in bad:
                with self.subTest(toolkit=tk):
                    responses = [
                        c.get(f"/api/connections/{tk}"),
                        c.put(f"/api/connections/{tk}", json={}),
                        c.delete(f"/api/connections/{tk}"),
                        c.post(f"/api/connections/{tk}/poll"),
                        c.post(f"/api/connections/{tk}/account"),
                        c.get(f"/api/connections/{tk}/events"),
                    ]
                    for r in responses:
                        self.assertEqual(r.status_code, 404)
                        self.assertEqual(r.json()["detail"], {"code": "unknown_toolkit", "message": "Unknown toolkit."})
            # well-formed but not in the catalog: still a 404, and the id is named
            r = c.get("/api/connections/not_a_toolkit")
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "unknown_toolkit"))
            self.assertIn("not_a_toolkit", r.json()["detail"]["message"])
            for m in (ra_store, rc, wc, rm, ev):
                m.assert_not_called()
            pn.assert_not_awaited()
            ra.assert_not_awaited()

    def test_get_maps_service_errors_to_code_and_message(self) -> None:
        with patch.object(service, "get_connection", return_value=CONN) as gc:
            r = client().get("/api/connections/gmail")
        self.assertEqual((r.status_code, r.json()), (200, CONN))
        gc.assert_called_once_with("gmail")
        with patch.object(service, "get_connection", side_effect=_err("unknown_toolkit", 404)):
            r = client().get("/api/connections/figma_x")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], {"code": "unknown_toolkit", "message": "unknown_toolkit happened"})

    def test_put_passes_only_the_given_fields(self) -> None:
        with patch.object(service, "configure", return_value=CONN) as cf:
            c = client()
            r = c.put("/api/connections/gmail", json={})
            self.assertEqual((r.status_code, r.json()), (200, CONN))
            cf.assert_called_with("gmail")
            r = c.put("/api/connections/gmail", json={"enabled": False, "interval_s": 300, "collectors": ["unread"]})
            self.assertEqual(r.status_code, 200)
            cf.assert_called_with("gmail", enabled=False, interval_s=300, collectors=["unread"])
            c.put("/api/connections/gmail", json={"interval_s": 3600})
            cf.assert_called_with("gmail", interval_s=3600)
            c.put("/api/connections/gmail", json={"collectors": []})
            cf.assert_called_with("gmail", collectors=[])

    def test_put_body_is_strict(self) -> None:
        with patch.object(service, "configure", return_value=CONN) as cf:
            c = client()
            for body in ({"interval_s": "900"}, {"enabled": "true"}, {"enabled": 1}, {"collectors": "unread"},
                         {"collectors": [1]}, {"extra": 1}, {"interval_s": 900.5}, [1]):
                with self.subTest(body=body):
                    self.assertEqual(c.put("/api/connections/gmail", json=body).status_code, 422)
            cf.assert_not_called()

    def test_put_maps_validation_errors(self) -> None:
        for code in ("invalid_interval", "invalid_collector"):
            with self.subTest(code=code):
                with patch.object(service, "configure", side_effect=_err(code)):
                    r = client().put("/api/connections/gmail", json={"interval_s": 5})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"], {"code": code, "message": f"{code} happened"})
        with patch.object(service, "configure", side_effect=_err("unknown_toolkit", 404)):
            r = client().put("/api/connections/unknown", json={"enabled": True})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "unknown_toolkit"))

    def test_delete_shape(self) -> None:
        with patch.object(service, "remove", return_value=True) as rm:
            r = client().delete("/api/connections/gmail")
        self.assertEqual((r.status_code, r.json()), (200, {"toolkit": "gmail", "removed": True}))
        rm.assert_called_once_with("gmail")
        with patch.object(service, "remove", return_value=False):
            self.assertEqual(client().delete("/api/connections/gmail").json(), {"toolkit": "gmail", "removed": False})

    def test_poll_route_awaits_the_service(self) -> None:
        with patch.object(service, "poll_now", new=AsyncMock(return_value=POLL)) as pn:
            r = client().post("/api/connections/gmail/poll")
        self.assertEqual((r.status_code, r.json()), (200, POLL))
        pn.assert_awaited_once_with("gmail")
        with patch.object(service, "poll_now", new=AsyncMock(side_effect=_err("unknown_toolkit", 404))):
            r = client().post("/api/connections/gmail/poll")
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "unknown_toolkit"))

    def test_account_route_awaits_the_service_and_never_5xxs_a_provider_failure(self) -> None:
        with patch.object(service, "refresh_account", new=AsyncMock(return_value=ACCOUNT)) as ra:
            r = client().post("/api/connections/gmail/account")
        self.assertEqual((r.status_code, r.json()), (200, ACCOUNT))
        ra.assert_awaited_once_with("gmail")
        failed = {**ACCOUNT, "account_label": None, "account_checked_at": None,
                  "error": "gmail is no longer connected on Composio (the sign-in expired or was revoked): "
                           "reconnect it from the Connectors tab"}
        with patch.object(service, "refresh_account", new=AsyncMock(return_value=failed)):
            r = client().post("/api/connections/gmail/account")
        self.assertEqual((r.status_code, r.json()), (200, failed), "a provider failure is 200 with error set")
        no_lookup = {"toolkit": "notion", "account_label": None, "account_checked_at": None,
                     "error": "no account lookup for notion yet", "cached": False}
        with patch.object(service, "refresh_account", new=AsyncMock(return_value=no_lookup)):
            r = client().post("/api/connections/notion/account")
        self.assertEqual((r.status_code, r.json()), (200, no_lookup))
        with patch.object(service, "refresh_account", new=AsyncMock(side_effect=_err("unknown_toolkit", 404))):
            r = client().post("/api/connections/figma_x/account")
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "unknown_toolkit"))
        self.assertEqual(client().get("/api/connections/gmail/account").status_code, 405, "POST only")

    def test_events_limit_is_bounded_by_the_query_and_defaults_to_50(self) -> None:
        with patch.object(service, "events", return_value=[EVENT]) as ev:
            c = client()
            r = c.get("/api/connections/gmail/events")
            self.assertEqual((r.status_code, r.json()), (200, {"toolkit": "gmail", "events": [EVENT]}))
            ev.assert_called_with("gmail", limit=50)
            self.assertEqual(c.get("/api/connections/gmail/events?limit=500").status_code, 200)
            ev.assert_called_with("gmail", limit=500)
            self.assertEqual(c.get("/api/connections/gmail/events?limit=1").status_code, 200)
            ev.assert_called_with("gmail", limit=1)
            self.assertEqual(c.get("/api/connections/gmail/events?limit=0").status_code, 422)
            self.assertEqual(c.get("/api/connections/gmail/events?limit=501").status_code, 422)
            self.assertEqual(c.get("/api/connections/gmail/events?limit=abc").status_code, 422)
            self.assertEqual(ev.call_count, 3)

    def test_router_is_thin_and_names_no_agent(self) -> None:
        src = (ROOT / "modules" / "connections" / "routes.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"^\s*(import os|from os |import pathlib|from pathlib)", "BFF rule P2")
        self.assertNotRegex(src, r"openclaw|hermes|claude_code|codex|antigravity")
        self.assertIsNone(re.search("[\\u2013\\u2014]", src))
        self.assertIn("from . import service", src)
        self.assertIn("POST   /api/connections/{toolkit}/account", src, "documented in the header")


class ServiceEntryAccountFieldsTests(unittest.TestCase):
    """The two account fields on every entry ``GET /api/connections`` and
    ``GET /api/connections/{toolkit}`` return, read from ``accounts.json``
    once per listing, configured or not. Hermetic: QUIRQ_STATE_ROOT in a
    temp dir, the workspace scope patched on the service module."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(root / ".quirq"),
                                            "XO_PROJECTS_ROOT": str(root / "projects")})
        self._env.start()
        self.assertEqual(store.connections_dir(), root / ".quirq" / "connections")
        scope = patch.object(service.space_scope, "is_enabled", return_value=False)
        scope.start()
        self.addCleanup(scope.stop)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_entries_carry_the_cached_label_configured_or_not(self) -> None:
        for entry in service.list_connections():
            self.assertEqual((entry["account_label"], entry["account_checked_at"]), (None, None), entry["toolkit"])
        store.write_config("gmail")
        remembered = store.remember_account("gmail", "ana@example.com", "ca_1")
        store.remember_account("notion", "Ana's workspace", None)      # no config.json: still carries a label
        by_id = {entry["toolkit"]: entry for entry in service.list_connections()}
        self.assertEqual((by_id["gmail"]["configured"], by_id["gmail"]["account_label"], by_id["gmail"]["account_checked_at"]),
                         (True, "ana@example.com", remembered["checked_at"]))
        self.assertEqual((by_id["notion"]["configured"], by_id["notion"]["account_label"]), (False, "Ana's workspace"))
        self.assertEqual((by_id["slack"]["account_label"], by_id["slack"]["account_checked_at"]), (None, None))
        one = service.get_connection("gmail")
        self.assertEqual(one, by_id["gmail"])
        # every field the entry had before is still there
        for name in ("toolkit", "display_name", "configured", "enabled", "interval_s", "collectors",
                     "available_collectors", "connected_here", "last_poll_at", "last_ok_at", "last_error",
                     "events_total"):
            self.assertIn(name, one)
        with patch.object(service.store, "read_accounts", wraps=store.read_accounts) as ra:
            service.list_connections()
        ra.assert_called_once()


if __name__ == "__main__":
    unittest.main()
