"""``routers/cowork_agent/bff/work.py``: the wire over ``services.work.service``.
The service is patched; these tests pin the routes, the status codes, the
strict bodies and the error mapping."""
from __future__ import annotations

import unittest
from unittest.mock import patch

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
        })
        # the section folders and their items moved to /api/inbox (docs section 18)
        self.assertEqual(client().get("/api/work/inbox/sections").status_code, 404)
        self.assertEqual(client().get("/api/work/inbox/items").status_code, 404)

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
