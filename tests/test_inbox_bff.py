"""``routers/cowork_agent/bff/inbox.py``: the wire over ``services.inbox.service``
(docs/work-and-workitems.md section 18). The service is patched for the
shape tests; one pass runs the real service over a temp state root."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import bff_routers, inbox_router, project_sharing_router
from routers.cowork_agent.bff import inbox as inbox_routes
from services.inbox import service
from services.work.store import WorkError

from tests.inbox_harness import SampleRoot

ROW = {"kind": "workitem", "key": "workitem:inbox-connections:aaaa", "id": "aaaa", "project_id": "inbox-connections",
       "title": "t", "section": "connections", "entity": "gmail", "state": "new"}
LISTING = {"schema": 1, "sections": [{"id": "connections", "label": "Connections", "counts": {"new": 1}, "entities": []}],
           "rows": [ROW], "count": 1, "runner": {"enabled": True}}
ITEM = "/api/inbox/inbox-connections/aaaa"


def client() -> TestClient:
    app = FastAPI()
    app.include_router(inbox_routes.router)
    return TestClient(app)


class InboxRoutesTests(unittest.TestCase):
    def test_the_router_is_mounted_with_every_route_and_nothing_else(self) -> None:
        self.assertIn(inbox_router, bff_routers)
        self.assertLess(bff_routers.index(project_sharing_router), bff_routers.index(inbox_router))
        methods = {(m, route.path) for route in inbox_routes.router.routes for m in route.methods}
        self.assertEqual(methods, {
            ("GET", "/api/inbox"), ("GET", "/api/inbox/sections"), ("PUT", "/api/inbox/sections/{section}"),
            ("POST", "/api/inbox"), ("GET", "/api/inbox/{project_id}/{workitem_id}"),
            ("POST", "/api/inbox/{project_id}/{workitem_id}/reply"), ("POST", "/api/inbox/{project_id}/{workitem_id}/start"),
            ("POST", "/api/inbox/{project_id}/{workitem_id}/send"), ("POST", "/api/inbox/{project_id}/{workitem_id}/archive"),
            ("POST", "/api/inbox/{project_id}/{workitem_id}/reopen"),
        })
        # the rows API of the first design is gone
        self.assertEqual(client().patch("/api/inbox", json={"ids": ["a"], "status": "seen"}).status_code, 405)
        self.assertEqual(client().delete("/api/inbox/deadbeef").status_code, 404)

    def test_list_passes_the_filters_and_maps_errors(self) -> None:
        with patch.object(service, "list_rows", return_value=LISTING) as lr:
            r = client().get("/api/inbox")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["rows"][0]["id"], "aaaa")
            lr.assert_called_once_with(section=None, entity=None, state="open", limit=100)
            client().get("/api/inbox?section=projects&entity=xo-space&state=closed&limit=500")
            lr.assert_called_with(section="projects", entity="xo-space", state="closed", limit=500)
            self.assertEqual(client().get("/api/inbox?limit=0").status_code, 422)
            self.assertEqual(client().get("/api/inbox?limit=501").status_code, 422)
        with patch.object(service, "list_rows", side_effect=WorkError("invalid_value", "state must be one of", 400)):
            r = client().get("/api/inbox?state=bogus")
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
        with patch.object(service, "list_rows", side_effect=WorkError("invalid_section", "no", 404)):
            self.assertEqual(client().get("/api/inbox?section=mail").status_code, 404)

    def test_sections_and_policy(self) -> None:
        with patch.object(service, "sections", return_value={"sections": [{"id": "connections"}], "runner": {"enabled": True}}):
            self.assertEqual(client().get("/api/inbox/sections").json()["sections"][0]["id"], "connections")
        with patch.object(service, "set_policy", return_value={"schema": 1}) as put:
            r = client().put("/api/inbox/sections/issues", json={"sessions": {"mode": "auto", "kinds": ["issue.open"], "runtime": None}})
            self.assertEqual(r.status_code, 200)
            put.assert_called_once_with("issues", {"sessions": {"mode": "auto", "kinds": ["issue.open"], "runtime": None}})
            self.assertEqual(client().put("/api/inbox/sections/issues", json={"items": {"unread": True}}).status_code, 422)
            self.assertEqual(client().put("/api/inbox/sections/issues", json={"sessions": {"act": 1}}).status_code, 422)
            self.assertEqual(client().put("/api/inbox/sections/issues", json={"odd": 1}).status_code, 422)
        with patch.object(service, "set_policy", side_effect=WorkError("invalid_section", "no", 404)):
            self.assertEqual(client().put("/api/inbox/sections/gmail", json={}).status_code, 404)

    def test_create_returns_201_and_maps_validation_codes(self) -> None:
        with patch.object(service, "create_post", return_value=ROW) as cp:
            r = client().post("/api/inbox", json={"title": "t", "link": {"view": "projects"}})
            self.assertEqual((r.status_code, r.json()["id"]), (201, "aaaa"))
            cp.assert_called_once_with(title="t", body="", kind="note", source="api", project_id=None,
                                       link={"view": "projects"}, url=None)
        self.assertEqual(client().post("/api/inbox", json={"body": "x"}).status_code, 422)
        self.assertEqual(client().post("/api/inbox", json={"title": "t", "zzz": 1}).status_code, 422)
        for code in ("invalid_value", "invalid_project_id", "invalid_link"):
            with self.subTest(code=code):
                with patch.object(service, "create_post", side_effect=service.InboxError(code, "bad")):
                    r = client().post("/api/inbox", json={"title": "t"})
                self.assertEqual((r.status_code, r.json()["detail"]), (400, {"code": code, "message": "bad"}))

    def test_detail_and_the_actions(self) -> None:
        with patch.object(service, "item_detail", return_value={"row": ROW, "running": False}):
            self.assertEqual(client().get(ITEM).json()["row"]["id"], "aaaa")
        with patch.object(service, "item_detail", side_effect=WorkError("workitem_not_found", "No such work item.", 404)):
            self.assertEqual(client().get("/api/inbox/inbox-connections/nope").status_code, 404)
        with patch.object(service, "reply", new=AsyncMock(return_value={"session_id": "s1"})) as reply:
            r = client().post(ITEM + "/reply", json={"text": "Keep it short."})
            self.assertEqual((r.status_code, r.json()["session_id"]), (202, "s1"))
            reply.assert_awaited_once_with("inbox-connections", "aaaa", "Keep it short.")
            self.assertEqual(client().post(ITEM + "/reply", json={}).status_code, 422)
            self.assertEqual(client().post(ITEM + "/reply", json={"text": "x", "to": "y"}).status_code, 422)
        with patch.object(service, "reply", new=AsyncMock(side_effect=WorkError("session_running", "wait", 409))):
            self.assertEqual(client().post(ITEM + "/reply", json={"text": "x"}).status_code, 409)
        with patch.object(service, "start", new=AsyncMock(return_value={"session_id": "s1", "attempt": 1})) as start:
            self.assertEqual(client().post(ITEM + "/start").status_code, 202)
            start.assert_awaited_once_with("inbox-connections", "aaaa", retry=False)
            client().post(ITEM + "/start?retry=true")
            start.assert_awaited_with("inbox-connections", "aaaa", retry=True)
        with patch.object(service, "send", new=AsyncMock(return_value={"session_id": "s1"})) as send:
            self.assertEqual(client().post(ITEM + "/send").status_code, 202)
            send.assert_awaited_once_with("inbox-connections", "aaaa")
        with patch.object(service, "send", new=AsyncMock(side_effect=WorkError("act_not_allowed", "no", 409))):
            self.assertEqual(client().post(ITEM + "/send").status_code, 409)
        with patch.object(service, "archive", return_value={**ROW, "state": "closed"}) as archive:
            self.assertEqual(client().post(ITEM + "/archive").json()["state"], "closed")
            archive.assert_called_once_with("inbox-connections", "aaaa", reason=None)
            client().post(ITEM + "/archive", json={"reason": "not_planned"})
            archive.assert_called_with("inbox-connections", "aaaa", reason="not_planned")
            self.assertEqual(client().post(ITEM + "/archive", json={"why": "x"}).status_code, 422)
        with patch.object(service, "reopen", return_value=ROW) as reopen:
            self.assertEqual(client().post(ITEM + "/reopen").json()["state"], "new")
            reopen.assert_called_once_with("inbox-connections", "aaaa")


class RealServiceTests(SampleRoot):
    def test_post_list_detail_archive_and_reopen_through_the_real_service(self) -> None:
        c = client()
        r = c.post("/api/inbox", json={"title": "Which license?", "body": "MIT or Apache", "kind": "question", "source": "claude_code"})
        self.assertEqual(r.status_code, 201)
        row = r.json()
        self.assertEqual((row["project_id"], row["section"], row["entity"], row["state"]), ("inbox-agents", "agents", "claude_code", "new"))
        listing = c.get("/api/inbox?section=agents").json()
        self.assertEqual([x["id"] for x in listing["rows"] if x["kind"] == "workitem"], [row["id"]])
        agents = next(s for s in listing["sections"] if s["id"] == "agents")
        self.assertEqual(agents["counts"]["new"], 1)
        self.assertIn("claude_code", [e["id"] for e in agents["entities"]])
        self.assertEqual(c.get("/api/inbox?section=agents&entity=nobody").json()["count"], 0)
        detail = c.get(f"/api/inbox/{row['project_id']}/{row['id']}").json()
        self.assertEqual(detail["fact"]["body"], "MIT or Apache")
        self.assertIsNone(detail["session"])
        self.assertEqual(detail["workitem"]["title"], "Which license?")
        self.assertTrue(detail["can_reply"])
        self.assertFalse(detail["can_send"])
        self.assertEqual(c.get(f"/api/inbox/{row['project_id']}/00000000-0000-4000-8000-000000000009").status_code, 404)
        self.assertEqual(c.get(f"/api/inbox/no-such-project/{row['id']}").status_code, 404)
        closed = c.post(f"/api/inbox/{row['project_id']}/{row['id']}/archive", json={"reason": "not_planned"}).json()
        self.assertEqual((closed["state"], closed["status"], closed["state_reason"]), ("closed", "closed", "not_planned"))
        self.assertEqual(c.get("/api/inbox?section=agents&entity=claude_code").json()["count"], 0, "closed rows leave the open list")
        self.assertEqual(c.get("/api/inbox?section=agents&entity=claude_code&state=closed").json()["count"], 1)
        reopened = c.post(f"/api/inbox/{row['project_id']}/{row['id']}/reopen").json()
        self.assertEqual((reopened["state"], reopened["state_reason"]), ("new", "reopened"))
        policy = c.put("/api/inbox/sections/agents", json={"sessions": {"mode": "auto", "max_concurrent": 1}}).json()
        self.assertEqual((policy["sessions"]["mode"], policy["sessions"]["max_concurrent"]), ("auto", 1))
        sections = c.get("/api/inbox/sections").json()
        self.assertEqual(next(s for s in sections["sections"] if s["id"] == "agents")["policy"]["sessions"]["mode"], "auto")


if __name__ == "__main__":
    unittest.main()
