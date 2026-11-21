"""Files an agent shared (``share_files``), cited by a coordinator: every
path that renders or carries a cited message's files takes them as well as
an owner's uploads (``attachments.is_upload`` / ``location``). Before, a
shared file has no ``id`` and each of these paths failed with a bare
``server_error`` (2026-11-21, xo-coworker's three asks at 20:29Z).

Also: an unexpected failure names its exception's class to the agent, never
its message."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.chats import attachments, rows, shared
from services.cowork_agent.storm.comms import calls, talk
from tests.storm_comms_kit import CHOICE, CommsTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PLAN = b"# Plan\n\nStep one.\n"


class _Shared(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.folder = attachments.chat_folder(self.chat)
        self.coord = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.coord)

    def made(self, path: str, data: bytes, kind: str) -> dict:
        """A file the project's agent shared: on disk in the project, and
        described as ``share_files`` describes it."""
        where = self.folder / path
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_bytes(data)
        return shared.describe(self.chat["id"], str(self.folder),
                               {"path": path, "name": Path(path).name, "type": kind, "bytes": len(data)})

    async def agent_said(self, text: str, files: list[dict], thread_ts: str | None = None) -> dict:
        return await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], thread_ts=thread_ts, author_kind="agent", author_id="thread", text=text,
            extra={"attachments": files}))

    async def thread(self) -> tuple[str, str]:
        """A thread of the project: its root's id and its session."""
        root = await self.owner_message(self.chat, "Rebuild the cards")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Cards",
                                  instructions="Rebuild them.", **CHOICE)
        return root["id"], started["session_id"]

    def last_wake(self, session_id: str) -> tuple[str, dict]:
        envelope, meta = self.rows("SELECT envelope, meta FROM wakes WHERE session_id = ? ORDER BY rowid",
                                   (session_id,))[-1]
        return envelope, json.loads(meta)


class LocationTests(_Shared):
    async def test_an_upload_and_a_shared_file_each_have_their_place(self) -> None:
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        self.assertFalse(attachments.is_upload(plan))
        self.assertEqual(attachments.location(self.folder, plan), (self.folder / "out/plan.md").resolve())
        upload = {"id": "att_" + "a" * 24, "name": "x.png", "type": "image/png", "bytes": 1}
        self.assertTrue(attachments.is_upload(upload))
        self.assertEqual(attachments.location(self.folder, upload), attachments.path_of(self.folder, upload))

    async def test_a_shared_file_never_resolves_outside_its_project(self) -> None:
        (self.kit.projects / "other").mkdir()
        (self.kit.projects / "other" / "secret.md").write_bytes(b"theirs")
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        for relative in ("../other/secret.md", "/etc/passwd", "", None):
            with self.subTest(relative=relative):
                self.assertIsNone(attachments.location(self.folder, {**plan, "relative_path": relative}))
        for project in (None, "", "..", "../other", "demo/../other"):
            with self.subTest(project=project):
                self.assertIsNone(attachments.location(self.folder, {**plan, "project_id": project}))
        (self.folder / "out" / "link.md").symlink_to(self.kit.projects / "other" / "secret.md")
        self.assertIsNone(attachments.location(self.folder, {**plan, "relative_path": "out/link.md"}))

    async def test_the_tag_says_which_kind_it_is(self) -> None:
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        upload = {"id": "att_" + "b" * 24, "name": "bug.png", "type": "image/png", "bytes": 1}
        from services.cowork_agent.storm.chats import envelope
        tags = envelope.attachment_tags({"extra": {"attachments": [upload, plan]}}, self.folder)
        self.assertIn(f'id="{upload["id"]}"', tags)
        self.assertIn('source="upload"', tags)
        self.assertIn(f'name="plan.md" type="text/markdown" bytes="{len(PLAN)}" '
                      f'path="{(self.folder / "out/plan.md").resolve()}" source="shared" trust="data"', tags)
        gone = {**plan, "relative_path": "../elsewhere.md"}
        self.assertIn('name="plan.md" type="text/markdown" bytes', envelope.attachment_tags(
            {"extra": {"attachments": [gone]}}, self.folder))
        self.assertNotIn("elsewhere", envelope.attachment_tags({"extra": {"attachments": [gone]}}, self.folder))


class CitedSharedFileTests(_Shared):
    async def test_an_ask_citing_the_owners_reply_to_a_shared_file_reaches_the_workspace(self) -> None:
        """The failing case: the owner's message in a thread brings the
        agent's message it answered, which carries a shared file."""
        ts, _session = await self.thread()
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        proposal = await self.agent_said("Here's the plan.", [plan], thread_ts=ts)
        yes = await self.owner_message(self.chat, "now correctly build them", thread_ts=ts)
        asked = await self.call(self.pc_token, "ask", to="workspace", question="Can xo-space take this?",
                                on_behalf_of=ts, context_message_ids=[yes["id"]])
        workspace = await self.coordinator(await self.chats.workspace())
        envelope, _meta = self.last_wake(workspace)
        self.assertIn(f'hop="{asked["hop_id"]}"', envelope)
        self.assertIn(proposal["id"], envelope)
        self.assertIn(f'path="{(self.folder / "out/plan.md").resolve()}" source="shared"', envelope)

    async def test_message_thread_hands_the_thread_a_shared_image(self) -> None:
        ts, session = await self.thread()
        shot = self.made("out/shot.png", PNG, "image/png")
        said = await self.agent_said("The screenshot.", [shot])
        await self.call(self.pc_token, "message_thread", thread_id=ts, note="See this.",
                        context_message_ids=[said["id"]])
        envelope, meta = self.last_wake(session)
        where = str((self.folder / "out/shot.png").resolve())
        self.assertIn(f'path="{where}" source="shared"', envelope)
        self.assertEqual(meta["images"], [where])

    async def test_create_task_cites_a_shared_file(self) -> None:
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        said = await self.agent_said("The plan.", [plan])
        root = await self.owner_message(self.chat, "build it")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Build",
                                  instructions="Build the plan.", context_message_ids=[said["id"]], **CHOICE)
        envelope, _meta = self.last_wake(started["session_id"])
        self.assertIn(f'path="{(self.folder / "out/plan.md").resolve()}" source="shared"', envelope)

    async def test_a_forward_to_another_project_copies_a_shared_file_in_as_an_upload(self) -> None:
        other = await self.project("other")
        shot = self.made("out/shot.png", PNG, "image/png")
        await self.agent_said("Our screenshot.", [shot])
        ok = await self.owner_message(self.chat, "ask other to match this")
        asked = await self.call(self.pc_token, "ask", to="workspace", question="For other.",
                                context_message_ids=[ok["id"]])
        ws_token = await self.token(await self.coordinator(await self.chats.workspace()))
        (carried_ids,) = self.rows("SELECT message_ids FROM hops WHERE id = ?", (asked["hop_id"],))[0]
        forwarded = await self.call(ws_token, "forward_to_project", project="other", ask_id=asked["hop_id"],
                                    context_message_ids=json.loads(carried_ids))
        copies = {m: json.loads(self.rows("SELECT extra FROM messages WHERE id = ?", (m,))[0][0])
                  for m in forwarded["forwarded_message_ids"]}
        [(copied,)] = [e["attachments"] for e in copies.values() if e.get("attachments")]
        self.assertTrue(attachments.is_upload(copied))
        self.assertEqual({k: copied[k] for k in ("name", "type", "bytes")},
                         {"name": "shot.png", "type": "image/png", "bytes": len(PNG)})
        other_folder = attachments.chat_folder(other)
        self.assertEqual(attachments.path_of(other_folder, copied).read_bytes(), PNG)

    async def test_a_shared_file_that_is_gone_is_left_out_and_the_forward_still_goes(self) -> None:
        await self.project("other")
        plan = self.made("out/plan.md", PLAN, "text/markdown")
        await self.agent_said("The plan.", [plan])
        (self.folder / "out/plan.md").unlink()
        ok = await self.owner_message(self.chat, "send it on")
        asked = await self.call(self.pc_token, "ask", to="workspace", question="For other.",
                                context_message_ids=[ok["id"]])
        ws_token = await self.token(await self.coordinator(await self.chats.workspace()))
        (carried_ids,) = self.rows("SELECT message_ids FROM hops WHERE id = ?", (asked["hop_id"],))[0]
        with self.assertLogs("services.cowork_agent.storm.chats.attachments", "WARNING") as logged:
            forwarded = await self.call(ws_token, "forward_to_project", project="other", ask_id=asked["hop_id"],
                                        context_message_ids=json.loads(carried_ids))
        self.assertEqual(len(forwarded["forwarded_message_ids"]), 2)
        self.assertIn("demo/out/plan.md", logged.output[0])


class ServerErrorTests(_Shared):
    async def test_an_unexpected_failure_names_its_class_never_its_message(self) -> None:
        with mock.patch.object(talk, "to_workspace", side_effect=KeyError("id")), \
                self.assertLogs("services.cowork_agent.storm.comms.calls", "ERROR"):
            said = await self.refused(self.pc_token, "ask", to="workspace", question="Anything?")
        self.assertIn(calls.UNEXPECTED.format(error="KeyError"), said)
        self.assertNotIn("'id'", said)
        (data,) = self.rows("SELECT data FROM events WHERE type = 'comms.call' ORDER BY seq DESC LIMIT 1")[0]
        self.assertEqual({k: json.loads(data)[k] for k in ("ok", "error", "exception")},
                         {"ok": False, "error": "server_error", "exception": "KeyError"})
