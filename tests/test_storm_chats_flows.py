"""The PoC's flows on the fake claude CLI, through storm's routes and the real
``xo_comms`` endpoint (design ch 11 §12; ch 18 §8: "workspace → project →
thread, the reply gate, resolve/reopen, hops"). Each session's agent is a
fake playing its role's script; it reads its wake, and acts only through
comms calls over HTTP, as the real CLI does.
"""

from __future__ import annotations

import json
import uuid

from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase

WS = "You are the workspace coordinator"
PC = "You are the coordinator of one project"
TH = "You are a thread"
#: The id of the message a wake is about (``<message trigger="true" … id="cmsg_…">``).
TRIGGER = r'trigger="true"[^>]*? id="(cmsg_[0-9a-f]+)"'
TEXT = "message.content.0.text"


def start(sid: str, *, bind: str | None = None) -> list[dict]:
    """A process starting on its first input (binding the triggering message id)."""
    return [*claude.initialize(), _input(bind), claude.init(sid)]


def _input(bind: str | None = None) -> dict:
    step: dict = {"expect": {"type": "user"}}
    if bind:
        step["bind_re"] = {bind: [TEXT, TRIGGER]}
    return step


def next_input(*, bind: str | None = None) -> dict:
    return _input(bind)


def done(sid: str, n: int = 1) -> list[dict]:
    return [*claude.text_turn(sid, "ok", message_id=f"msg_{sid}_{n}"), claude.idle(sid)]


class FlowTestCase(CommsTestCase):
    async def post(self, chat_id: str, text: str, **body) -> dict:
        response = await self.http.post(f"/api/chat/chats/{chat_id}/messages",
                                        json={"text": text, "client_id": uuid.uuid4().hex, **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def settle(self, predicate, timeout: float = 20.0) -> None:
        async with self.kit.pacing():
            await self.kit.until(predicate, timeout)

    async def chat_ids(self) -> dict[str, str]:
        chats = (await self.http.get("/api/chat/chats")).json()["chats"]
        return {c["project"] or "workspace": c["id"] for c in chats}

    def texts(self, chat_id: str, thread_ts: str | None = None) -> list[tuple[str, str]]:
        return self.rows("SELECT author_kind, text FROM messages WHERE chat_id = ? AND thread_ts IS ? ORDER BY seq",
                         (chat_id, thread_ts))

    def hops(self) -> list[tuple]:
        return [(k, f, t, json.loads(v), s) for k, f, t, v, s in self.rows(
            "SELECT kind, from_role, to_role, via, state FROM hops ORDER BY seq")]


class WorkspaceFlowTests(FlowTestCase):
    async def test_the_workspace_answers_in_place(self) -> None:
        await self.boot_roles({WS: [*start("ws"), claude.mcp_call("post_message", {"text": "Hi! Two projects."}),
                                    *done("ws")]})
        ids = await self.chat_ids()
        said = await self.post(ids["workspace"], "Hello, what's here?")
        await self.settle(lambda: len(self.texts(ids["workspace"])) == 2)
        self.assertEqual(self.texts(ids["workspace"]), [("owner", "Hello, what's here?"),
                                                        ("agent", "Hi! Two projects.")])
        await self.settle(lambda: self.rows("SELECT count(*) FROM router_decisions")[0][0] == 1)
        decided_by, route = self.rows("SELECT decided_by, route FROM router_decisions WHERE message_id = ?",
                                      (said["id"],))[0]
        self.assertEqual((decided_by, json.loads(route)["actions"][0]["kind"]), ("agent", "answer"))
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])

    async def test_workspace_to_project_to_thread_with_every_hop_recorded(self) -> None:
        await self.boot_roles({
            WS: [*start("ws", bind="MID"),
                 claude.mcp_call("forward_to_project", {"project": "demo", "context_message_ids": ["$MID"]}),
                 *done("ws")],
            PC: [*start("pc", bind="COPY"),
                 claude.mcp_call("start_thread_session", {"message_id": "$COPY", "title": "Dark mode",
                                                          "instructions": "Use the existing theme tokens.",
                                                          "project_ack": "On it."}),
                 *done("pc"),
                 next_input(), claude.mcp_call("no_reply_needed", {"reason": "the result stays in the thread"}),
                 *done("pc", 2)],
            TH: [*start("th"), claude.mcp_call("update_status", {"text": "✱ Add dark mode"}),
                 claude.mcp_call("reply", {"text": "Done: dark mode added."}),
                 claude.mcp_call("update_status", {"text": "✓ Add dark mode"}), *done("th")],
        })
        ids = await self.chat_ids()
        await self.post(ids["workspace"], "In demo, add dark mode")
        await self.settle(lambda: self.rows("SELECT count(*) FROM hops WHERE kind = 'status' AND state = 'delivered'")
                          [0][0] == 1)
        self.assertEqual(self.hops(), [
            ("forward", "workspace-coordinator", "project-coordinator", ["workspace"], "delivered"),
            ("brief", "project-coordinator", "thread", ["workspace", "project"], "answered"),   # by its status
            ("status", "thread", "project-coordinator", ["thread"], "delivered")])
        [(ts, state, title)] = self.rows("SELECT ts, state, title FROM threads")
        self.assertEqual((state, title), ("done", "Dark mode"))
        self.assertEqual(self.texts(ids["demo"], ts), [
            ("system", "This project isn't a git repository, so this thread works in the project folder directly."),
            ("agent", "Done: dark mode added.")])
        card = (await self.http.get(f"/api/chat/chats/{ids['demo']}/threads/{ts}")).json()["card"]
        self.assertEqual((card["state"], card["happened"]), ("done", "Done: dark mode added."))
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])

    async def test_fan_out_and_no_such_project(self) -> None:
        await self.boot_roles({
            WS: [*start("ws", bind="MID"),
                 claude.mcp_call("forward_to_project", {"project": "demo", "context_message_ids": ["$MID"],
                                                        "note": "The UI part."}),
                 claude.mcp_call("forward_to_project", {"project": "api", "context_message_ids": ["$MID"],
                                                        "note": "The endpoint part."}),
                 *done("ws"),
                 next_input(), claude.mcp_call("no_such_project", {"text": "There's no project called billing."}),
                 *done("ws", 2)],
            PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}), *done("pc")],
        })
        (self.kit.projects / "api").mkdir()
        ids = await self.chat_ids()
        await self.post(ids["workspace"], "Add login to the UI and the API")
        await self.settle(lambda: len(self.rows("SELECT id FROM hops WHERE kind = 'forward'")) == 2)
        copies = self.rows("SELECT chat_id FROM messages WHERE json_extract(extra, '$.forwarded_from') IS NOT NULL")
        self.assertEqual(sorted(c for (c,) in copies), sorted([ids["demo"], ids["api"]]))
        await self.post(ids["workspace"], "What's the status of billing?")
        await self.settle(lambda: ("agent", "There's no project called billing.") in self.texts(ids["workspace"]))


class GateTests(FlowTestCase):
    async def test_a_silent_input_is_gated_once_and_then_answers(self) -> None:
        await self.boot_roles({PC: [*start("pc"), *done("pc"),
                                    next_input(), claude.mcp_call("post_message", {"text": "Sorry: done now."}),
                                    *done("pc", 2)]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Is the build green?")
        await self.settle(lambda: ("agent", "Sorry: done now.") in self.texts(ids["demo"]))
        gate = self.rows("SELECT envelope, priority FROM wakes WHERE id LIKE 'gate:%'")
        self.assertEqual(len(gate), 1)
        self.assertIn("post_message", gate[0][0])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'turn.unanswered'"), [(0,)])

    async def test_silence_after_the_gate_is_recorded_unanswered_and_the_thread_card_fails(self) -> None:
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "T", "instructions": "x"}),
                 *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2)],
            TH: [*start("th"), *done("th"), next_input(), *done("th", 2)],
        })
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Do the thing")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'turn.unanswered'")[0][0] == 1)
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("failed",)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(1,)])


class SecondThreadFlowTests(FlowTestCase):
    async def test_a_second_thread_in_the_same_project_lives_on_its_own(self) -> None:
        """ch 11 §12's "second thread": a new task while the first thread
        exists gets its own thread and session; each is carded, hopped and
        replied on its own line."""
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "Fix login",
                                                          "instructions": "x"}), *done("pc"),
                 next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2),
                 next_input(bind="MID2"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID2", "title": "Dark mode",
                                                          "instructions": "y"}), *done("pc", 3),
                 next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 4)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Fixed."}), *done("th")]})
        ids = await self.chat_ids()
        first = await self.post(ids["demo"], "Fix the login page")
        pc = self.rows("SELECT id FROM sessions WHERE role = 'project-coordinator'")[0][0]
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (pc,))[0][0] == 2)      # its status input ended
        second = await self.post(ids["demo"], "Add a dark mode")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (pc,))[0][0] == 4)
        threads = dict(self.rows("SELECT ts, session_id FROM threads"))
        self.assertEqual(set(threads), {first["id"], second["id"]})
        self.assertNotEqual(threads[first["id"]], threads[second["id"]])
        for ts, session in threads.items():
            with self.subTest(thread=ts):
                self.assertEqual(self.texts(ids["demo"], ts)[-1], ("agent", "Fixed."))
                self.assertEqual(self.rows("SELECT author_session_id FROM messages WHERE thread_ts = ? AND "
                                           "author_kind = 'agent'", (ts,)), [(session,)])
                card = json.loads(self.rows("SELECT card FROM cards WHERE thread_ts = ? ORDER BY seq DESC",
                                            (ts,))[0][0])
                self.assertEqual((card["state"], card["happened"]), ("done", "Fixed."))
                self.assertEqual(self.rows("SELECT kind, to_session FROM hops WHERE thread_ts = ? AND kind = 'brief'",
                                           (ts,)), [("brief", session)])
                self.assertEqual(self.rows("SELECT from_session, state FROM hops WHERE thread_ts = ? AND "
                                           "kind = 'status'", (ts,)), [(session, "delivered")])


class ThreadFlowTests(FlowTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "Fix login",
                                                          "instructions": "x"}),
                 *done("pc"),
                 next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2),
                 next_input(bind="FOLLOW"),
                 claude.mcp_call("message_thread", {"thread_id": "$MID", "context_message_ids": ["$FOLLOW"]}),
                 claude.mcp_call("post_message", {"text": "Sent to the thread."}), *done("pc", 3)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Fixed."}), *done("th"),
                 next_input(), claude.mcp_call("reply", {"text": "Done too."}), *done("th", 2)]})
        self.ids = await self.chat_ids()

    async def thread_started(self) -> str:
        await self.post(self.ids["demo"], "Fix the login page")
        await self.settle(lambda: len(self.rows("SELECT ts FROM threads")) == 1)
        return self.rows("SELECT ts FROM threads")[0][0]

    async def test_the_person_resolves_reopens_and_a_reply_reopens(self) -> None:
        ts = await self.thread_started()
        path = f"/api/chat/chats/{self.ids['demo']}/threads/{ts}"
        wakes_before = self.rows("SELECT count(*) FROM wakes")[0][0]
        for _ in range(2):                                       # idempotent: the UI's Undo is a second PATCH
            resolved = await self.http.patch(path, json={"resolved": True})
            self.assertEqual(resolved.json()["state"], "resolved")
        self.assertEqual(self.rows("SELECT count(*) FROM wakes")[0][0], wakes_before)      # no turn started
        self.assertEqual((await self.http.patch(path, json={"resolved": False})).json()["state"], "working")
        await self.http.patch(path, json={"resolved": True})
        reply = await self.post(self.ids["demo"], "One more thing: keep the old URL working", thread_ts=ts)
        self.assertEqual(self.rows("SELECT state, resolved_at FROM threads"), [("working", None)])
        session = self.rows("SELECT session_id FROM threads")[0][0]
        self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"msg:{reply['id']}",)), [(session,)])
        self.assertEqual((await self.http.patch(path, json={"intelligence": {"agent": "other"}})).status_code, 409)

    async def test_a_follow_up_on_the_timeline_reaches_the_thread_through_the_coordinator(self) -> None:
        ts = await self.thread_started()
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("done",)])
        follow = await self.post(self.ids["demo"], "Also keep the old URL working")
        await self.settle(lambda: ("agent", "Done too.") in self.texts(self.ids["demo"], ts))
        kind, cause, state = self.rows("SELECT kind, cause, state FROM hops WHERE kind = 'relay'")[0]
        self.assertEqual((kind, cause), ("relay", follow["id"]))
        self.assertIn(state, ("delivered", "answered"))
        self.assertIn(("agent", "Sent to the thread."), self.texts(self.ids["demo"]))

    async def test_a_retried_post_stores_one_message(self) -> None:
        body = {"text": "Hi", "client_id": "same"}
        first = await self.http.post(f"/api/chat/chats/{self.ids['demo']}/messages", json=body)
        second = await self.http.post(f"/api/chat/chats/{self.ids['demo']}/messages", json=body)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE client_id = 'same'"), [(1,)])

    async def test_the_router_off_refuses_new_timeline_messages(self) -> None:
        self.assertEqual((await self.http.put("/api/chat/router", json={"mode": "off"})).status_code, 200)
        refused = await self.http.post(f"/api/chat/chats/{self.ids['demo']}/messages",
                                       json={"text": "x", "client_id": "c"})
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "router_off"))


class RecycleTests(FlowTestCase):
    async def test_an_old_coordinator_is_replaced_and_its_successor_starts_from_the_handoff(self) -> None:
        from services.cowork_agent.storm.chats import memory, recycle
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("post_message", {"text": "First."}), *done("pc"),
                                    next_input(), claude.mcp_call("post_message", {"text": "Second."}),
                                    *done("pc", 2)]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Remember: I like terse answers.")
        await self.settle(lambda: ("agent", "First.") in self.texts(ids["demo"]))
        memory.write(ids["demo"], "MEMORY.md", "- [Style](style.md) — terse answers", if_version="new")
        old = self.rows("SELECT id FROM sessions WHERE role = 'project-coordinator'")[0][0]
        # Recycled only once its input ended (not merely idle): wait for that.
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                            "session_id = ?", (old,))[0][0] == 1)
        self.kit.clock.advance(recycle.AGE_MAX_S + 1)
        said = await self.post(ids["demo"], "And now?")
        new, incarnation, predecessor = self.rows("SELECT id, incarnation, predecessor FROM sessions WHERE "
                                                  "role = 'project-coordinator' AND retired_at IS NULL")[0]
        self.assertEqual((incarnation, predecessor), (2, old))
        self.assertIsNotNone(self.rows("SELECT retired_at FROM sessions WHERE id = ?", (old,))[0][0])
        order = [r[0] for r in self.rows("SELECT id FROM wakes WHERE session_id = ? ORDER BY rowid", (new,))]
        self.assertEqual(order[:2], [f"handoff:{old}", f"msg:{said['id']}"])
        handoff = self.rows("SELECT envelope FROM wakes WHERE id = ?", (f"handoff:{old}",))[0][0]
        self.assertIn("terse answers", handoff)
        self.assertIn("Remember: I like terse answers.", handoff)
