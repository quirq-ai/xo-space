"""Files the owner attaches to a message (hub #30): the upload and read
routes, what they refuse, the message that names them, what each agent is
handed, forwarding, the sweep, and the read policy. Every path is the
server's: the security cases (hostile names and ids, planted links, markup
served as text, cross-site writes, the quota) are here with the rest."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.chats import attachments, rows
from services.cowork_agent.storm.engine import engine as engine_module
from services.cowork_agent.storm.engine.model import IMAGE_INPUT_MAX_BYTES
from services.cowork_agent.storm.review.paths import Place, Places
from tests.storm_comms_kit import CHOICE, CommsTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64


class _Attachments(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.folder = attachments.chat_folder(self.chat)

    async def upload(self, data: bytes, name: str | None = "file.txt", chat: dict | None = None):
        params = {"name": name} if name is not None else {}
        return await self.http.post(f"/api/chat/chats/{(chat or self.chat)['id']}/attachments", params=params,
                                    content=data)

    async def uploaded(self, data: bytes, name: str = "file.txt", chat: dict | None = None) -> dict:
        response = await self.upload(data, name, chat)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def read(self, att_id: str, chat: dict | None = None):
        return self.http.get(f"/api/chat/chats/{(chat or self.chat)['id']}/attachments/{att_id}")

    def root(self, chat: dict | None = None) -> Path:
        return attachments.chat_folder(chat or self.chat) / ".xo" / "attachments"


class UploadTests(_Attachments):
    async def test_an_image_is_kept_under_a_name_the_server_picks_and_read_back(self) -> None:
        att = await self.uploaded(PNG, "screen shot.png")
        self.assertEqual({k: att[k] for k in ("name", "type", "bytes")},
                         {"name": "screen shot.png", "type": "image/png", "bytes": len(PNG)})
        self.assertRegex(att["id"], r"^att_[0-9a-f]{24}$")
        self.assertEqual(sorted(p.name for p in (self.root() / att["id"]).iterdir()), ["file.png", "meta.json"])
        self.assertEqual((self.root() / ".gitignore").read_text(), "*\n")       # git never sees a file
        self.assertNotIn("path", json.dumps(att))
        got = await self.read(att["id"])
        self.assertEqual((got.status_code, got.content), (200, PNG))
        self.assertEqual(got.headers["content-type"], "image/png")
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        self.assertTrue(got.headers["content-security-policy"].startswith("sandbox;"))
        self.assertEqual(got.headers["cross-origin-resource-policy"], "same-site")
        self.assertTrue(got.headers["content-disposition"].startswith("inline;"))

    async def test_the_type_comes_from_the_bytes_and_markup_is_served_as_text(self) -> None:
        svg = await self.uploaded(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "x.svg")
        self.assertEqual(svg["type"], "text/plain")
        got = await self.read(svg["id"])
        self.assertEqual(got.headers["content-type"], "text/plain; charset=utf-8")
        self.assertTrue(got.headers["content-disposition"].startswith("attachment;"))
        self.assertIn("sandbox", got.headers["content-security-policy"])
        self.assertEqual((await self.uploaded(PNG, "looks.txt"))["type"], "image/png")
        self.assertEqual((await self.uploaded(b"# Notes\n", "notes.md"))["type"], "text/markdown")
        pdf = await self.uploaded(PDF, "spec.pdf")
        got = await self.read(pdf["id"])
        self.assertEqual((pdf["type"], got.headers["content-type"]), ("application/pdf", "application/pdf"))
        self.assertTrue(got.headers["content-disposition"].startswith("inline;"))
        self.assertIn("default-src 'none'", got.headers["content-security-policy"])

    async def test_what_is_refused(self) -> None:
        for data, status, code in ((b"PK\x03\x04" + b"\x00" * 40, 415, "unsupported_type"),
                                   (b"\xff\xfe\xfa\x00 not utf-8", 415, "unsupported_type"),
                                   (b"text with a \x00 byte", 415, "unsupported_type"),
                                   (b"", 400, "empty_file")):
            with self.subTest(code=code, data=data[:8]):
                response = await self.upload(data)
                self.assertEqual((response.status_code, response.json()["detail"]["code"]), (status, code))
        with mock.patch.object(attachments, "MAX_BYTES", 10):
            response = await self.upload(b"x" * 11)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (413, "too_large"))
        # nothing half-written is left behind
        self.assertEqual([p.name for p in self.root().iterdir()], [".gitignore"])
        self.assertEqual((await self.http.post("/api/chat/chats/chat_nope/attachments", content=b"x")).status_code,
                         404)

    async def test_a_declared_length_over_the_cap_is_refused_before_reading(self) -> None:
        with mock.patch.object(attachments, "MAX_BYTES", 10), \
                mock.patch.object(attachments.Upload, "open", side_effect=AssertionError("read")):
            response = await self.upload(b"x" * 11)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (413, "too_large"))

    async def test_a_hostile_name_is_a_display_name_only(self) -> None:
        for raw, kept in (("../../.ssh/authorized_keys", "authorized_keys"), ("/etc/passwd", "passwd"),
                          ("..\\..\\boot.ini", "boot.ini"), ("a\r\nX-Evil: 1.txt", "aX-Evil: 1.txt"),
                          ("..", "file"), ("é" * 300, "é" * 127)):
            with self.subTest(raw=raw[:20]):
                att = await self.uploaded(b"hello", raw)
                self.assertEqual(att["name"], kept)
                self.assertEqual([p.name for p in (self.root() / att["id"]).iterdir() if p.name != "meta.json"],
                                 ["file.txt"])
                got = await self.read(att["id"])
                self.assertNotIn("\n", got.headers["content-disposition"])
        self.assertFalse((self.folder / ".ssh").exists())

    async def test_an_id_reaches_only_its_own_chats_file(self) -> None:
        att = await self.uploaded(b"mine")
        other = await self.project("other")
        for bad in ("..%2F..%2Fmeta", "att_" + "g" * 24, "att_" + "0" * 24, "ATT_" + "0" * 24):
            with self.subTest(bad=bad):
                self.assertEqual((await self.read(bad)).status_code, 404)
        self.assertEqual((await self.read(att["id"], other)).status_code, 404)

    async def test_a_planted_link_is_never_followed(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "secret").write_text("key")
        xo = self.folder / ".xo"
        xo.mkdir(exist_ok=True)
        (xo / "attachments").symlink_to(outside)                     # a cloned repo committed this
        response = await self.upload(b"x")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]),
                         (409, "unsafe_attachments_folder"))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["secret"])
        (xo / "attachments").unlink()
        att = await self.uploaded(b"real")
        # a link in place of the file, and an id folder that is a link
        target = self.root() / att["id"] / "file.txt"
        target.unlink()
        target.symlink_to(outside / "secret")
        self.assertEqual((await self.read(att["id"])).status_code, 404)
        fake = "att_" + "1" * 24
        (self.root() / fake).symlink_to(outside)
        self.assertEqual((await self.read(fake)).status_code, 404)

    async def test_a_cross_site_upload_is_refused(self) -> None:
        response = await self.http.post(f"/api/chat/chats/{self.chat['id']}/attachments", content=b"x",
                                        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.root().exists())

    async def test_a_chat_holds_only_so_many_unsent_files(self) -> None:
        with mock.patch.object(attachments, "UNSENT_MAX_FILES", 2):
            first = await self.uploaded(b"1")
            await self.uploaded(b"2")
            refused = await self.upload(b"3")
            self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (429, "too_many_unsent"))
            sent = await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages",
                                        json={"text": "see file", "client_id": "c1", "attachments": [first["id"]]})
            self.assertEqual(sent.status_code, 200)
            await self.uploaded(b"3")                                     # one was sent: room again


class MessageTests(_Attachments):
    async def test_a_message_names_its_files_and_the_coordinator_gets_their_paths(self) -> None:
        att = await self.uploaded(PNG, '<i>"x" & y.png')
        sent = await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages",
                                    json={"text": "", "client_id": "c1", "attachments": [att["id"]]})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["extra"]["attachments"], [att])
        listed = (await self.http.get(f"/api/chat/chats/{self.chat['id']}/messages")).json()["messages"]
        self.assertEqual(listed[-1]["extra"]["attachments"], [att])
        coordinator = await self.coordinator(self.chat)
        envelope_text, meta = self.rows("SELECT envelope, meta FROM wakes WHERE session_id = ?", (coordinator,))[-1]
        self.assertIn(f'path="{attachments.path_of(self.folder, att)}"', envelope_text)
        self.assertIn('name="&lt;i&gt;&quot;x&quot; &amp; y.png"', envelope_text)
        self.assertIn('trust="data"', envelope_text)
        self.assertNotIn("images", json.loads(meta))                    # a coordinator gets the path only
        retried = await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages",
                                       json={"text": "", "client_id": "c1", "attachments": [att["id"]]})
        self.assertEqual(retried.json()["id"], sent.json()["id"])

    async def test_what_a_message_may_not_name(self) -> None:
        other = await self.project("other")
        elsewhere = await self.uploaded(b"theirs", chat=other)
        for body, status in (({"text": "", "client_id": "a"}, 422),
                             ({"text": "x", "client_id": "b", "attachments": [elsewhere["id"]]}, 400),
                             ({"text": "x", "client_id": "c", "attachments": ["../../etc/passwd"]}, 400),
                             ({"text": "x", "client_id": "d", "attachments": ["att_" + "0" * 24] * 11}, 422)):
            with self.subTest(body=body):
                response = await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages", json=body)
                self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE chat_id = ?", (self.chat["id"],)), [(0,)])

    async def test_the_thread_doing_the_work_sees_the_images(self) -> None:
        image = await self.uploaded(PNG, "bug.png")
        notes = await self.uploaded(b"steps", "steps.txt")
        root = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="owner", author_id="owner", text="Fix this",
            extra={"attachments": [image, notes]}))
        token = await self.token(await self.coordinator(self.chat))
        started = await self.call(token, "start_thread_session", message_id=root["id"], title="Fix",
                                  instructions="Fix it.", **CHOICE)
        envelope_text, meta = self.rows("SELECT envelope, meta FROM wakes WHERE session_id = ?",
                                        (started["session_id"],))[-1]
        self.assertEqual(json.loads(meta)["images"], [str(attachments.path_of(self.folder, image))])
        self.assertIn(str(attachments.path_of(self.folder, notes)), envelope_text)
        # a follow-up posted in the thread itself
        later = await self.uploaded(PNG, "after.png")
        await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages",
                             json={"text": "and this", "thread_ts": root["id"], "client_id": "t1",
                                   "attachments": [later["id"]]})
        meta = self.rows("SELECT meta FROM wakes WHERE session_id = ? ORDER BY rowid", (started["session_id"],))[-1][0]
        self.assertEqual(json.loads(meta)["images"], [str(attachments.path_of(self.folder, later))])

    async def test_a_forward_takes_the_files_into_the_project(self) -> None:
        workspace = await self.chats.workspace()
        att = await self.uploaded(PNG, "mock.png", chat=workspace)
        said = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=workspace["id"], author_kind="owner", author_id="owner", text="Build this in demo",
            extra={"attachments": [att]}))
        ws_token = await self.token(await self.coordinator(workspace))
        forwarded = await self.call(ws_token, "forward_to_project", project="demo", context_message_ids=[said["id"]])
        [copy] = forwarded["forwarded_message_ids"]
        [carried] = json.loads(self.rows("SELECT extra FROM messages WHERE id = ?", (copy,))[0][0])["attachments"]
        self.assertNotEqual(carried["id"], att["id"])
        self.assertEqual({k: carried[k] for k in ("name", "type", "bytes")},
                         {"name": "mock.png", "type": "image/png", "bytes": len(PNG)})
        self.assertEqual((await self.read(carried["id"])).content, PNG)             # read in demo's chat
        self.assertEqual((await self.read(att["id"])).status_code, 404)              # not the workspace's id
        self.assertTrue(attachments.path_of(self.folder, carried).is_file())

    def test_a_decision_model_reads_a_count_and_types_never_names(self) -> None:
        message = {"text": "Look", "extra": {"attachments": [
            {"id": "att_1", "name": "payroll-2026.pdf", "type": "application/pdf", "bytes": 1},
            {"id": "att_2", "name": "secret.png", "type": "image/png", "bytes": 1}]}}
        said = attachments.routing_text(message)
        self.assertEqual(said, "Look\n[2 attachments: application/pdf, image/png]")
        self.assertNotIn("payroll", said)


class SweepTests(_Attachments):
    async def test_only_old_unsent_files_go(self) -> None:
        sent = await self.uploaded(b"sent")
        old = await self.uploaded(b"old")
        fresh = await self.uploaded(b"fresh")
        await self.http.post(f"/api/chat/chats/{self.chat['id']}/messages",
                             json={"text": "x", "client_id": "c1", "attachments": [sent["id"]]})
        outside = Path(tempfile.mkdtemp())
        (outside / "keep").write_text("k")
        planted = self.root() / ("att_" + "2" * 24)
        planted.symlink_to(outside)
        later = time.time() + attachments.UNSENT_KEEP_S + 1
        with mock.patch.object(attachments.time, "time", return_value=later):
            meta = attachments.lookup(self.folder, fresh["id"])
            (self.root() / fresh["id"] / "meta.json").unlink()
            (self.root() / fresh["id"] / "meta.json").write_text(json.dumps({**meta, "created_at": later}))
            removed = await attachments.sweep_all(self.chats)
        self.assertEqual(removed, 1)
        self.assertEqual(sorted(p.name for p in self.root().iterdir() if p.name.startswith("att_")),
                         sorted([sent["id"], fresh["id"], planted.name]))
        self.assertNotIn(old["id"], os.listdir(self.root()))
        self.assertEqual(os.listdir(outside), ["keep"])                              # a link is never followed


class ImageInputTests(unittest.TestCase):
    def test_only_the_runtimes_types_still_on_disk_and_small_enough(self) -> None:
        folder = Path(tempfile.mkdtemp())
        small, big, gif = folder / "a.png", folder / "b.png", folder / "c.gif"
        small.write_bytes(PNG)
        big.write_bytes(b"x" * (IMAGE_INPUT_MAX_BYTES + 1))
        gif.write_bytes(b"GIF89a")
        (folder / "link.png").symlink_to(small)
        meta = json.dumps({"origin": "person", "images": [str(small), str(big), str(gif), str(folder / "gone.png"),
                                                          str(folder / "link.png")]})
        wake = ("w1", "text", meta, "2026-10-22T00:00:00Z", "message")
        got = engine_module._input([wake], image_types=frozenset({"image/png"}))
        self.assertEqual(got.images, (str(small),))
        self.assertEqual(engine_module._input([wake]).images, ())                   # a runtime without images


class ReadPolicyTests(unittest.TestCase):
    def test_a_thread_reads_its_projects_attachments_quietly_and_never_writes_them(self) -> None:
        home = Path(tempfile.mkdtemp()).resolve()
        project = home / "xo-projects" / "app"
        worktree = project / ".xo" / "worktrees" / "thread-1"
        worktree.mkdir(parents=True)
        files = project / ".xo" / "attachments" / ("att_" + "3" * 24)
        files.mkdir(parents=True)
        (files / "file.png").write_bytes(PNG)
        (home / ".ssh").mkdir()
        (files / "file.txt").symlink_to(home / ".ssh")
        places = Places(session_folder=worktree, home=home, state_root=home / ".quirq", worktree=worktree,
                        project_folder=project, worktrees_root=project / ".xo" / "worktrees")
        self.assertEqual(places.place(str(files / "file.png"), worktree), Place.INSIDE)
        self.assertEqual(places.place(str(files / "file.txt"), worktree), Place.SECRET)   # judged where it leads
        self.assertIn("protected", places.protected(str(files / "file.png"), worktree) or "")
        other = home / "xo-projects" / "other" / ".xo" / "attachments" / "x.png"
        self.assertEqual(places.place(str(other), worktree), Place.OUTSIDE)              # another project's


if __name__ == "__main__":
    unittest.main()
