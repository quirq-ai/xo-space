"""Storm's chats beyond the PoC flows (design ch 11, ch 17 §2.2, §2.6): the
routes' shapes, the router's setting and decision models, a project made on
the person's Allow, new projects announced, pins, pauses and memory."""

from __future__ import annotations

import json
import os
import stat
import uuid
from unittest import mock

import services.decision_models as dm
from services.cowork_agent.storm.chats import memory, router
from services.decision_models import keys
from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase
from tests.test_storm_chats_flows import PC, FlowTestCase, done, start


def _choice(answer_id: str, chosen: str, p: float) -> dm.Answer:
    return dm.Answer(answer_id, "choice", chosen=chosen, probability=p, probabilities={chosen: p})


class ChatRouteTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()

    async def test_listing_takes_existing_projects_in_silently_and_announces_new_ones(self) -> None:
        listed = (await self.http.get("/api/chat/chats")).json()["chats"]
        self.assertEqual([(c["kind"], c["project"]) for c in listed], [("workspace", None), ("project", "demo")])
        workspace = listed[0]["id"]
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(0,)])
        (self.kit.projects / "fresh").mkdir()
        listed = (await self.http.get("/api/chat/chats")).json()["chats"]
        self.assertEqual(len(listed), 3)
        self.assertEqual(self.rows("SELECT chat_id, author_kind, text FROM messages"),
                         [(workspace, "system", "Project fresh created.")])
        await self.http.get("/api/chat/chats")
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(1,)])           # once

    async def test_messages_and_hops_page_by_seq(self) -> None:
        chat = await self.project()
        for n in range(5):
            await self.owner_message(chat, f"m{n}")
        first = (await self.http.get(f"/api/chat/chats/{chat['id']}/messages", params={"limit": 2})).json()
        self.assertEqual([m["text"] for m in first["messages"]], ["m0", "m1"])
        rest = (await self.http.get(f"/api/chat/chats/{chat['id']}/messages",
                                    params={"after_seq": first["next_after_seq"]})).json()
        self.assertEqual([m["text"] for m in rest["messages"]], ["m2", "m3", "m4"])
        before = (await self.http.get(f"/api/chat/chats/{chat['id']}/messages",
                                      params={"before_seq": rest["messages"][-1]["seq"], "limit": 2})).json()
        self.assertEqual([m["text"] for m in before["messages"]], ["m2", "m3"])
        self.assertEqual((await self.http.get(f"/api/chat/chats/{chat['id']}/hops")).json(),
                         {"hops": [], "next_after_seq": None})
        self.assertEqual((await self.http.get("/api/chat/chats/chat_nope/messages")).status_code, 404)

    async def test_a_pin_is_checked_against_the_catalog_and_kept(self) -> None:
        chat = await self.project()
        self.kit.scenario(claude.initialize())             # the model-free models probe
        bad = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"model": "gpt-9"})
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertEqual(bad.json()["detail"]["code"], "unsupported_model")
        pinned = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"model": "haiku", "effort": "low"})
        self.assertEqual(pinned.json()["pin"], {"agent": None, "model": "haiku", "effort": "low"})
        unknown = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"bogus": 1})
        self.assertEqual(unknown.status_code, 422)
        routed = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"routing": {"mode": "off"}})
        self.assertEqual(routed.json()["routing"], {"mode": "off", "decision_model": None})
        cleared = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"routing": None})
        self.assertIsNone(cleared.json()["routing"])

    async def test_memory_is_read_only_through_the_api(self) -> None:
        chat = await self.project()
        memory.write(chat["id"], "MEMORY.md", "- [Style](style.md) — terse", if_version="new")
        files = (await self.http.get(f"/api/chat/chats/{chat['id']}/memory")).json()["files"]
        self.assertEqual([f["path"] for f in files], ["MEMORY.md"])
        read = (await self.http.get(f"/api/chat/chats/{chat['id']}/memory/MEMORY.md")).json()
        self.assertEqual(read["content"], "- [Style](style.md) — terse")
        self.assertEqual((await self.http.get(f"/api/chat/chats/{chat['id']}/memory/nope.md")).status_code, 404)
        self.assertEqual((await self.http.put(f"/api/chat/chats/{chat['id']}/memory/MEMORY.md")).status_code, 405)


class RouterTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()

    async def test_on_by_default_and_a_decision_model_must_be_connected_first(self) -> None:
        self.assertEqual((await self.http.get("/api/chat/router")).json(),
                         {"mode": "agent", "decision_model": None, "abstain_below": 0.5})
        refused = await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        self.assertEqual(refused.json()["detail"]["code"], "model_not_connected")
        connected = await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "ts-key-123"})
        self.assertEqual(connected.status_code, 200, connected.text)
        jev = next(m for m in connected.json()["models"] if m["provider"] == "jev")
        self.assertEqual((jev["connected"], jev["model"]), (True, "jev-latest"))
        self.assertNotIn("ts-key-123", connected.text)
        self.assertEqual(stat.S_IMODE(os.stat(keys.path()).st_mode), 0o600)
        self.assertEqual(keys.path().read_text(), "TYPESAFE_API_KEY=ts-key-123\n")
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)
        chosen = await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        self.assertEqual(chosen.json()["mode"], "decision")
        gone = await self.http.delete("/api/chat/router/models/jev")
        self.assertFalse(next(m for m in gone.json()["models"] if m["provider"] == "jev")["connected"])
        self.assertEqual((await self.http.get("/api/chat/router")).json()["mode"], "agent")    # falls back
        self.assertEqual(keys.path().read_text(), "")
        self.assertEqual((await self.http.delete("/api/chat/router/models/nope")).status_code, 404)


class DecisionRoutingTests(FlowTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}),
                                    *done("pc")]})
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        self.ids = await self.chat_ids()

    async def test_a_confident_new_task_starts_a_thread_on_the_coordinators_behalf(self) -> None:
        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            self.assertEqual((provider, model), ("jev", "jev-latest"))
            return dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.93)})
        with mock.patch.object(dm, "ask", ask):
            said = await self.post(self.ids["demo"], "Add a CSV export. It should stream.")
            await self.settle(lambda: self.rows("SELECT count(*) FROM threads")[0][0] == 1)
        title, = self.rows("SELECT title FROM threads")[0]
        self.assertEqual(title, "Add a CSV export")
        decided_by, route = self.rows("SELECT decided_by, route FROM router_decisions WHERE message_id = ?",
                                      (said["id"],))[0]
        self.assertEqual((decided_by, json.loads(route)["actions"]), ("jev", [{"kind": "start_thread"}]))
        brief = self.rows("SELECT note FROM hops WHERE kind = 'brief'")[0][0]
        self.assertIn("Routed by jev", brief)
        coordinator = self.rows("SELECT id FROM sessions WHERE role = 'project-coordinator'")[0][0]
        self.assertFalse([w for w in self.rows("SELECT id FROM wakes WHERE session_id = ?", (coordinator,))
                          if w[0] == f"msg:{said['id']}"])                     # the agent wasn't asked

    async def test_an_unsure_model_hands_the_message_to_the_coordinator_with_why(self) -> None:
        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            return dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.2)})
        with mock.patch.object(dm, "ask", ask):
            said = await self.post(self.ids["demo"], "hmm")
            await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id = ?", (f"msg:{said['id']}",))
                              [0][0] == 1)
        text = self.rows("SELECT envelope FROM wakes WHERE id = ?", (f"msg:{said['id']}",))[0][0]
        self.assertIn("The jev decision model handed this message to you (unsure what the message asks for)", text)
        route = json.loads(self.rows("SELECT route FROM router_decisions")[0][0])
        self.assertEqual(route["escalated_from"], "jev")


class ProjectCreationTests(CommsTestCase):
    async def test_the_persons_allow_creates_the_project_and_hands_it_the_request(self) -> None:
        await self.boot()
        workspace = await self.chats.workspace()
        ws_token = await self.token(await self.coordinator(workspace))
        said = await self.owner_message(workspace, "Build a public status page")
        proposed = await self.call(ws_token, "propose_project", name="status-page", purpose="A public status page",
                                   why_new="No project serves it", context_message_ids=[said["id"]],
                                   first_task="Start with the uptime chart.")
        decided = await self.queue.decide(proposed["review_id"], _request("allow", answer="status-board"))
        self.assertEqual(decided["state"], "decided")
        self.assertTrue((self.kit.projects / "status-board" / ".xo" / "project.json").is_file())
        meta = json.loads((self.kit.projects / "status-board" / ".xo" / "project.json").read_text())
        self.assertEqual(meta["description"], "A public status page")
        self.assertIn(("system", "Project status-board created."),
                      self.rows("SELECT author_kind, text FROM messages WHERE chat_id = ?", (workspace["id"],)))
        chat = await self.chats.project("status-board")
        copies = self.rows("SELECT text, extra FROM messages WHERE chat_id = ?", (chat["id"],))
        self.assertEqual([(t, json.loads(e)["forwarded_from"]["message_id"]) for t, e in copies],
                         [("Build a public status page", said["id"])])
        coordinator = self.rows("SELECT id FROM sessions WHERE chat_id = ? AND role = 'project-coordinator'",
                                (chat["id"],))[0][0]
        [(wake_id, _priority, text)] = self.wakes(coordinator)
        self.assertIn("Start with the uptime chart.", text)
        self.assertIn("Build a public status page", text)

    async def test_a_decision_model_suggests_the_project_that_already_owns_the_work(self) -> None:
        await self.boot()
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        workspace = await self.chats.workspace()
        ws_token = await self.token(await self.coordinator(workspace))
        said = await self.owner_message(workspace, "Add a status page for demo's API")

        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            self.assertIn("demo", questions[0].options)
            return dm.Result("jev", {router.OWNER_ID: _choice(router.OWNER_ID, "demo", 0.88)})
        with mock.patch.object(dm, "ask", ask):
            proposed = await self.call(ws_token, "propose_project", name="status-page", purpose="A status page",
                                       why_new="new", context_message_ids=[said["id"]])
        detail = json.loads(self.rows("SELECT detail FROM review_items WHERE id = ?", (proposed["review_id"],))[0][0])
        self.assertEqual(detail["suggestion"], {"forward_to": "demo", "probability": 0.88, "by": "jev"})


class PauseTests(CommsTestCase):
    async def test_a_paused_thread_resumes_by_the_person_and_by_their_reply(self) -> None:
        await self.boot()
        chat = await self.project()
        pc_token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Do it")
        started = await self.call(pc_token, "start_thread_session", message_id=root["id"], title="T",
                                  instructions="x")
        session = started["session_id"]
        await self.kit.engine.pause(session, reason="stuck on a denied step")
        await self._until_state("paused")
        path = f"/api/chat/chats/{chat['id']}/threads/{root['id']}"
        self.assertEqual((await self.http.patch(path, json={"paused": True})).status_code, 400)
        resumed = await self.http.patch(path, json={"paused": False})
        self.assertEqual(resumed.json()["state"], "working")
        await self._until_state("working")
        self.assertIsNone(self.rows("SELECT paused FROM sessions WHERE id = ?", (session,))[0][0])
        await self.kit.engine.pause(session, reason="stuck again")
        await self._until_state("paused")
        await self.http.post(f"/api/chat/chats/{chat['id']}/messages",
                             json={"text": "try another way", "thread_ts": root["id"], "client_id": "r1"})
        await self._until_state("working")
        self.assertIsNone(self.rows("SELECT paused FROM sessions WHERE id = ?", (session,))[0][0])

    async def _until_state(self, state: str) -> None:
        await self.kit.until(lambda: self.rows("SELECT state FROM threads") == [(state,)])


def _request(decision: str, **kw):
    from services.cowork_agent.storm.review.decisions import Request
    return Request(decision=decision, idempotency_key=uuid.uuid4().hex, **kw)
