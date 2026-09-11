from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import inbox as inbox_routes
from services.inbox import service

ITEM = {"id": "deadbeef", "ts": "2026-09-10T12:00:00Z", "source": "api", "kind": "note", "title": "t",
        "body": "", "project_id": None, "link": None, "status": "new", "key": None}
LISTING = {"schema": 1, "updated_at": None, "counts": {"new": 1, "seen": 0, "done": 0}, "items": [ITEM]}


def client() -> TestClient:
    app = FastAPI()
    app.include_router(inbox_routes.router)
    return TestClient(app)


class InboxRoutesTests(unittest.TestCase):
    def test_list_validates_status_in_the_handler_and_limit_by_query(self) -> None:
        with patch.object(service, "list_items", return_value=LISTING) as li:
            r = client().get("/api/inbox?status=bogus")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["code"], "invalid_status")
            self.assertEqual(client().get("/api/inbox?limit=0").status_code, 422)
            self.assertEqual(client().get("/api/inbox?limit=501").status_code, 422)
            li.assert_not_called()
            r = client().get("/api/inbox")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["counts"]["new"], 1)
            li.assert_called_with(status="open", limit=200)
            for status in ("open", "done", "all"):
                self.assertEqual(client().get(f"/api/inbox?status={status}&limit=500").status_code, 200)
            li.assert_called_with(status="all", limit=500)

    def test_create_returns_201_and_maps_validation_codes(self) -> None:
        with patch.object(service, "create_item", return_value=ITEM) as ci:
            r = client().post("/api/inbox", json={"title": "t", "link": {"view": "projects"}})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["id"], "deadbeef")
        ci.assert_called_once_with(title="t", body="", kind="note", source="api", project_id=None,
                                   link={"view": "projects"}, url=None)
        with patch.object(service, "create_item", return_value=ITEM) as ci:
            r = client().post("/api/inbox", json={"title": "t", "url": "https://example.test/x"})
        self.assertEqual(r.status_code, 201)
        ci.assert_called_once_with(title="t", body="", kind="note", source="api", project_id=None,
                                   link=None, url="https://example.test/x")
        for code in ("invalid_value", "invalid_project_id", "invalid_link"):
            with self.subTest(code=code):
                with patch.object(service, "create_item", side_effect=service.InboxError(code, "bad")):
                    r = client().post("/api/inbox", json={"title": "t"})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"], {"code": code, "message": "bad"})
        with patch.object(service, "create_item", return_value=ITEM) as ci:
            self.assertEqual(client().post("/api/inbox", json={"title": "t", "extra": 1}).status_code, 422)
            self.assertEqual(client().post("/api/inbox", json={"title": 7}).status_code, 422)
            self.assertEqual(client().post("/api/inbox", json={}).status_code, 422)
            ci.assert_not_called()

    def test_create_and_list_through_the_real_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": tmp, "QUIRQ_STATE_ROOT": tmp + "/.quirq"}):
                service._reset_throttle()
                c = client()
                r = c.post("/api/inbox", json={"title": "t", "link": {"path": "../x"}})
                self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_link"))
                r = c.post("/api/inbox", json={"title": "t", "project_id": "a/b"})
                self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_project_id"))
                r = c.post("/api/inbox", json={"title": "   "})
                self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
                r = c.post("/api/inbox", json={"title": "hello", "body": "b", "kind": "note", "project_id": "p"})
                self.assertEqual(r.status_code, 201)
                item_id = r.json()["id"]
                listing = c.get("/api/inbox").json()
                self.assertEqual(listing["counts"], {"new": 1, "seen": 0, "done": 0})
                self.assertEqual(listing["items"][0]["id"], item_id)
                self.assertEqual(c.patch(f"/api/inbox/{item_id}", json={"status": "done"}).json()["status"], "done")
                self.assertEqual(c.get("/api/inbox").json()["items"], [])
                self.assertEqual(c.delete(f"/api/inbox/{item_id}").json(), {"item_id": item_id, "deleted": True})
                service._reset_throttle()

    def test_patch_and_delete_reject_malformed_ids_before_the_service(self) -> None:
        with patch.object(service, "update_item") as up, patch.object(service, "delete_item") as de:
            for bad in ("nope", "DEADBEEF", "deadbeef1", "deadbee", "dead-bee"):
                with self.subTest(bad=bad):
                    r = client().patch(f"/api/inbox/{bad}", json={"status": "seen"})
                    self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "item_not_found"))
                    r = client().delete(f"/api/inbox/{bad}")
                    self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "item_not_found"))
            up.assert_not_called()
            de.assert_not_called()

    def test_patch_maps_typed_errors_and_body_shape(self) -> None:
        with patch.object(service, "update_item", side_effect=service.InboxError("item_not_found", "gone", 404)):
            r = client().patch("/api/inbox/deadbeef", json={"status": "seen"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "item_not_found"))
        with patch.object(service, "update_item", side_effect=service.InboxError("invalid_status", "bad")):
            r = client().patch("/api/inbox/deadbeef", json={"status": "archived"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_status"))
        with patch.object(service, "update_item", return_value={**ITEM, "status": "seen"}) as up:
            self.assertEqual(client().patch("/api/inbox/deadbeef", json={"status": "seen"}).json()["status"], "seen")
            up.assert_called_once_with("deadbeef", "seen")
            self.assertEqual(client().patch("/api/inbox/deadbeef", json={}).status_code, 422)
            self.assertEqual(client().patch("/api/inbox/deadbeef", json={"status": "seen", "x": 1}).status_code, 422)

    def test_delete_is_idempotent_in_shape(self) -> None:
        for deleted in (True, False):
            with patch.object(service, "delete_item", return_value=deleted) as de:
                r = client().delete("/api/inbox/deadbeef")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"item_id": "deadbeef", "deleted": deleted})
            de.assert_called_once_with("deadbeef")

    def test_router_is_registered_right_after_project_sharing(self) -> None:
        from routers.cowork_agent.bff import bff_routers
        from routers.cowork_agent.bff.project_sharing import router as sharing_router
        self.assertEqual(bff_routers.index(inbox_routes.router), bff_routers.index(sharing_router) + 1)
        paths = {route.path for route in inbox_routes.router.routes}
        self.assertEqual(paths, {"/api/inbox", "/api/inbox/{item_id}"})


if __name__ == "__main__":
    unittest.main()
