"""The /api/brain/* routes and the Inbox's brain feeder, through the real
service in a sandboxed state root."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import brain as brain_routes
from services.brain import store
from services.inbox import feeders
from services.inbox import service as inbox_service
from services.inbox import store as inbox_store

from tests._brain_support import ALPHA_README, BETA_NOTES, NOW, BrainSandbox


def app(local: bool = True) -> FastAPI:
    a = FastAPI()
    a.include_router(brain_routes.router)
    if local:
        a.dependency_overrides[brain_routes._require_local] = lambda: None
    return a


class RouteTests(BrainSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.project("alpha", {"README.md": ALPHA_README}, pid="pid-alpha")
        self.project("beta", {"NOTES.md": BETA_NOTES}, pid="pid-beta")
        self.project("fresh", {"a.md": "# Fresh\n"}).joinpath(".xo", "project.json").write_text("{}")

    def wait_ready(self, c: TestClient, *ids: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            sources = {s["id"]: s for s in c.get("/api/brain/sources").json()["sources"]}
            if all(sources.get(i, {}).get("status") == "ready" and not sources[i]["learning"] for i in ids):
                return sources
            time.sleep(0.05)
        self.fail(f"sources never became ready: {sources}")

    def test_learn_then_recall_over_http(self) -> None:
        with TestClient(app()) as c:
            listing = c.get("/api/brain/sources").json()
            self.assertEqual(listing["sources"], [])
            self.assertEqual({p["project"]: p["pid"] for p in listing["projects"]},
                             {"alpha": "pid-alpha", "beta": "pid-beta", "fresh": None})
            for name in ("alpha", "beta"):
                r = c.post("/api/brain/sources", json={"project": name})
                self.assertEqual(r.status_code, 201, r.text)
            r = c.post("/api/brain/learn", json={})
            self.assertEqual(r.status_code, 202)
            self.assertEqual(sorted(r.json()["started"]), ["pid-alpha", "pid-beta"])
            sources = self.wait_ready(c, "pid-alpha", "pid-beta")
            self.assertGreater(sources["pid-beta"]["stats"]["shared"], 0)

            status = c.get("/api/brain/status").json()
            self.assertEqual(status["model"], {"name": "none", "reason": False, "embed": False})
            self.assertGreater(status["counts"]["shared_pieces"], 0)

            r = c.post("/api/brain/recall", json={"cue": "spreading activation", "context_source": "pid-beta"})
            self.assertEqual(r.status_code, 200)
            top = r.json()["results"][0]
            self.assertEqual(top["note"]["source_id"], "pid-beta")
            piece = c.get(f"/api/brain/pieces/{top['piece']['id']}").json()
            self.assertEqual({n["source_id"] for n in piece["notes"]}, {"pid-alpha", "pid-beta"})
            self.assertTrue(piece["evidence"])
            self.assertEqual(c.get("/api/brain/pieces/999999").status_code, 404)
            self.assertEqual(c.get("/api/brain/pieces?shared=true").status_code, 200)
            self.assertTrue(c.get(f"/api/brain/pieces/{top['piece']['id']}/similar").json() is not None)
            self.assertEqual(c.get("/api/brain/graph").status_code, 200)
            self.assertEqual(c.post("/api/brain/discover").status_code, 200)
            for path in ("patterns", "analogies", "hypotheses", "relations", "runs", "findings", "goals", "experiences"):
                with self.subTest(path=path):
                    self.assertEqual(c.get(f"/api/brain/{path}").status_code, 200)
            self.assertEqual(c.delete("/api/brain/sources/pid-alpha").status_code, 200)
            self.assertEqual(c.delete("/api/brain/sources/pid-alpha").status_code, 404)

    def test_a_project_without_an_id_or_folder_is_refused(self) -> None:
        with TestClient(app()) as c:
            r = c.post("/api/brain/sources", json={"project": "fresh"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "project_not_ready"))
            r = c.post("/api/brain/sources", json={"project": "nope"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "project_not_found"))
            r = c.post("/api/brain/learn", json={"source_id": "pid-alpha"})
            self.assertEqual(r.status_code, 404)

    def test_bodies_are_strict_and_values_checked(self) -> None:
        with TestClient(app()) as c:
            self.assertEqual(c.post("/api/brain/recall", json={"cue": "x", "extra": 1}).status_code, 422)
            self.assertEqual(c.post("/api/brain/recall", json={}).status_code, 422)
            self.assertEqual(c.post("/api/brain/recall", json={"cue": "x", "hops": 9}).status_code, 422)
            self.assertEqual(c.post("/api/brain/recall", json={"cue": "x", "answer": True}).status_code, 200)
            self.assertEqual(c.post("/api/brain/recall", json={"cue": "x", "answer": "yes please"}).status_code, 422)
            r = c.post("/api/brain/recall", json={"cue": "   "})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "invalid_value"))
            r = c.post("/api/brain/recall", json={"cue": "x", "context_source": "../etc"})
            self.assertEqual(r.status_code, 404)
            self.assertEqual(c.post("/api/brain/use", json={"piece_ids": [1]}).status_code, 400)
            self.assertEqual(c.post("/api/brain/use", json={"piece_ids": ["1", "2"]}).status_code, 422)
            self.assertEqual(c.get("/api/brain/findings?status=bogus").status_code, 400)
            self.assertEqual(c.patch("/api/brain/findings/1", json={"status": "later"}).status_code, 400)
            self.assertEqual(c.patch("/api/brain/findings/999", json={"status": "done"}).status_code, 404)
            r = c.post("/api/brain/experiences", json={"goal": "g", "result": "meh"})
            self.assertEqual(r.status_code, 400)
            r = c.post("/api/brain/experiences", json={"goal": "g", "result": "worked", "lessons": "l"})
            self.assertEqual((r.status_code, r.json()["result"]), (201, "worked"))

    def test_designing_without_a_model_is_501(self) -> None:
        with TestClient(app()) as c:
            r = c.post("/api/brain/goals", json={"goal": "build a search"})
            self.assertEqual((r.status_code, r.json()["detail"]["code"]), (501, "model_required"))
            self.assertEqual(c.post("/api/brain/designs/1/approve").status_code, 404)
            self.assertEqual(c.post("/api/brain/designs/1/build").status_code, 404)

    def test_actions_that_run_programs_or_approve_need_a_local_client(self) -> None:
        with TestClient(app(local=False)) as c:     # TestClient's peer is "testclient", not loopback
            for method, path, body in (("post", "/api/brain/sources", {"project": "alpha"}),
                                       ("delete", "/api/brain/sources/pid-alpha", None),
                                       ("post", "/api/brain/learn", {}), ("post", "/api/brain/discover", None),
                                       ("post", "/api/brain/designs/1/approve", None),
                                       ("post", "/api/brain/designs/1/build", None)):
                with self.subTest(path=path):
                    r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                    self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "local_only"))
            self.assertEqual(c.post("/api/brain/recall", json={"cue": "x"}).status_code, 200)


class InboxFeederTests(BrainSandbox):
    def test_open_findings_become_inbox_items_and_close_with_them(self) -> None:
        self.alpha_beta()
        with store.write() as conn:
            store.upsert_finding(conn, kind="pattern", ref="7", title="Recurring pattern: queue · worker",
                                 body="Appears in alpha, beta.", source_id="pid-alpha", now=NOW)
            store.upsert_finding(conn, kind="gap", ref="k8s|", title="Missing knowledge: k8s", now=NOW)
        doc = inbox_store.normalize_document({})
        result = feeders.brain(doc)
        [item] = result.items                       # a gap asked once is not worth an item yet
        self.assertEqual((item["source"], item["kind"], item["key"], item["project_id"], item["link"]),
                         ("brain", "brain.pattern", "brain:pattern:7", "alpha", {"view": "brain"}))
        self.assertEqual(result.watched.key_prefix, "brain:")

        inbox_service._reset_throttle()
        inbox_service.refresh(force=True)
        items = {i["key"]: i for i in inbox_store.load_document()[0]["items"]}
        self.assertEqual(items["brain:pattern:7"]["status"], "new")
        with store.write() as conn:
            conn.execute("UPDATE findings SET status = 'done' WHERE kind = 'pattern'")
        inbox_service.refresh(force=True)
        items = {i["key"]: i for i in inbox_store.load_document()[0]["items"]}
        self.assertEqual((items["brain:pattern:7"]["status"], items["brain:pattern:7"].get("auto_closed")),
                         ("done", True))

    def test_no_brain_store_means_no_items_and_no_file(self) -> None:
        self.assertEqual(feeders.brain(inbox_store.normalize_document({})).items, [])
        self.assertFalse(store.db_path().exists())
