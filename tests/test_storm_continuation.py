"""Continuing a thread's work in a new linked thread (mixed-runtime plan,
step 9): ``chats/continuation.py`` and the coordinator's tool."""

from __future__ import annotations

import json
import subprocess

from services.cowork_agent.storm.chats import continuation
from services.cowork_agent.storm.worktrees import trees
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT


def sh(cwd, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ContinueTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.root = await self.owner_message(self.chat, "Fix the login page")
        started = await self.call(self.pc_token, "start_thread_session", message_id=self.root["id"],
                                  title="Fix login", instructions="The form posts to the wrong URL.", **CHOICE)
        self.old_session = started["session_id"]
        self.asked = await self.owner_message(self.chat, "Move the login work to the other runtime")

    async def test_a_coordinator_moves_a_thread_the_owner_asked_to_move(self) -> None:
        moved = await self.call(self.pc_token, "continue_thread", thread_id=self.root["id"], agent=AGENT,
                                reason="the owner asked", context_message_ids=[self.asked["id"]])
        new_ts = moved["thread_id"]
        self.assertEqual((moved["from_thread_id"], moved["agent"], moved["wip"]), (self.root["id"], AGENT, None))
        # The new thread: on a timeline message that says where it came from, its own session.
        (text, extra), = self.rows("SELECT text, extra FROM messages WHERE id = ?", (new_ts,))
        self.assertTrue(text.startswith("Continuing “Fix login” on"))
        self.assertEqual(json.loads(extra)["continued_from"], self.root["id"])
        self.assertEqual(self.rows("SELECT title, session_id FROM threads WHERE ts = ?", (new_ts,)),
                         [("Fix login (continued)", moved["session_id"])])
        # Its first input is XO's handoff, ending with the checkpoint.
        (envelope,) = [w[2] for w in self.wakes(moved["session_id"]) if w[0] == f"continue:{self.root['id']}"]
        self.assertIn(f'<continuation from-thread="{self.root["id"]}"', envelope)
        self.assertIn("Fix the login page", envelope)                     # the original request, word for word
        self.assertIn("The form posts to the wrong URL.", envelope)        # the brief
        self.assertIn("checkpoint", envelope)
        # The old thread: frozen, linked, and it says so.
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (self.root["id"],)), [("paused",)])
        self.assertIsNotNone(self.rows("SELECT paused FROM sessions WHERE id = ?", (self.old_session,))[0][0])
        self.assertEqual(await self.kit.engine.store.read(
            lambda conn: continuation.continued_to(conn, self.chat["id"], self.root["id"])), new_ts)
        notes = [t for (t,) in self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                                         (self.root["id"],))]
        self.assertTrue(any(n.startswith("Continued in [Fix login (continued)]") for n in notes))
        # Once only.
        self.assertIn("continued in thread", await self.refused(
            self.pc_token, "continue_thread", thread_id=self.root["id"], reason="again",
            context_message_ids=[self.asked["id"]]))

    async def test_on_its_own_a_coordinator_moves_only_a_thread_whose_runtime_cant_take_work(self) -> None:
        self.assertIn("only when the owner asks", await self.refused(
            self.pc_token, "continue_thread", thread_id=self.root["id"], reason="I think it's better"))
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (self.root["id"],)), [("working",)])

    async def test_a_runtime_the_project_doesnt_allow_is_refused_before_anything_changes(self) -> None:
        self.assertIn("has no storm runtime", await self.refused(
            self.pc_token, "continue_thread", thread_id=self.root["id"], agent="nope", reason="x",
            context_message_ids=[self.asked["id"]]))
        self.assertEqual(self.rows("SELECT paused FROM sessions WHERE id = ?", (self.old_session,)), [(None,)])


class SnapshotTests(CommsTestCase):
    async def test_the_old_worktree_is_committed_and_the_new_thread_branches_from_it(self) -> None:
        await self.boot()
        project = self.kit.projects / "repo"
        project.mkdir()
        sh(project, "init", "-q", "-b", "main")
        (project / "app.py").write_text("print('v1')\n")
        sh(project, "add", "-A")
        sh(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        base = sh(project, "rev-parse", "HEAD")
        chat = await self.project("repo")
        old_tree = await trees.ensure(project, "cmsg_old", chat_id=chat["id"])
        (old_tree.path / "app.py").write_text("print('v2, half done')\n")
        (old_tree.path / "new.py").write_text("x = 1\n")                 # untracked: kept too
        old = {"ts": "cmsg_old", "worktree_path": str(old_tree.path), "base_commit": base}
        wip = await continuation._snapshot(self.chats, chat, old, "cmsg_new")
        self.assertEqual(sh(old_tree.path, "rev-parse", "HEAD"), wip["sha"])
        self.assertIn("WIP: thread cmsg_old continued in thread cmsg_new", sh(old_tree.path, "log", "-1", "--format=%s"))
        new_path = trees.tree_path(project, "cmsg_new")
        self.assertEqual(sh(new_path, "rev-parse", "HEAD"), wip["sha"])
        self.assertEqual(sh(new_path, "rev-parse", "--abbrev-ref", "HEAD"), wip["branch"])
        self.assertEqual((new_path / "new.py").read_text(), "x = 1\n")
        self.assertEqual(wip["base"], base)
        self.assertIn("app.py", wip["stat"])
        self.assertIn("new.py", wip["stat"])
