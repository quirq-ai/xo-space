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
from services import project_management
from services.cowork_agent.storm.chats import ChatRefused, memory, router
from services.decision_models import keys
from tests.fakes import claude
from tests.storm_comms_kit import CHOICE, CommsTestCase
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

    async def test_a_new_project_chat_is_said_on_the_stream(self) -> None:
        """UI gap #17: the chat list hears of a chat when it is made, in the
        transaction that makes it; a listing that makes none says nothing."""
        def said() -> list[tuple]:
            return self.rows("SELECT chat_id, json_extract(data, '$.kind'), json_extract(data, '$.project'), "
                             "json_extract(data, '$.created_at') FROM events WHERE type = 'chat' ORDER BY seq")
        await self.http.get("/api/chat/chats")
        (demo,) = said()
        (self.kit.projects / "fresh").mkdir()
        listed = {c["project"]: c for c in (await self.http.get("/api/chat/chats")).json()["chats"]}
        await self.http.get("/api/chat/chats")
        self.assertEqual(said(), [demo, (listed["fresh"]["id"], "project", "fresh", listed["fresh"]["created_at"])])
        self.assertEqual(demo[:3], (listed["demo"]["id"], "project", "demo"))

    async def test_a_thread_finds_its_session_however_many_the_chat_has(self) -> None:
        """By the thread's own session id, never from a capped chat-wide list."""
        chat = await self.project()
        pc_token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(pc_token, "start_thread_session", message_id=root["id"], title="Fix login",
                                  instructions="Fix it.", **CHOICE)
        with mock.patch.object(self.kit.engine, "list_sessions", side_effect=AssertionError("capped list used")):
            thread = (await self.http.get(f"/api/chat/chats/{chat['id']}/threads/{root['id']}")).json()
        self.assertEqual((thread["session_id"], thread["session"]["agent"]), (started["session_id"], "claude_code"))
        self.assertEqual(thread["runs"], {"running": 0, "waiting": 0, "done": 0, "failed": 0, "stopped": 0,
                                          "lost": 0, "total": 0, "tokens": 0})            # ch 14 §4.1

    async def test_the_listing_keeps_a_copied_project_in_its_own_chat(self) -> None:
        for name in ("app", "app-copy"):                     # `cp -r app app-copy` keeps .xo/project.json
            (self.kit.projects / name / ".xo").mkdir(parents=True)
            (self.kit.projects / name / ".xo" / "project.json").write_text(json.dumps({"pid": "pid-1"}))
        for _ in range(2):                                    # the first listing and a later one agree
            listed = {c["project"]: c for c in (await self.http.get("/api/chat/chats")).json()["chats"]}
            self.assertNotEqual(listed["app"]["id"], listed["app-copy"]["id"])
        self.assertEqual((listed["app"]["project_pid"], listed["app-copy"]["project_pid"]), ("pid-1", "app-copy"))
        self.assertEqual((await self.chats.project("app-copy"))["id"], listed["app-copy"]["id"])

    async def test_a_project_removed_through_xo_retires_its_chat(self) -> None:
        """§0.102 (owner, 2026-10-20): a new project of the same name gets a
        fresh chat; the old one is kept, read-only, with its coordinator
        retired (before, the new project inherited the old chat)."""
        (self.kit.projects / "foo").mkdir()                       # a plain folder: no pid yet
        old = await self.chats.project("foo")
        await self.owner_message(old, "secret plan for the OLD foo")
        coordinator = await self.coordinator(old)
        removed = await project_management.remove_project("foo", "foo")
        self.assertTrue(removed["removed"])
        retired = (await self.http.get(f"/api/chat/chats/{old['id']}")).json()
        self.assertIsNotNone(retired["retired_at"])
        self.assertIsNotNone(self.rows("SELECT retired_at FROM sessions WHERE id = ?", (coordinator,))[0][0])
        refused = await self.http.post(f"/api/chat/chats/{old['id']}/messages",
                                       json={"text": "more", "client_id": uuid.uuid4().hex})
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "chat_retired"))
        (self.kit.projects / "foo" / ".xo").mkdir(parents=True)   # a new project with the same name
        (self.kit.projects / "foo" / ".xo" / "project.json").write_text(json.dumps({"pid": "pid-new"}))
        listed = {c["project"]: c for c in (await self.http.get("/api/chat/chats")).json()["chats"]}
        self.assertNotEqual(listed["foo"]["id"], old["id"])
        self.assertEqual((await self.http.get(f"/api/chat/chats/{listed['foo']['id']}/messages")).json()["messages"],
                         [])
        self.assertEqual([m["text"] for m in (await self.http.get(
            f"/api/chat/chats/{old['id']}/messages")).json()["messages"]], ["secret plan for the OLD foo"])

    async def test_removing_an_unlisted_copy_leaves_the_originals_chat(self) -> None:
        """A `cp -r` copy shares its original's pid: removing the copy before
        storm listed it retires nothing of the original's."""
        for name in ("app", "app-copy"):
            (self.kit.projects / name / ".xo").mkdir(parents=True)
            (self.kit.projects / name / ".xo" / "project.json").write_text(json.dumps({"pid": "pid-1"}))
        original = await self.chats.project("app")
        await project_management.remove_project("app-copy", "app-copy")
        self.assertIsNone((await self.http.get(f"/api/chat/chats/{original['id']}")).json()["retired_at"])
        self.assertEqual((await self.chats.project("app"))["id"], original["id"])

    async def test_a_retired_chat_starts_no_coordinator_and_its_items_are_withdrawn(self) -> None:
        (self.kit.projects / "foo").mkdir()
        chat = await self.chats.project("foo")
        pc_token = await self.token(await self.coordinator(chat))
        asked = await self.call(pc_token, "ask_owner", kind="question", title="Which colour?", why="For the theme")
        await project_management.remove_project("foo", "foo")
        self.assertEqual(self.rows("SELECT state FROM review_items WHERE id = ?", (asked["review_id"],)),
                         [("withdrawn",)])
        with self.assertRaises(ChatRefused) as refused:
            await self.chats.with_coordinator(chat, lambda w, session_id: session_id)   # e.g. a routing in flight
        self.assertEqual(refused.exception.code, "chat_retired")
        self.assertEqual(self.rows("SELECT count(*) FROM sessions WHERE chat_id = ? AND retired_at IS NULL",
                                   (chat["id"],)), [(0,)])

    async def test_a_stop_that_fails_still_releases_every_session_of_a_removed_project(self) -> None:
        (self.kit.projects / "foo").mkdir()
        chat = await self.chats.project("foo")
        pc = await self.coordinator(chat)
        root = await self.owner_message(chat, "Fix it")
        thread = (await self.call(await self.token(pc), "start_thread_session", message_id=root["id"], title="Fix",
                                  instructions="x", **CHOICE))["session_id"]

        async def stop(session_id, *, by):
            if session_id == pc:
                raise RuntimeError("the CLI didn't answer")
        released = []
        with mock.patch.object(self.kit.engine, "stop", side_effect=stop), \
                mock.patch.object(self.kit.engine, "retired", side_effect=released.append), \
                self.assertLogs("services.cowork_agent.storm.chats", "WARNING"):
            await project_management.remove_project("foo", "foo")
        self.assertEqual(sorted(released), sorted([pc, thread]))

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
        further = (await self.http.get(f"/api/chat/chats/{chat['id']}/messages",
                                       params={"before_seq": before["next_before_seq"], "limit": 2})).json()
        self.assertEqual(([m["text"] for m in further["messages"]], further["next_before_seq"]),
                         (["m0", "m1"], further["messages"][0]["seq"]))
        self.assertEqual((await self.http.get(f"/api/chat/chats/{chat['id']}/hops")).json(),
                         {"hops": [], "next_after_seq": None})
        pc_token = await self.token(await self.coordinator(chat))
        for n in range(3):                                   # three status-free hops: the coordinator asks
            await self.call(pc_token, "ask_workspace", question=f"q{n}")
        hops = await self.http.get(f"/api/chat/chats/{(await self.chats.workspace())['id']}/hops",
                                   params={"before_seq": 10 ** 9, "limit": 2})
        self.assertEqual([h["note"] for h in hops.json()["hops"]], ["q1", "q2"])
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
            # The routing starts the thread, then settles the decision in a
            # commit of its own: wait for the routing to end, not the thread.
            routing = f"storm-chats:route:{said['id']}"
            await self.settle(lambda: not any(t.get_name() == routing for t in self.chats._tasks))
        title, = self.rows("SELECT title FROM threads")[0]
        self.assertEqual(title, "Add a CSV export")
        decided_by, route = self.rows("SELECT decided_by, route FROM router_decisions WHERE message_id = ?",
                                      (said["id"],))[0]
        self.assertEqual((decided_by, [a["kind"] for a in json.loads(route)["actions"]]), ("jev", ["start_thread"]))
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
    async def test_a_coordinator_that_cant_start_leaves_the_person_a_note(self) -> None:
        await self.boot()
        workspace = await self.chats.workspace()
        ws_token = await self.token(await self.coordinator(workspace))
        said = await self.owner_message(workspace, "Build a public status page")
        proposed = await self.call(ws_token, "propose_project", name="status-page", purpose="A public status page",
                                   why_new="No project serves it", context_message_ids=[said["id"]])
        refused = ChatRefused("auth_required", "claude_code can't run: it isn't logged in.", status=409)
        with mock.patch.object(type(self.chats), "runtime_for", side_effect=refused), \
                self.assertLogs("services.cowork_agent.storm.chats", "ERROR"):
            await self.queue.decide(proposed["review_id"], _request("allow"))
        self.assertTrue((self.kit.projects / "status-page").is_dir())
        self.assertIn(("system", "Project status-page created, but its coordinator couldn't start (claude_code can't "
                                 "run: it isn't logged in), so the request didn't reach it. Send it in the project's "
                                 "chat once that is fixed."),
                      self.rows("SELECT author_kind, text FROM messages WHERE chat_id = ?", (workspace["id"],)))

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
                                  instructions="x", **CHOICE)
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

    async def test_a_paused_coordinator_resumes_by_the_persons_timeline_message_and_its_retry(self) -> None:
        """No thread to resume it and no one above it to start a fresh one:
        the person's message on the timeline lifts the pause, and a retry
        does while the message still waits to run."""
        await self.boot()
        chat = await self.project()
        coordinator = await self.coordinator(chat)
        paused = lambda: self.rows("SELECT paused FROM sessions WHERE id = ?", (coordinator,))[0][0]  # noqa: E731
        path = f"/api/chat/chats/{chat['id']}/messages"
        await self.kit.engine.pause(coordinator, reason="paused by the person after repeated denials")
        said = (await self.http.post(path, json={"text": "Are you there?", "client_id": "t1"})).json()
        self.assertIsNone(paused())
        self.assertEqual(self.rows("SELECT session_id, state FROM wakes WHERE id = ?", (f"msg:{said['id']}",)),
                         [(coordinator, "queued")])
        await self.kit.engine.pause(coordinator, reason="stuck on a denied step")
        self.assertEqual((await self.http.post(path, json={"text": "Are you there?", "client_id": "t1"})).json()["id"],
                         said["id"])
        self.assertIsNone(paused())

    async def _until_state(self, state: str) -> None:
        await self.kit.until(lambda: self.rows("SELECT state FROM threads") == [(state,)])


def _request(decision: str, **kw):
    from services.cowork_agent.storm.review.decisions import Request
    return Request(decision=decision, idempotency_key=uuid.uuid4().hex, **kw)
