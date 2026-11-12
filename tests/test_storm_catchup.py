"""What a person catching up reads (v2): a thread's events with their times,
the chat's seen mark, and a thread's summary and git state on its view."""

from __future__ import annotations

import json

from tests.storm_comms_kit import CHOICE, CommsTestCase


class CatchUpTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        root = await self.owner_message(self.chat, "Write docs")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Docs",
                                  instructions="x", **CHOICE)
        self.ts = root["id"]
        self.thread_token = await self.token(started["session_id"])
        self.url = f"/api/chat/chats/{self.chat['id']}"

    async def test_a_threads_state_changes_come_with_their_times_and_chatter_is_left_out(self) -> None:
        (self.kit.projects / "demo" / "notes.md").write_text("# Notes")
        await self.call(self.thread_token, "share_files", paths=["notes.md"])
        await self.call(self.thread_token, "reply", text="Done.")
        resolved = await self.http.patch(f"{self.url}/threads/{self.ts}", json={"resolved": True})
        self.assertEqual(resolved.status_code, 200, resolved.text)
        page = (await self.http.get(f"{self.url}/threads/{self.ts}/events")).json()
        types = [e["type"] for e in page["events"]]
        self.assertIn("share", types)
        self.assertNotIn("message", types)
        states = [e["state"] for e in page["events"] if "state" in e]
        self.assertEqual(states[0], "stalled")  # Created before its first wake is queued.
        self.assertEqual(states[-1], "resolved")
        last = page["events"][-1]
        self.assertEqual((last["type"], last["thread_ts"], last["data"]["state"]), ("thread", self.ts, "resolved"))
        self.assertRegex(last["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([e["seq"] for e in page["events"]], sorted(e["seq"] for e in page["events"]))
        self.assertIsNone(page["next_after_seq"])

        # Paged by seq, read from a time on, asked for by type; chat-wide too.
        first = (await self.http.get(f"{self.url}/threads/{self.ts}/events", params={"limit": 1})).json()
        self.assertEqual(first["next_after_seq"], page["events"][0]["seq"])
        rest = (await self.http.get(f"{self.url}/threads/{self.ts}/events",
                                    params={"after_seq": first["next_after_seq"]})).json()["events"]
        self.assertEqual(rest, page["events"][1:])
        only = (await self.http.get(f"{self.url}/threads/{self.ts}/events", params={"types": "message"})).json()
        self.assertTrue(only["events"])
        self.assertEqual({e["type"] for e in only["events"]}, {"message"})
        later = (await self.http.get(f"{self.url}/thread-events", params={"since": "2999-01-01T00:00:00+02:00"})).json()
        self.assertEqual(later["events"], [])
        chat_wide = (await self.http.get(f"{self.url}/thread-events")).json()["events"]
        self.assertEqual(chat_wide, page["events"])

    async def test_a_new_kind_of_thread_event_shows_with_no_change_here(self) -> None:
        """A steer, interrupt or stop a later build records on the thread."""
        await self.kit.engine.write(lambda w: w.append("thread.steered", {"by": "owner"}, chat_id=self.chat["id"],
                                                       thread_ts=self.ts))
        events = (await self.http.get(f"{self.url}/threads/{self.ts}/events")).json()["events"]
        self.assertEqual((events[-1]["type"], events[-1]["data"]), ("thread.steered", {"by": "owner"}))

    async def test_bad_requests(self) -> None:
        missing = await self.http.get(f"{self.url}/threads/nope/events")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "thread_not_found"))
        bad = await self.http.get(f"{self.url}/thread-events", params={"since": "yesterday"})
        self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "bad_since"))
        none = await self.http.put("/api/chat/chats/chat_nope/seen")
        self.assertEqual((none.status_code, none.json()["detail"]["code"]), (404, "chat_not_found"))

    async def test_the_seen_mark_is_the_same_everywhere_and_never_moves_back(self) -> None:
        self.assertIsNone((await self.http.get(self.url)).json()["seen"])
        head = self.rows("SELECT max(seq) FROM events")[0][0]
        marked = (await self.http.put(f"{self.url}/seen")).json()["seen"]
        self.assertEqual(marked["seen_seq"], head)
        self.assertRegex(marked["seen_at"], r"Z$")
        self.assertEqual((await self.http.get(self.url)).json()["seen"], marked)
        listed = {c["id"]: c for c in (await self.http.get("/api/chat/chats")).json()["chats"]}
        self.assertEqual(listed[self.chat["id"]]["seen"], marked)
        older = (await self.http.put(f"{self.url}/seen", json={"seen_seq": 1})).json()["seen"]
        self.assertEqual(older, marked)                     # a slower device's older mark keeps the newer
        (event,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'chat.seen'")]
        self.assertEqual(event, {"id": self.chat["id"], **marked})
        await self.call(self.thread_token, "reply", text="More.")
        moved = (await self.http.put(f"{self.url}/seen", json={"seen_seq": 10**9})).json()["seen"]
        self.assertEqual(moved["seen_seq"], self.rows("SELECT max(seq) FROM events WHERE type <> 'chat.seen'")[0][0])

    async def test_a_thread_shows_its_summary_and_git_state(self) -> None:
        thread = (await self.http.get(f"{self.url}/threads/{self.ts}")).json()
        self.assertEqual((thread["summary"], thread["summary_at"], thread["git"]), (None, None, None))
        state = {"branch": "xo/thread-abc123", "merged": True}
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO thread_git (chat_id, thread_ts, state, checked_at) VALUES (?, ?, ?, ?)",
            (self.chat["id"], self.ts, json.dumps(state), w.now)))
        listed = (await self.http.get(f"{self.url}/threads")).json()["threads"]
        self.assertEqual([t["git"] for t in listed], [state])
