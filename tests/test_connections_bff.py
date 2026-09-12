"""Routes for polled connections: thin over the connections service.

Hermetic: every service function is patched by name on the module the
router imports, so nothing here touches ~/.quirq or the network."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import connections as routes
from routers.cowork_agent.bff import bff_routers, connections_router, inbox_router
from services.cowork_agent.connections import service

ROOT = Path(__file__).resolve().parents[1]

CONN = {
    "toolkit": "gmail", "display_name": "Gmail", "configured": True, "enabled": True,
    "interval_s": 900, "collectors": ["unread"],
    "available_collectors": [{"id": "unread", "label": "Unread mail", "default": True}],
    "connected_here": True, "last_poll_at": "2026-09-11T10:00:00Z", "last_ok_at": "2026-09-11T10:00:00Z",
    "last_error": None, "events_total": 3,
}
EVENT = {"ts": "2026-09-11T10:00:00Z", "type": "unread", "key": "m1", "title": "hello", "body": "",
         "url": "https://mail.google.com/mail/u/0/#all/m1", "toolkit": "gmail"}
POLL = {"toolkit": "gmail", "polled": True, "new_events": 2, "error": None, "skipped": None}
EXPECTED_PATHS = {
    "/api/connections",
    "/api/connections/{toolkit}",
    "/api/connections/{toolkit}/poll",
    "/api/connections/{toolkit}/events",
}


def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _err(code: str, status: int = 400) -> service.ConnectionsError:
    return service.ConnectionsError(code, f"{code} happened", status)


class ConnectionsRoutesTests(unittest.TestCase):
    def test_router_exposes_exactly_four_paths_right_after_the_inbox(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        self.assertEqual(set(app.openapi()["paths"]), EXPECTED_PATHS)
        self.assertEqual(bff_routers.index(connections_router), bff_routers.index(inbox_router) + 1)

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

    def test_bad_toolkit_ids_are_404_before_any_service_call(self) -> None:
        bad = ["Gmail", "a-b", "x" * 41, "g.mail", "gmail%20x"]
        with patch.object(service, "get_connection") as gc, patch.object(service, "configure") as cf, \
             patch.object(service, "remove") as rm, patch.object(service, "events") as ev, \
             patch.object(service, "poll_now", new=AsyncMock()) as pn:
            c = client()
            for tk in bad:
                with self.subTest(toolkit=tk):
                    responses = [
                        c.get(f"/api/connections/{tk}"),
                        c.put(f"/api/connections/{tk}", json={}),
                        c.delete(f"/api/connections/{tk}"),
                        c.post(f"/api/connections/{tk}/poll"),
                        c.get(f"/api/connections/{tk}/events"),
                    ]
                    for r in responses:
                        self.assertEqual(r.status_code, 404)
                        self.assertEqual(r.json()["detail"]["code"], "unknown_toolkit")
                        self.assertIn("message", r.json()["detail"])
            for m in (gc, cf, rm, ev):
                m.assert_not_called()
            pn.assert_not_awaited()

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
        src = (ROOT / "routers" / "cowork_agent" / "bff" / "connections.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"^\s*(import os|from os |import pathlib|from pathlib)", "BFF rule P2")
        self.assertNotRegex(src, r"openclaw|hermes|claude_code|codex|antigravity")
        self.assertIsNone(re.search("[\\u2013\\u2014]", src))
        self.assertIn("from services.cowork_agent.connections import service", src)


if __name__ == "__main__":
    unittest.main()
