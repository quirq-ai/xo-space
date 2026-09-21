"""``routers/cowork_agent/bff/work.py``: the wire over ``services.work.service``.
The service is patched; these tests pin the routes, the status codes, the
strict bodies and the error mapping."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import bff_routers, connections_router, work_router
from routers.cowork_agent.bff import work as work_routes
from services.work import service

PAGE = {"schema": 2, "counts": {"decisions": 1, "calendar": 0, "completed": 0, "work": 0}, "badge": 1,
        "decisions": [{"key": "todo:p:1", "reason": "todo_blocked", "since": "2026-09-18T00:00:00Z"}],
        "calendar": [], "completed": [], "work": [], "me": ["local"], "projects": []}


def client() -> TestClient:
    app = FastAPI()
    app.include_router(work_routes.router)
    return TestClient(app)


class WorkRoutesTests(unittest.TestCase):
    def test_the_router_is_mounted_after_connections_with_every_route(self) -> None:
        self.assertEqual(bff_routers.index(work_router), bff_routers.index(connections_router) + 1)
        methods = {(m, route.path) for route in work_routes.router.routes for m in route.methods}
        self.assertEqual(methods, {
            ("GET", "/api/work/inbox"), ("GET", "/api/work/attention"), ("GET", "/api/work/summary"),
            ("GET", "/api/feed"), ("POST", "/api/feed"), ("PUT", "/api/work/watermark"),
            ("POST", "/api/work/dismiss"), ("DELETE", "/api/work/dismiss/{key:path}"),
            ("POST", "/api/work/ack"), ("DELETE", "/api/work/ack/{key:path}"),
            ("POST", "/api/work/promote"), ("PATCH", "/api/work/pins"),
            ("GET", "/api/work/inbox/connections"), ("PUT", "/api/work/inbox/connections/{toolkit}"),
            ("GET", "/api/work/inbox/items"), ("GET", "/api/work/inbox/items/{toolkit}/{item_id}"),
            ("GET", "/api/work/inbox/items/{toolkit}/{item_id}/thread"),
            ("POST", "/api/work/inbox/items/{toolkit}/{item_id}/reply"),
            ("POST", "/api/work/inbox/items/{toolkit}/{item_id}/start"),
            ("POST", "/api/work/inbox/items/{toolkit}/{item_id}/decide"),
            ("POST", "/api/work/inbox/items/{toolkit}/{item_id}/send"),
        })

    def test_connection_policy_and_items_routes(self) -> None:
        with patch.object(service, "inbox_connections", return_value={"connections": [], "available": ["gmail"], "runner": {"enabled": True}}):
            self.assertEqual(client().get("/api/work/inbox/connections").json()["available"], ["gmail"])
        with patch.object(service, "set_connection_policy", return_value={"schema": 1}) as put:
            r = client().put("/api/work/inbox/connections/gmail", json={"items": {"unread": True}, "sessions": {"mode": "auto", "kinds": ["unread"], "runtime": None}})
            self.assertEqual(r.status_code, 200)
            put.assert_called_once_with("gmail", {"items": {"unread": True}, "sessions": {"mode": "auto", "kinds": ["unread"], "runtime": None}})
            self.assertEqual(client().put("/api/work/inbox/connections/gmail", json={"items": {"unread": "yes"}}).status_code, 422)
            self.assertEqual(client().put("/api/work/inbox/connections/gmail", json={"sessions": {"act": 1}}).status_code, 422)
            self.assertEqual(client().put("/api/work/inbox/connections/gmail", json={"odd": 1}).status_code, 422)
        with patch.object(service, "set_connection_policy", side_effect=service.WorkError("connection_not_configured", "no", 404)):
            self.assertEqual(client().put("/api/work/inbox/connections/slack", json={}).status_code, 404)
        with patch.object(service, "list_inbox_items", return_value={"items": [], "count": 0}) as li:
            self.assertEqual(client().get("/api/work/inbox/items?connection=gmail&status=new&limit=5").status_code, 200)
            li.assert_called_once_with(connection="gmail", status="new", limit=5)
            self.assertEqual(client().get("/api/work/inbox/items?limit=0").status_code, 422)
        with patch.object(service, "inbox_item", return_value={"item": {"id": "unread-1"}}):
            self.assertEqual(client().get("/api/work/inbox/items/gmail/unread-1").json()["item"]["id"], "unread-1")

    def test_thread_and_reply(self) -> None:
        with patch.object(service, "item_thread", return_value={"key": "item:gmail:unread-1", "thread": [], "running": False}):
            self.assertEqual(client().get("/api/work/inbox/items/gmail/unread-1/thread").json()["running"], False)
        with patch.object(service, "reply_inbox_item", new=AsyncMock(return_value={"session_id": "s1"})) as reply:
            r = client().post("/api/work/inbox/items/gmail/unread-1/reply", json={"text": "Keep it short."})
            self.assertEqual((r.status_code, r.json()["session_id"]), (202, "s1"))
            reply.assert_awaited_once_with("gmail", "unread-1", "Keep it short.")
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/reply", json={}).status_code, 422)
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/reply", json={"text": "x", "to": "y"}).status_code, 422)
        with patch.object(service, "reply_inbox_item", new=AsyncMock(side_effect=service.WorkError("session_running", "wait", 409))):
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/reply", json={"text": "x"}).status_code, 409)

    def test_start_decide_and_send(self) -> None:
        with patch.object(service, "start_inbox_item", new=AsyncMock(return_value={"session_id": "s1", "attempt": 1, "project_id": "inbox-gmail"})) as start:
            r = client().post("/api/work/inbox/items/gmail/unread-1/start")
            self.assertEqual((r.status_code, r.json()["session_id"]), (202, "s1"))
            start.assert_awaited_once_with("gmail", "unread-1", retry=False)
            client().post("/api/work/inbox/items/gmail/unread-1/start?retry=true")
            start.assert_awaited_with("gmail", "unread-1", retry=True)
        with patch.object(service, "start_inbox_item", new=AsyncMock(side_effect=service.WorkError("session_exists", "running", 409))):
            r = client().post("/api/work/inbox/items/gmail/unread-1/start")
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "session_exists"))
        with patch.object(service, "decide_inbox_item", return_value={"id": "unread-1", "decided": {"action": "tracked"}}) as decide:
            r = client().post("/api/work/inbox/items/gmail/unread-1/decide", json={"action": "track", "project_id": "p"})
            self.assertEqual(r.status_code, 200)
            decide.assert_called_once_with("gmail", "unread-1", "track", project_id="p", title=None, assignee=None)
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/decide", json={"action": "track", "x": 1}).status_code, 422)
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/decide", json={}).status_code, 422)
        with patch.object(service, "send_inbox_item", new=AsyncMock(return_value={"session_id": "s1"})) as send:
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/send").status_code, 202)
            send.assert_awaited_once_with("gmail", "unread-1")
        with patch.object(service, "send_inbox_item", new=AsyncMock(side_effect=service.WorkError("act_not_allowed", "no", 409))):
            self.assertEqual(client().post("/api/work/inbox/items/gmail/unread-1/send").status_code, 409)

    def test_reads(self) -> None:
        with patch.object(service, "inbox_page", return_value=PAGE) as page, \
             patch.object(service, "attention_items", return_value={"items": PAGE["decisions"], "counts": {"todo_blocked": 1}, "total": 1}), \
             patch.object(service, "summary", return_value={"badge": 1}):
            r = client().get("/api/work/inbox")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["badge"], 1)
            page.assert_called_once_with()
            self.assertEqual(client().get("/api/work/attention").json()["total"], 1)
            self.assertEqual(client().get("/api/work/summary").json(), {"badge": 1})

    def test_feed_query_is_parsed_and_validated(self) -> None:
        with patch.object(service, "feed", return_value={"entries": [], "next_cursor": None, "watermark": None, "sources": {}}) as feed:
            r = client().get("/api/feed?limit=50&sources=jobs,posts&kinds=job.failed&project=p&before=2026-09-18T00:00:00Z&since=2026-09-01T00:00:00Z")
            self.assertEqual(r.status_code, 200)
            feed.assert_called_once_with(limit=50, before="2026-09-18T00:00:00Z", since="2026-09-01T00:00:00Z",
                                         sources=["jobs", "posts"], kinds=["job.failed"], project="p")
            self.assertEqual(client().get("/api/feed?limit=0").status_code, 422)
            self.assertEqual(client().get("/api/feed?limit=501").status_code, 422)
            client().get("/api/feed")
            feed.assert_called_with(limit=100, before=None, since=None, sources=None, kinds=None, project=None)
        with patch.object(service, "feed", side_effect=service.WorkError("invalid_value", "bad")):
            r = client().get("/api/feed?before=soon")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"], {"code": "invalid_value", "message": "bad"})

    def test_post_is_201_strict_and_mapped(self) -> None:
        stored = {"id": "deadbeef", "ts": "2026-09-18T00:00:00Z", "title": "t"}
        with patch.object(service, "create_post", return_value=stored) as cp:
            r = client().post("/api/feed", json={"title": "t", "kind": "question", "ref": {"workitem_id": "w"}})
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.json()["id"], "deadbeef")
            cp.assert_called_once_with(title="t", body="", kind="question", source="api", project_id=None, link=None, url=None,
                                       ref={"workitem_id": "w"})
            self.assertEqual(client().post("/api/feed", json={"title": "t", "extra": 1}).status_code, 422)
            self.assertEqual(client().post("/api/feed", json={"body": "no title"}).status_code, 422)
        with patch.object(service, "create_post", side_effect=service.WorkError("invalid_project_id", "bad")):
            r = client().post("/api/feed", json={"title": "t", "project_id": "../x"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_project_id"))

    def test_marks_answer_204_and_take_a_path_key_with_slashes(self) -> None:
        with patch.object(service, "dismiss") as dismiss, patch.object(service, "undismiss") as undismiss, \
             patch.object(service, "ack") as ack, patch.object(service, "unack") as unack, \
             patch.object(service, "set_pin") as pin, patch.object(service, "set_watermark", return_value={"watermark": "x"}) as wm:
            self.assertEqual(client().post("/api/work/dismiss", json={"key": "todo:p:1", "since": "2026-09-18T00:00:00Z"}).status_code, 204)
            dismiss.assert_called_once_with("todo:p:1", "2026-09-18T00:00:00Z")
            self.assertEqual(client().post("/api/work/dismiss", json={"key": "share:o/r"}).status_code, 204)
            dismiss.assert_called_with("share:o/r", None)
            self.assertEqual(client().delete("/api/work/dismiss/share:o/r").status_code, 204)
            undismiss.assert_called_once_with("share:o/r")
            self.assertEqual(client().post("/api/work/ack", json={"key": "job:runs:x:2026-09-18"}).status_code, 204)
            ack.assert_called_once_with("job:runs:x:2026-09-18")
            self.assertEqual(client().delete("/api/work/ack/job:runs:x:2026-09-18").status_code, 204)
            unack.assert_called_once_with("job:runs:x:2026-09-18")
            self.assertEqual(client().patch("/api/work/pins", json={"key": "k", "pinned": True}).status_code, 204)
            pin.assert_called_once_with("k", True)
            self.assertEqual(client().patch("/api/work/pins", json={"key": "k", "pinned": "yes"}).status_code, 422)
            self.assertEqual(client().put("/api/work/watermark", json={"ts": "x"}).json(), {"watermark": "x"})
            wm.assert_called_once_with("x")
            self.assertEqual(client().post("/api/work/dismiss", json={"key": "k", "since": "s", "more": 1}).status_code, 422)
        with patch.object(service, "dismiss", side_effect=service.WorkError("scope_unavailable", "bad", 500)):
            self.assertEqual(client().post("/api/work/dismiss", json={"key": "k"}).status_code, 500)

    def test_promote_answers_201_when_created_and_200_when_it_already_had(self) -> None:
        with patch.object(service, "promote", return_value={"created": True, "project_id": "p", "workitem_id": "w", "workitem": {}}) as promote:
            r = client().post("/api/work/promote", json={"key": "issue:p:1", "project_id": "p", "assignee": "me"})
            self.assertEqual(r.status_code, 201)
            promote.assert_called_once_with(key="issue:p:1", project_id="p", title=None, assignee="me")
        with patch.object(service, "promote", return_value={"created": False, "project_id": "p", "workitem_id": "w", "workitem": None}):
            self.assertEqual(client().post("/api/work/promote", json={"key": "issue:p:1", "project_id": "p"}).status_code, 200)
        with patch.object(service, "promote", side_effect=service.WorkError("entry_not_found", "gone", 404)):
            r = client().post("/api/work/promote", json={"key": "post:nope", "project_id": "p"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "entry_not_found"))
        self.assertEqual(client().post("/api/work/promote", json={"key": "k"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
