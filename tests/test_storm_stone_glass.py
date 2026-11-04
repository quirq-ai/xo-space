"""What xo-coworker's stone-and-glass UI needs from storm: shared files with
plain-word titles, tool words that match what the UI shows, the person's
shape kept across devices, and ``busy`` whenever an agent is running."""

from __future__ import annotations

import asyncio
import json
from unittest import mock

from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine import engine as engine_module
from services.cowork_agent.storm.presets import prompts as _prompts  # noqa: F401 - the package must exist
from tests.storm_comms_kit import CHOICE, CommsTestCase
from pathlib import Path

PROMPTS = Path(tools.__file__).resolve().parents[1] / "presets" / "prompts"


class WordsTests(CommsTestCase):
    def test_the_tools_say_what_the_ui_shows(self) -> None:
        share = tools.TOOLS["share_files"]
        self.assertIn("as pictures, several as an album", share[1])
        self.assertIn("caption", share[1])
        self.assertEqual(share[2]["properties"]["titles"]["items"]["maxLength"], tools.SHARE_TITLE_MAX)
        title = tools.TOOLS["ask_owner"][2]["properties"]["title"]
        self.assertIn("detail.command", title["description"])

    def test_the_prompts_ask_for_screenshots_and_asks_in_words(self) -> None:
        thread = (PROMPTS / "thread.md").read_text()
        self.assertIn("share a screenshot of it", thread)
        for name in ("thread.md", "project_coordinator.md", "workspace_coordinator.md"):
            self.assertIn("Write asks in words", (PROMPTS / name).read_text(), name)


class SharedTitleTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        from services.cowork_agent.storm.made import Made
        self.made = Made()
        self.made.start(self.kit.engine)
        self.addAsyncCleanup(self.made.stop)
        self.chat = await self.project()
        pc = await self.coordinator(self.chat)
        root = await self.owner_message(self.chat, "Design the picker")
        started = await self.call(await self.token(pc), "start_thread_session", message_id=root["id"],
                                  title="Picker", instructions="x", **CHOICE)
        self.token_ = await self.token(started["session_id"])
        self.folder = self.kit.projects / "demo"

    async def test_each_file_takes_a_plain_words_title(self) -> None:
        for name in ("r3-dark-320.png", "notes.md"):
            (self.folder / name).write_bytes(b"x")
        await self.call(self.token_, "share_files", paths=["r3-dark-320.png", "notes.md"],
                        titles=["Right panel,  dark, narrow"])
        (data,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'share'")]
        self.assertEqual([f.get("title") for f in data["files"]], ["Right panel, dark, narrow", None])
        await self.kit.until(lambda: not self.made._tasks)
        items = {i["name"]: i for i in await self.made.listing(self.chat)}
        self.assertEqual((items["r3-dark-320.png"]["title"], items["notes.md"]["title"]),
                         ("Right panel, dark, narrow", None))

    async def test_titles_are_checked(self) -> None:
        (self.folder / "a.md").write_text("a")
        self.assertIn("one per path", await self.refused(self.token_, "share_files", paths=["a.md"],
                                                          titles=["A", "B"]))
        self.assertIn("at most", await self.refused(self.token_, "share_files", paths=["a.md"],
                                                     titles=["x" * (tools.SHARE_TITLE_MAX + 1)]))


class ShapeTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()

    async def test_the_persons_shape_follows_them_and_two_devices_never_overwrite_silently(self) -> None:
        empty = (await self.http.get("/api/chat/shape")).json()
        self.assertEqual((empty["shape"], empty["version"]), (None, "new"))
        doc = {"shape": 1, "preset": "focused", "surfaces": {"xo:home/handled": {"on": False}}}
        saved = (await self.http.put("/api/chat/shape", json={"shape": doc, "if_version": "new"})).json()
        self.assertEqual(saved["shape"], doc)
        self.assertNotEqual(saved["version"], "new")
        self.assertEqual((await self.http.get("/api/chat/shape")).json(), saved)
        stale = await self.http.put("/api/chat/shape", json={"shape": {"shape": 1}, "if_version": "new"})
        self.assertEqual((stale.status_code, stale.json()["detail"]["code"]), (409, "shape_changed"))
        self.assertEqual(stale.json()["detail"]["current"]["shape"], doc)
        (event,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'settings'")]
        self.assertEqual((event["key"], event["value"]["doc"]), ("ui.shape", doc))
        cleared = (await self.http.put("/api/chat/shape", json={"shape": None})).json()
        self.assertEqual((cleared["shape"], cleared["version"]), (None, "new"))

    async def test_what_isnt_a_layer_is_refused(self) -> None:
        big = await self.http.put("/api/chat/shape", json={"shape": {"x": "y" * (70 * 1024)}})
        self.assertEqual((big.status_code, big.json()["detail"]["code"]), (413, "shape_too_large"))
        deep: dict = {}
        for _ in range(20):
            deep = {"a": deep}
        nested = await self.http.put("/api/chat/shape", json={"shape": deep})
        self.assertEqual((nested.status_code, nested.json()["detail"]["code"]), (400, "bad_shape"))
        self.assertEqual((await self.http.put("/api/chat/shape", json={"shape": [1]})).status_code, 422)


class BusyTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        pc = await self.coordinator(self.chat)
        root = await self.owner_message(self.chat, "Work")
        started = await self.call(await self.token(pc), "start_thread_session", message_id=root["id"],
                                  title="Work", instructions="x", **CHOICE)
        self.ts, self.sid = root["id"], started["session_id"]

    async def thread(self) -> dict:
        return (await self.http.get(f"/api/chat/chats/{self.chat['id']}/threads/{self.ts}")).json()

    def status(self) -> str:
        return self.rows("SELECT status FROM sessions WHERE id = ?", (self.sid,))[0][0]

    async def test_busy_while_its_agent_runs_or_a_sub_agent_works_on(self) -> None:
        # Its brief queued it, and the memory guard holds it there; once that settles, the brief is set aside
        # so nothing queues it again, and it is made warm (between turns).
        await self.kit.until(lambda: self.status() == "queued")
        await self.kit.engine.write(lambda w: w.execute(
            "UPDATE wakes SET state = 'dropped' WHERE session_id = ?", (self.sid,)))
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET status = 'warm' WHERE id = ?",
                                                        (self.sid,)))
        await asyncio.sleep(0.05)
        self.assertEqual(self.status(), "warm")
        thread = await self.thread()
        self.assertEqual((thread["busy"], thread["session"]["busy"]), (False, False))
        for state in sorted(engine_module.BUSY_STATES):
            await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET status = ? WHERE id = ?",
                                                            (state, self.sid)))
            self.assertTrue((await self.thread())["busy"], state)
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET status = 'warm' WHERE id = ?",
                                                        (self.sid,)))
        live = mock.Mock(**{"background_runs.return_value": 1, "facts.runs": {"r1"}, "facts.held": {},
                            "facts.turn": None, "held_items.return_value": []})
        with mock.patch.dict(self.kit.engine._live, {self.sid: live}):
            self.assertTrue((await self.thread())["busy"])
            (session,) = [s for s in (await self.http.get("/api/chat/sessions", params={"thread": self.ts})).json()[
                "sessions"] if s["id"] == self.sid]
            self.assertTrue(session["busy"])
