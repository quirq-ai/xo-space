"""Agents in runs (and the try preset): a thread works as an agent
and records it on its root message, continuation keeps it, agents propose
agents through a card the owner approves, and a try runs read-only.

The engine runs on the fake claude CLI with no process starting
(``CommsTestCase.boot``); what a call writes is read from the store.
"""

from __future__ import annotations

import json

from services.cowork_agent.storm.chats import Intelligence, rows
from services.cowork_agent.storm.engine.specs import spec_from_json
from services.cowork_agent.storm.review.decisions import Request
from services.storage.layout import agents_dir
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT

REVIEWER = """---
name: reviewer
description: Reviews a change and reports what is wrong.
tools: [read, search, list]
---
Review the diff. Never change files.
"""


def _request(decision: str, **fields) -> Request:
    return Request(decision=decision, idempotency_key=f"k-{decision}", **fields)


class PersonaRunsTestCase(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)

    def write_agent(self, text: str = REVIEWER, name: str = "reviewer") -> None:
        folder = self.kit.projects / "demo" / ".xo" / "agents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.md").write_text(text, encoding="utf-8")

    def spec_of(self, session_id: str):
        return spec_from_json(self.rows("SELECT spec FROM sessions WHERE id = ?", (session_id,))[0][0])

    def extra_of(self, message_id: str) -> dict:
        return json.loads(self.rows("SELECT extra FROM messages WHERE id = ?", (message_id,))[0][0])


class RunTests(PersonaRunsTestCase):
    async def test_a_task_with_an_agent_works_as_it_and_its_root_records_it(self) -> None:
        self.write_agent()
        root = await self.owner_message(self.chat, "Review the login change")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Review",
                                  instructions="Look at the last commit.", persona="reviewer", **CHOICE)
        persona = self.extra_of(root["id"])["persona"]
        self.assertEqual((persona["name"], persona["level"]), ("reviewer", "project"))
        self.assertTrue(persona["sha256"].startswith("sha256:"))
        spec = self.spec_of(started["session_id"])
        self.assertTrue(spec.system_prompt.text.endswith("# You work as the agent “reviewer”\n\n"
                                                         "Review the diff. Never change files."))
        self.assertEqual(spec.tools.deny, ("edit", "create", "shell", "web_fetch", "web_search"))

    async def test_a_task_without_an_agent_is_unchanged(self) -> None:
        root = await self.owner_message(self.chat, "Fix the login page")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Fix",
                                  instructions="x", **CHOICE)
        self.assertNotIn("persona", self.extra_of(root["id"]))
        self.assertEqual(self.spec_of(started["session_id"]).tools.deny, ())

    async def test_a_missing_or_broken_agent_starts_nothing(self) -> None:
        root = await self.owner_message(self.chat, "Review it")
        self.assertIn("There is no agent 'reviewer'", await self.refused(
            self.pc_token, "create_task", message_id=root["id"], title="Review", instructions="x",
            persona="reviewer", **CHOICE))
        self.write_agent(REVIEWER.replace("tools:", "permissionMode: auto\ntools:"))
        self.assertIn("has mistakes to fix first", await self.refused(
            self.pc_token, "create_task", message_id=root["id"], title="Review", instructions="x",
            persona="reviewer", **CHOICE))
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(0,)])

    async def test_an_agents_runtime_model_and_effort_are_defaults_the_caller_overrides(self) -> None:
        self.write_agent(REVIEWER.replace("tools:", f"runtime: {AGENT}\nmodel: haiku\neffort: low\ntools:"))
        persona = await self.chats.persona(self.chat, "reviewer")
        ts = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="system", author_id="xo", text="run")["id"])
        await self.chats.start_thread(self.chat, ts, "Review", Intelligence(), lambda w, s, n: s, persona=persona)
        self.assertEqual(self.rows("SELECT agent, model, effort FROM intelligence WHERE scope = 'thread'"),
                         [(AGENT, "haiku", "low")])

    async def test_a_try_runs_read_only_with_its_agent(self) -> None:
        self.write_agent()
        persona = await self.chats.persona(self.chat, "reviewer")
        ts = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="system", author_id="xo", text="Try", extra={"try": True})["id"])
        session = await self.chats.start_thread(self.chat, ts, "Try reviewer", Intelligence(**CHOICE),
                                                lambda w, s, n: s, preset="try", persona=persona)
        self.assertEqual(self.rows("SELECT preset, role FROM sessions WHERE id = ?", (session,)), [("try", "thread")])
        spec = self.spec_of(session)
        self.assertEqual((spec.tools.preset, spec.permissions.mode, spec.orchestration),
                         ("read_only", "deny_unasked", "coordinator"))
        self.assertEqual(self.extra_of(ts), {"try": True, "persona": self.extra_of(ts)["persona"]})


class ContinuationTests(PersonaRunsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.write_agent()
        self.root = await self.owner_message(self.chat, "Review the login change")
        await self.call(self.pc_token, "create_task", message_id=self.root["id"], title="Review",
                        instructions="x", persona="reviewer", **CHOICE)
        self.asked = await self.owner_message(self.chat, "Move the review elsewhere")

    async def move(self) -> dict:
        return await self.call(self.pc_token, "continue_thread", thread_id=self.root["id"], agent=AGENT,
                               reason="the owner asked", context_message_ids=[self.asked["id"]])

    async def test_continuation_keeps_the_agent(self) -> None:
        moved = await self.move()
        self.assertEqual(self.extra_of(moved["thread_id"])["persona"]["name"], "reviewer")
        self.assertIn("Review the diff.", self.spec_of(moved["session_id"]).system_prompt.text)

    async def test_continuation_without_the_agents_file_says_so(self) -> None:
        (self.kit.projects / "demo" / ".xo" / "agents" / "reviewer.md").unlink()
        moved = await self.move()
        self.assertNotIn("persona", self.extra_of(moved["thread_id"]))
        notes = [t for (t,) in self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                                         (moved["thread_id"],))]
        self.assertIn("The agent “reviewer” is gone, so this thread continues without it.", notes)


class ProposeTests(PersonaRunsTestCase):
    def saved(self) -> str | None:
        path = self.kit.projects / "demo" / ".xo" / "agents" / "reviewer.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    async def test_an_agent_proposes_an_agent_and_nothing_is_saved_until_approved(self) -> None:
        asked = await self.call(self.pc_token, "create_persona", text=REVIEWER, why="Reviews need a fixed checklist.")
        self.assertIsNone(self.saved())
        item = await self.queue.get_item(asked["review_id"])
        self.assertEqual((item["kind"], item["risk"], item["title"]),
                         ("approval", "notable", "Add the agent “reviewer” to project demo"))
        proposal = item["detail"]["create_persona"]
        self.assertEqual((proposal["file"], proposal["base_version"], proposal["level"]), (REVIEWER, None, "project"))
        self.assertNotIn("changes", proposal)
        self.assertEqual(item["detail"]["text"], "Reviews need a fixed checklist.")
        self.assertEqual([o["id"] for o in item["options"]], ["approve", "send_back"])
        await self.queue.decide(asked["review_id"], _request("approve"))
        await self.kit.until(lambda: self.saved() is not None)
        self.assertEqual(self.saved(), REVIEWER)
        listed = await self.call(self.pc_token, "list_personas")
        self.assertEqual([(p["name"], p["level"], p["tools"]) for p in listed["personas"]],
                         [("reviewer", "project", ["read", "search", "list"])])

    async def test_a_change_shows_the_diff_and_never_overwrites_a_newer_edit(self) -> None:
        self.write_agent()
        changed = REVIEWER.replace("Never change files.", "Never change files. Check the tests too.")
        asked = await self.call(self.pc_token, "create_persona", text=changed)
        item = await self.queue.get_item(asked["review_id"])
        self.assertEqual(item["title"], "Change the agent “reviewer” in project demo")
        proposal = item["detail"]["create_persona"]
        self.assertIn("+Review the diff. Never change files. Check the tests too.", proposal["changes"])
        self.assertTrue(proposal["base_version"].startswith("sha256:"))
        self.write_agent(REVIEWER + "The owner's own line.\n")          # the owner edits it meanwhile
        await self.queue.decide(asked["review_id"], _request("approve"))
        await self.kit.until(lambda: any("wasn't saved" in t for (t,) in self.rows("SELECT text FROM messages")))
        self.assertEqual(self.saved(), REVIEWER + "The owner's own line.\n")

    async def test_send_back_saves_nothing(self) -> None:
        asked = await self.call(self.pc_token, "create_persona", text=REVIEWER)
        await self.queue.decide(asked["review_id"], _request("send_back", note="Not now."))
        self.assertIsNone(self.saved())

    async def test_the_space_level_saves_in_the_spaces_folder(self) -> None:
        asked = await self.call(self.pc_token, "create_persona", text=REVIEWER, level="space")
        await self.queue.decide(asked["review_id"], _request("approve"))
        await self.kit.until(lambda: (agents_dir() / "reviewer.md").exists())
        self.assertEqual((agents_dir() / "reviewer.md").read_text(encoding="utf-8"), REVIEWER)
        self.assertIsNone(self.saved())

    async def test_a_file_with_mistakes_or_a_secret_or_no_change_is_refused_at_once(self) -> None:
        self.assertIn("mistakes to fix first", await self.refused(
            self.pc_token, "create_persona", text=REVIEWER.replace("tools:", "hooks: {}\ntools:")))
        self.assertIn("front matter", await self.refused(self.pc_token, "create_persona", text="no front matter"))
        self.assertIn("looks like a secret", await self.refused(
            self.pc_token, "create_persona", text=REVIEWER + "Use ghp_" + "a" * 36 + "\n"))
        self.write_agent()
        self.assertIn("already reads exactly this", await self.refused(self.pc_token, "create_persona",
                                                                       text=REVIEWER))
        self.assertEqual(self.rows("SELECT count(*) FROM review_items"), [(0,)])
