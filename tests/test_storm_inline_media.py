"""Inline media (xo-coworker's chat proposal §1, §13): an image's size and
tint for the person's uploads and for what agents share, shared files placed
for display, and an agent's message carrying what its turn shared."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image

from services import media_info
from services.cowork_agent.storm import made
from services.cowork_agent.storm.chats import attachments, shared
from services.cowork_agent.storm.engine import Writes
from tests.storm_comms_kit import CHOICE, CommsTestCase


def png(w: int, h: int, colour=(255, 0, 0)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(out, "PNG")
    return out.getvalue()


class ImageInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, data: bytes) -> Path:
        (self.dir / name).write_bytes(data)
        return self.dir / name

    def test_size_and_average_colour(self) -> None:
        self.assertEqual(media_info.image_info(self.write("a.png", png(30, 20, (16, 32, 48)))),
                         {"w": 30, "h": 20, "tint": "#102030"})

    def test_a_photo_turned_a_quarter_is_shown_the_other_way(self) -> None:
        out = io.BytesIO()
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (40, 10), (0, 0, 255)).save(out, "JPEG", exif=exif)
        got = media_info.image_info(self.write("p.jpg", out.getvalue()))
        self.assertEqual((got["w"], got["h"]), (10, 40))

    def test_what_isnt_a_readable_image_has_none(self) -> None:
        none = {"w": None, "h": None, "tint": None}
        self.assertEqual(media_info.image_info(self.write("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")), none)
        self.assertEqual(media_info.image_info(self.write("bad.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 20)), none)
        self.assertEqual(media_info.image_info(self.write("a.png", png(2, 2)), "text/plain"), none)

    def test_a_huge_image_gets_its_size_and_no_tint(self) -> None:
        with unittest.mock.patch.object(media_info, "TINT_MAX_PIXELS", 100):
            self.assertEqual(media_info.image_info(self.write("big.png", png(20, 20))),
                             {"w": 20, "h": 20, "tint": None})


class InlineMediaTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        root = await self.owner_message(self.chat, "Draw a chart")
        started = await self.call(await self.token(self.pc), "start_thread_session", message_id=root["id"],
                                  title="Chart", instructions="x", **CHOICE)
        self.ts, self.sid = root["id"], started["session_id"]
        self.token_ = await self.token(self.sid)
        self.folder = self.kit.projects / "demo"
        self.late = shared.LateShares()
        self.late.start(self.kit.engine)
        self.addAsyncCleanup(self.late.stop)

    async def start_turn(self, turn_id: str = "turn_1") -> str:
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES (?, ?, 0, 'person', "
            "'running', ?)", (turn_id, self.sid, w.now)))
        return turn_id

    async def end_turn(self, turn_id: str) -> None:
        def write(w: Writes) -> None:
            w.execute("UPDATE turns SET status = 'finished', ended_at = ? WHERE id = ?", (w.now, turn_id))
            w.append("turn.finished", {}, chat_id=self.chat["id"], session_id=self.sid, thread_ts=self.ts,
                     turn_id=turn_id)
        await self.kit.engine.write(write)
        await self.kit.until(lambda: not self.late._tasks)

    async def reply(self, text: str) -> dict:
        """The thread's agent replies, through its comms tool."""
        return {"id": (await self.call(self.token_, "reply", text=text))["message_id"]}

    def extra(self, message_id: str) -> dict:
        return json.loads(self.rows("SELECT extra FROM messages WHERE id = ?", (message_id,))[0][0])

    async def test_an_uploaded_image_carries_its_size_and_tint(self) -> None:
        att = (await self.http.post(f"/api/chat/chats/{self.chat['id']}/attachments", params={"name": "s.png"},
                                    content=png(64, 48, (0, 128, 0)))).json()
        self.assertEqual((att["w"], att["h"], att["tint"]), (64, 48, "#008000"))
        self.assertEqual(attachments.public({**att, "type": "image/png"})["w"], 64)
        text = (await self.http.post(f"/api/chat/chats/{self.chat['id']}/attachments", params={"name": "n.md"},
                                     content=b"# hi")).json()
        self.assertNotIn("w", text)

    async def test_a_shared_image_is_placed_for_display(self) -> None:
        (self.folder / "out").mkdir()
        (self.folder / "out" / "chart.png").write_bytes(png(300, 200))
        (self.folder / "notes.md").write_text("# n")
        result = await self.call(self.token_, "share_files", paths=["out/chart.png", "notes.md"])
        self.assertEqual(set(result["shared"][0]), {"path", "name", "type", "bytes"})    # the agent's view is as before
        (data,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'share'")]
        chart, notes = data["files"]
        self.assertEqual({k: chart[k] for k in ("path", "project_id", "relative_path", "w", "h")},
                         {"path": "out/chart.png", "project_id": "demo", "relative_path": "out/chart.png",
                          "w": 300, "h": 200})
        self.assertEqual(chart["made_id"], made.file_id(self.chat["id"], self.folder / "out" / "chart.png"))
        self.assertRegex(chart["mtime"], r"Z$")
        self.assertRegex(chart["tint"], r"^#[0-9a-f]{6}$")
        self.assertNotIn("w", notes)
        outputs = json.loads(self.rows("SELECT outputs FROM threads WHERE ts = ?", (self.ts,))[0][0])
        self.assertEqual(outputs, data["files"])

    async def test_a_reply_carries_what_its_turn_shared_since_its_previous_message(self) -> None:
        (self.folder / "a.png").write_bytes(png(10, 10))
        (self.folder / "b.png").write_bytes(png(20, 10))
        (self.folder / "c.png").write_bytes(png(30, 10))
        turn = await self.start_turn()
        await self.call(self.token_, "share_files", paths=["a.png"])
        first = await self.reply("Here is a.")
        await self.call(self.token_, "share_files", paths=["b.png"])
        second = await self.reply("And b.")
        await self.call(self.token_, "share_files", paths=["c.png"])        # after the last reply
        self.assertEqual([f["name"] for f in self.extra(first["id"])["attachments"]], ["a.png"])
        self.assertEqual([(f["name"], f["w"]) for f in self.extra(second["id"])["attachments"]], [("b.png", 20)])
        await self.end_turn(turn)
        self.assertEqual([f["name"] for f in self.extra(second["id"])["attachments"]], ["b.png", "c.png"])
        (updated,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'message.updated'")]
        self.assertEqual((updated["id"], updated["thread_ts"]), (second["id"], self.ts))
        self.assertEqual([f["name"] for f in updated["extra"]["attachments"]], ["b.png", "c.png"])
        messages = (await self.http.get(f"/api/chat/chats/{self.chat['id']}/messages",
                                        params={"thread": self.ts})).json()["messages"]
        self.assertEqual([f["name"] for f in messages[-1]["extra"]["attachments"]], ["b.png", "c.png"])

    async def test_a_turn_with_no_message_and_one_with_no_share_change_nothing(self) -> None:
        (self.folder / "a.png").write_bytes(png(10, 10))
        turn = await self.start_turn()
        await self.call(self.token_, "share_files", paths=["a.png"])
        await self.end_turn(turn)
        turn = await self.start_turn("turn_2")
        quiet = await self.reply("Nothing to show.")
        await self.end_turn(turn)
        self.assertEqual(self.extra(quiet["id"]), {})
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'message.updated'"), [(0,)])


if __name__ == "__main__":
    unittest.main()
