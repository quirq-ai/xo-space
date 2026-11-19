"""The person deletes a thread (chats/deletion.py; xo-coworker's DELETE-SPEC
of 2026-11-19), on a real git project and the engine, through the routes:
``can_delete`` on the rows; what a delete would lose (files, commits on no
branch, a running turn, queued inputs, timers, open questions) refusing it
until confirmed; the stop, the worktree removed with its branch kept, the
thread hidden everywhere and ``thread_deleted``; the undo bringing it all
back, its tree from the kept branch; ``undo_expired``; and the purge, which
keeps the founder's records and the branch."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import chats as storm_chats
from services.storage.layout import turn_logs_dir
from services.cowork_agent.storm.chats import attachments, deletion, rows
from services.cowork_agent.storm.chats.conversation import Conversation
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.memory import MemoryReading
from services.cowork_agent.storm.records import ledger, tasks, timers
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.stream.frames import Filter
from services.cowork_agent.storm.stream.replay import read_page
from services.cowork_agent.storm.worktrees import place, trees
from services.cowork_agent.storm.worktrees.service import Worktrees
from services.xo_structure import ATTACHMENTS_DIRNAME, XO_DIRNAME
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit, message
from tests.storm_worktrees_kit import branches, git_env, sh, worktree_paths

CHAT = "chat_p0000000001"
TS = "cmsg_0000000000000001"
OTHER = "cmsg_0000000000000002"
KEY = "k" * 64
WRITER = {"X-XO-UI-Writer": KEY}


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    return application


def running_turn() -> list[dict]:
    """A turn that writes a file, then runs a long command until it is
    interrupted."""
    return [*claude.initialize(), claude.user_message(), claude.init(SID),
            {"write": {"path": "wip.txt", "text": "half done"}},
            claude.tool_call(SID, "t1", "Bash", {"command": "sleep 30"}), {"sleep": 30},
            *claude.host_control("interrupt"),
            claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
            claude.idle(SID)]


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        env = git_env()
        env.start()
        self.addCleanup(env.stop)
        self.kit = Kit()
        self.project = self.kit.work
        sh(self.project, "init", "-q", "-b", "main")
        (self.project / "README.md").write_text("a project\n")
        (self.project / ".gitignore").write_text("node_modules/\n.env\n")
        sh(self.project, "add", "README.md", ".gitignore")
        sh(self.project, "commit", "-q", "-m", "first")
        self.trees = Worktrees()
        self.queue = ReviewQueue(Conversation())
        self.chats: storm_chats.Chats | None = None
        writer = mock.patch("routers.cowork_agent.bff.ui_writer.writer_key.matches", lambda got: got == KEY)
        writer.start()
        self.addCleanup(writer.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await storm_chats.shutdown()
        await self.queue.stop()
        await self.trees.stop()
        await self.kit.close()

    async def boot(self, steps: list[dict] | None = None) -> None:
        """The engine, its worktrees, the review queue and the chats. Without
        ``steps`` no session's process starts (the memory guard holds each
        back): its inputs stay queued, and a test makes the tree itself."""
        self.kit.scenario(steps if steps is not None else claude.initialize())
        if steps is None:
            self.kit.reading = MemoryReading(pressure_avg10=100.0)
        self.engine = await self.kit.boot(desk=self.queue, preparers=[self.trees])
        await self.queue.start(self.engine)
        self.chats = storm_chats.boot(self.engine, self.queue)
        self.trees.start(self.engine)

    async def thread(self, ts: str = TS, *, tree: bool = True) -> str:
        """A thread rooted on the owner's timeline message ``ts``, with a
        message in it, its task, and its session; its tree made (as its
        first start would) unless ``tree`` is false."""
        cwd = place(self.project, ts).cwd

        def chat(w: Writes) -> None:
            w.execute("INSERT OR IGNORE INTO chats (id, kind, project_pid, project_name, created_at) "
                      "VALUES (?, 'project', 'pid-demo', 'demo', ?)", (CHAT, w.now))
            rows.add_message(w, chat_id=CHAT, author_kind="owner", author_id="owner", text="build it",
                             message_id=ts)

        def add(w: Writes, sid: str) -> None:
            rows.add_thread(w, chat_id=CHAT, ts=ts, title="a thread", session_id=sid, cwd=cwd)
            rows.add_message(w, chat_id=CHAT, thread_ts=ts, author_kind="agent", author_id="thread",
                             author_session_id=sid, text="on it",
                             extra={"attachments": [{"id": "att_" + "a" * 24, "name": "x.txt"}]})
            rows.add_hop(w, chat_id=CHAT, thread_ts=ts, kind="brief", from_role="project-coordinator",
                         from_session=None, to_role="thread", to_session=sid, note="do it")
            w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, 1, '{}', ?)",
                      (CHAT, ts, w.now))
            task, _ = tasks.create(w, chat_id=CHAT, thread_ts=ts, title="build it", instructions="do it",
                                   asked_by=ledger.FOUNDER, by=ledger.FOUNDER, request_key=f"task:{ts}")
            tasks.report(w, task, text="half way", actor=sid)
        await self.engine.write(chat)
        sid = await self.engine.create_session(
            c.SessionSpec(cwd=cwd, permissions=c.PermissionPolicy(mode="ask")), agent=AGENT, role="thread",
            preset="thread", chat_id=CHAT, thread_ts=ts, also=add)
        if tree:
            made = await trees.ensure(self.project, ts, chat_id=CHAT)
            await self.engine.write(lambda w: w.execute(
                "UPDATE threads SET worktree_path = ?, branch = ?, base_commit = ? WHERE chat_id = ? AND ts = ?",
                (str(made.path), made.branch, made.base_commit, CHAT, ts)))
        return sid

    def tree(self, ts: str = TS) -> Path:
        return trees.tree_path(self.project, ts)

    def branch(self, ts: str = TS) -> str:
        return self.kit.rows("SELECT branch FROM threads WHERE chat_id = ? AND ts = ?", (CHAT, ts))[0][0]

    def deleted_at(self, ts: str = TS) -> str | None:
        return self.kit.rows("SELECT deleted_at FROM threads WHERE chat_id = ? AND ts = ?", (CHAT, ts))[0][0]

    async def delete(self, ts: str = TS, **body) -> httpx.Response:
        return await self.client.request("DELETE", f"/api/chat/chats/{CHAT}/threads/{ts}", headers=WRITER,
                                         json={"keep_branch": True, **body})

    async def restore(self, ts: str = TS) -> httpx.Response:
        return await self.client.post(f"/api/chat/chats/{CHAT}/threads/{ts}/restore", headers=WRITER)

    async def streamed(self) -> list[tuple]:
        """What a client of the chat's stream replays from the start."""
        return [(e.type, e.data) for e in await read_page(self.engine.store, Filter(chat=CHAT), limit=10_000)]

    async def paused(self, sid: str) -> str | None:
        return (await self.engine.get_session(sid))["paused"]


class CanDeleteTests(Case):
    async def test_every_row_and_the_new_threads_event_say_whether_it_can_be_deleted(self) -> None:
        await self.boot()
        await self.thread(tree=False)
        listed = (await self.client.get(f"/api/chat/chats/{CHAT}/threads")).json()["threads"]
        self.assertEqual([(t["ts"], t["can_delete"]) for t in listed], [(TS, True)])
        self.assertIs((await self.client.get(f"/api/chat/chats/{CHAT}/threads/{TS}")).json()["can_delete"], True)
        (event,) = [e for e in self.kit.recorder.of("thread") if e.thread_ts == TS and "title" in e.data]
        self.assertIs(event.data["can_delete"], True)

    async def test_a_retired_chats_threads_cant_be_deleted(self) -> None:
        await self.boot()
        await self.thread(tree=False)
        self.kit.execute("UPDATE chats SET retired_at = '2026-11-19T00:00:00Z' WHERE id = ?", (CHAT,))
        self.assertIs((await self.client.get(f"/api/chat/chats/{CHAT}/threads/{TS}")).json()["can_delete"], False)
        refused = await self.delete()
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "chat_retired"))

    async def test_delete_and_restore_need_the_ui_writer_key(self) -> None:
        await self.boot()
        await self.thread(tree=False)
        for response in (await self.client.request("DELETE", f"/api/chat/chats/{CHAT}/threads/{TS}",
                                                   json={"keep_branch": True}),
                         await self.client.post(f"/api/chat/chats/{CHAT}/threads/{TS}/restore")):
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "ui_only"))
        self.assertIsNone(self.deleted_at())

    async def test_the_branch_is_always_kept(self) -> None:
        await self.boot()
        await self.thread()
        refused = await self.delete(keep_branch=False)
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (400, "keep_branch_required"))
        self.assertTrue(self.tree().exists())


class DeleteTests(Case):
    async def test_a_clean_thread_is_deleted_at_once_its_branch_kept_and_hidden_everywhere(self) -> None:
        await self.boot()
        sid = await self.thread()
        branch = self.branch()
        done = await self.delete()
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json(), {"deleted": True, "undo_until": deletion.undo_until(self.deleted_at())})
        # The folder went, its registration with it; the branch stays.
        self.assertFalse(self.tree().exists())
        self.assertNotIn(str(self.tree()), worktree_paths(self.project))
        self.assertIn(branch, branches(self.project))
        (removed,) = self.kit.recorder.of("worktree.removed")
        self.assertEqual(removed.data, {"by": "delete", "forced": False})
        # Its session is held, so nothing runs it.
        self.assertEqual(await self.paused(sid), deletion.PAUSE_REASON)
        (event,) = self.kit.recorder.of("thread_deleted")
        self.assertEqual((event.data, event.chat_id, event.thread_ts), ({"chat_id": CHAT, "ts": TS}, CHAT, TS))
        self.assertIn(("thread_deleted", {"chat_id": CHAT, "ts": TS}), await self.streamed())   # other clients
        # Hidden everywhere.
        gone = await self.client.get(f"/api/chat/chats/{CHAT}/threads/{TS}")
        self.assertEqual((gone.status_code, gone.json()["detail"]["code"]), (404, "thread_deleted"))
        self.assertEqual((await self.client.get(f"/api/chat/chats/{CHAT}/threads")).json()["threads"], [])
        timeline = (await self.client.get(f"/api/chat/chats/{CHAT}/messages")).json()["messages"]
        self.assertEqual(timeline, [])                                  # its root went with it
        for path in (f"/api/chat/chats/{CHAT}/messages?thread={TS}", f"/api/chat/chats/{CHAT}/threads/{TS}/events"):
            self.assertEqual((await self.client.get(path)).json()["detail"]["code"], "thread_deleted")
        self.assertEqual((await self.client.get(f"/api/chat/chats/{CHAT}/hops")).json()["hops"], [])
        self.assertEqual((await self.client.get(f"/api/chat/chats/{CHAT}/thread-events")).json()["events"], [])
        work = (await self.client.get(f"/api/chat/chats/{CHAT}/work")).json()
        self.assertEqual(work["tasks"], [])
        listing = (await self.client.get("/api/chat/worktrees", params={"chat": CHAT})).json()
        self.assertEqual((listing["worktrees"], listing["removed"]), ([], []))
        posted = await self.client.post(f"/api/chat/chats/{CHAT}/messages",
                                        json={"text": "more", "thread_ts": TS, "client_id": "c1"})
        self.assertEqual((posted.status_code, posted.json()["detail"]["code"]), (404, "thread_deleted"))
        patched = await self.client.patch(f"/api/chat/chats/{CHAT}/threads/{TS}", json={"resolved": True})
        self.assertEqual(patched.json()["detail"]["code"], "thread_deleted")

    async def test_another_thread_of_the_chat_is_untouched(self) -> None:
        await self.boot()
        await self.thread()
        await self.thread(OTHER)
        self.assertEqual((await self.delete()).status_code, 200)
        listed = (await self.client.get(f"/api/chat/chats/{CHAT}/threads")).json()["threads"]
        self.assertEqual([t["ts"] for t in listed], [OTHER])
        self.assertTrue(self.tree(OTHER).exists())
        timeline = (await self.client.get(f"/api/chat/chats/{CHAT}/messages")).json()["messages"]
        self.assertEqual([m["id"] for m in timeline], [OTHER])
        hops = (await self.client.get(f"/api/chat/chats/{CHAT}/hops")).json()["hops"]
        self.assertEqual([h["thread_ts"] for h in hops], [OTHER])

    async def test_deleting_again_answers_the_same_while_undoable_and_404_after(self) -> None:
        await self.boot()
        await self.thread()
        first = (await self.delete()).json()
        self.assertEqual((await self.delete()).json(), first)
        self.kit.clock.advance(deletion.UNDO_S + 1)
        again = await self.delete()
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (404, "thread_deleted"))

    async def test_a_thread_in_a_non_git_project_is_deleted_with_no_tree(self) -> None:
        await self.boot()
        await self.thread(tree=False)
        self.kit.execute("UPDATE threads SET worktree_path = NULL")
        self.assertEqual((await self.delete()).status_code, 200)
        self.assertEqual(self.kit.recorder.of("worktree.removed"), [])

    async def test_a_folder_that_isnt_xos_tree_for_the_thread_refuses_and_changes_nothing(self) -> None:
        await self.boot()
        sid = await self.thread()
        admin = Path(sh(self.tree(), "rev-parse", "--absolute-git-dir").strip())
        (admin / trees.MARKER).write_text(json.dumps({"thread": "another", "chat": CHAT}))
        refused = await self.delete()
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "not_xo_worktree"))
        self.assertTrue(self.tree().exists())
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))

    async def test_a_session_that_wont_let_go_of_its_process_refuses_and_is_let_go(self) -> None:
        await self.boot()
        sid = await self.thread()
        self.kit.execute("INSERT INTO procs VALUES (999999, 1, ?, 1, 'claude', '2026-11-19T00:00:00Z')", (sid,))
        with mock.patch.object(deletion, "SETTLE_S", 0.2):
            refused = await self.delete()
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "session_busy"))
        self.assertTrue(self.tree().exists())
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))               # the pause it took is lifted
        self.kit.execute("DELETE FROM procs")


class WouldLoseWorkTests(Case):
    async def test_files_and_commits_on_no_branch_refuse_it_until_confirmed(self) -> None:
        await self.boot()
        sid = await self.thread()
        tree = self.tree()
        (tree / "README.md").write_text("changed\n")
        (tree / "draft.txt").write_text("only copy")
        (tree / ".env").write_text("TOKEN=1")
        sh(tree, "checkout", "-q", "--detach")
        (tree / "lone.txt").write_text("x")
        sh(tree, "add", "lone.txt")
        sh(tree, "commit", "-q", "-m", "on no branch")

        refused = await self.delete()
        self.assertEqual(refused.status_code, 409)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "would_lose_work")
        what = detail["what"]
        self.assertEqual((what["changed"], what["untracked"], what["ignored"], what["commits"]),
                         ({"count": 1, "paths": ["README.md"]}, {"count": 1, "paths": ["draft.txt"]},
                          {"count": 1, "paths": [".env"]}, 1))
        self.assertEqual(what["branch"], self.branch())
        self.assertEqual(what["summary"], "1 uncommitted file, 1 untracked file, an ignored file (.env), 1 commit on "
                                          "no branch")
        self.assertEqual((what["running"], what["runs"], what["wakes"], what["timers"], what["asks"]),
                         (False, 0, 0, 0, 0))
        self.assertIn(detail["confirm"], detail["message"])
        # Nothing changed.
        self.assertTrue((tree / "draft.txt").exists())
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))
        self.assertEqual(self.kit.recorder.of("session.halted"), [])

        wrong = await self.delete(confirm="0" * 12)
        self.assertEqual(wrong.json()["detail"]["code"], "would_lose_work")
        done = await self.delete(confirm=detail["confirm"])
        self.assertEqual(done.status_code, 200, done.text)
        self.assertFalse(tree.exists())
        self.assertIn(self.branch(), branches(self.project))
        (removed,) = self.kit.recorder.of("worktree.removed")
        self.assertEqual((removed.data["by"], removed.data["forced"]), ("delete", True))
        self.assertEqual(removed.data["lost"]["untracked"], what["untracked"])     # what was lost, recorded

    async def test_a_new_kind_of_work_since_the_confirmation_refuses_again(self) -> None:
        await self.boot()
        await self.thread()
        (self.tree() / "draft.txt").write_text("only copy")
        first = (await self.delete()).json()["detail"]
        (self.tree() / "README.md").write_text("changed too\n")
        again = await self.delete(confirm=first["confirm"])
        self.assertEqual(again.status_code, 409)
        self.assertNotEqual(again.json()["detail"]["confirm"], first["confirm"])
        self.assertTrue(self.tree().exists())
        self.assertEqual((await self.delete(confirm=again.json()["detail"]["confirm"])).status_code, 200)

    async def test_commits_on_the_kept_branch_lose_nothing(self) -> None:
        await self.boot()
        await self.thread()
        (self.tree() / "feature.txt").write_text("done")
        sh(self.tree(), "add", "feature.txt")
        sh(self.tree(), "commit", "-q", "-m", "the work")
        tip = sh(self.tree(), "rev-parse", "HEAD").strip()
        done = await self.delete()
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(sh(self.project, "rev-parse", self.branch()).strip(), tip)

    async def test_build_folders_lose_nothing(self) -> None:
        await self.boot()
        await self.thread()
        (self.tree() / "node_modules" / "x").mkdir(parents=True)
        (self.tree() / "node_modules" / "x" / "index.js").write_text("rebuildable")
        self.assertEqual((await self.delete()).status_code, 200)

    async def test_queued_inputs_timers_and_open_questions_refuse_it_and_are_kept_for_the_undo(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.engine.deliver(sid, message(1))                     # held back by the memory guard: queued

        def pending(w: Writes) -> None:
            timers.arm(w, kind="at", chat_id=CHAT, role="thread", thread_ts=TS, due_at="2099-01-01T00:00:00Z",
                       note="look again", asked_by=sid, by=sid, request_key="timer-1")
            w.execute("INSERT INTO review_items (id, seq, chat_id, thread_ts, session_id, origin, kind, title, state, "
                      "created_at) VALUES ('rv_1', 1, ?, ?, ?, 'agent', 'question', 'Which one?', 'open', ?)",
                      (CHAT, TS, sid, w.now))
        await self.engine.write(pending)
        refused = (await self.delete()).json()["detail"]
        self.assertEqual((refused["what"]["wakes"], refused["what"]["timers"], refused["what"]["asks"]), (1, 1, 1))
        self.assertEqual(refused["what"]["summary"], "1 message waiting for it, 1 armed timer, 1 open question to you")
        done = await self.delete(confirm=refused["confirm"])
        self.assertEqual(done.status_code, 200, done.text)
        # Held, not dropped: the undo brings them back.
        self.assertEqual(self.kit.rows("SELECT state FROM wakes WHERE session_id = ?", (sid,)), [("queued",)])
        self.assertEqual(self.kit.rows("SELECT state FROM timers"), [("armed",)])
        self.assertEqual(self.kit.rows("SELECT state FROM review_items"), [("open",)])
        self.assertEqual((await self.client.get("/api/chat/review", params={"state": "open"})).json()["items"], [])

        back = await self.restore()
        self.assertEqual(back.status_code, 200, back.text)
        self.assertIsNone(await self.paused(sid))
        self.assertEqual(self.kit.rows("SELECT state FROM wakes WHERE session_id = ?", (sid,)), [("queued",)])
        self.assertEqual(len((await self.client.get("/api/chat/review", params={"state": "open"})).json()["items"]),
                         1)


    async def test_an_input_arriving_while_it_is_paused_refuses_it_and_lifts_the_pause(self) -> None:
        await self.boot()
        sid = await self.thread()
        hold = deletion._hold

        async def meanwhile(engine, session_id):
            await engine.deliver(session_id, message(1))
            return await hold(engine, session_id)
        with mock.patch.object(deletion, "_hold", meanwhile):
            refused = await self.delete()
        self.assertEqual((refused.status_code, refused.json()["detail"]["what"]["wakes"]), (409, 1))
        self.assertTrue(self.tree().exists())
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))
        self.assertEqual(self.kit.recorder.of("session.halted"), [])
        confirm = refused.json()["detail"]["confirm"]
        self.assertEqual((await self.delete(confirm=confirm)).status_code, 200)

    async def test_a_coordinator_can_no_longer_reach_a_deleted_thread(self) -> None:
        from types import SimpleNamespace

        from services.cowork_agent.storm.comms import talk
        from services.cowork_agent.storm.comms.calls import CommsError
        await self.boot()
        await self.thread()
        self.assertEqual((await self.delete()).status_code, 200)
        call = SimpleNamespace(caller=SimpleNamespace(role="project-coordinator", chat_id=CHAT, thread_ts=None))
        with self.assertRaises(CommsError) as caught:
            await self.engine.store.read(lambda conn: talk._thread(conn, call, TS))
        self.assertEqual(caught.exception.code, "thread_deleted")


class RunningTests(Case):
    async def test_a_running_turn_refuses_it_then_is_stopped_and_what_it_wrote_goes_with_it(self) -> None:
        await self.boot(running_turn())
        sid = await self.thread(tree=False)                     # its first start makes the tree
        await self.engine.deliver(sid, message(1))
        started = await self.kit.recorder.seen("turn.started", session_id=sid)
        await self.kit.until(lambda: (self.tree() / "wip.txt").exists())

        refused = (await self.delete()).json()["detail"]
        self.assertEqual(refused["code"], "would_lose_work")
        self.assertIs(refused["what"]["running"], True)
        self.assertIn("a running turn", refused["what"]["summary"])
        self.assertEqual((await self.engine.get_session(sid))["state"], "running")     # nothing was stopped

        done = await self.delete(confirm=refused["confirm"])
        self.assertEqual(done.status_code, 200, done.text)
        stopped = await self.kit.recorder.seen("turn.stopped", session_id=sid)
        self.assertEqual((stopped.turn_id, stopped.data["by"]), (started.turn_id, "person"))
        (halted,) = self.kit.recorder.of("session.halted", sid)
        self.assertEqual((halted.data["reason"], halted.data["by"]), ("thread_deleted", "person"))
        self.assertEqual((await self.engine.get_session(sid))["state"], "cold")
        self.assertEqual(self.kit.rows("SELECT pid FROM procs"), [])
        self.assertFalse(self.tree().exists())
        self.assertIn(self.branch(), branches(self.project))


class RestoreTests(Case):
    async def test_the_undo_brings_back_the_row_its_messages_and_its_tree_from_the_kept_branch(self) -> None:
        await self.boot()
        sid = await self.thread()
        (self.tree() / "feature.txt").write_text("committed work")
        sh(self.tree(), "add", "feature.txt")
        sh(self.tree(), "commit", "-q", "-m", "the work")
        (self.tree() / "scratch.txt").write_text("never committed")
        refused = (await self.delete()).json()["detail"]
        self.assertEqual((await self.delete(confirm=refused["confirm"])).status_code, 200)
        branch = self.branch()

        back = await self.restore()
        self.assertEqual(back.status_code, 200, back.text)
        row = back.json()
        self.assertEqual((row["ts"], row["can_delete"], row["worktree"]),
                         (TS, True, {"path": str(self.tree()), "branch": branch}))
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))
        # The tree is back on its branch at its tip; what was never committed is gone, as confirmed.
        self.assertEqual((self.tree() / "feature.txt").read_text(), "committed work")
        self.assertFalse((self.tree() / "scratch.txt").exists())
        self.assertEqual(sh(self.tree(), "rev-parse", "--abbrev-ref", "HEAD").strip(), branch)
        self.assertIn(str(self.tree()), worktree_paths(self.project))
        found = await trees.find(self.project, self.tree())
        self.assertTrue(found.ours(CHAT, TS))
        # Everything shows again, and other clients get the whole row.
        self.assertEqual((await self.client.get(f"/api/chat/chats/{CHAT}/threads/{TS}")).status_code, 200)
        self.assertEqual([m["id"] for m in (await self.client.get(f"/api/chat/chats/{CHAT}/messages")).json()
                          ["messages"]], [TS])
        self.assertEqual([m["text"] for m in (await self.client.get(
            f"/api/chat/chats/{CHAT}/messages", params={"thread": TS})).json()["messages"]], ["on it"])
        restored = [e for e in self.kit.recorder.of("thread") if e.thread_ts == TS and "title" in e.data][-1]
        self.assertEqual((restored.data["chat_id"], restored.data["can_delete"]), (CHAT, True))
        self.assertEqual(len(self.kit.recorder.of("worktree.restored")), 1)
        # A second Undo answers the same, changing nothing.
        again = await self.restore()
        self.assertEqual((again.status_code, again.json()["worktree"]), (200, row["worktree"]))

    async def test_a_thread_whose_tree_cant_be_made_again_is_restored_without_it(self) -> None:
        await self.boot()
        sid = await self.thread()
        self.assertEqual((await self.delete()).status_code, 200)
        self.tree().write_text("something in the way")
        back = await self.restore()
        self.assertEqual(back.status_code, 200, back.text)
        self.assertIsNone(back.json()["worktree"])
        self.assertIsNone(self.deleted_at())
        self.assertIsNone(await self.paused(sid))
        self.assertIn(self.branch(), branches(self.project))

    async def test_after_undo_until_it_is_gone(self) -> None:
        await self.boot()
        await self.thread()
        self.assertEqual((await self.delete()).status_code, 200)
        self.kit.clock.advance(deletion.UNDO_S)
        late = await self.restore()
        self.assertEqual((late.status_code, late.json()["detail"]["code"]), (410, "undo_expired"))
        self.assertIsNotNone(self.deleted_at())
        self.assertFalse(self.tree().exists())

    async def test_a_session_the_loop_breaker_paused_stays_paused_through_delete_and_undo(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.engine.pause(sid, reason="loop")
        self.assertEqual((await self.delete()).status_code, 200)
        self.assertEqual(await self.paused(sid), "loop")
        self.assertEqual((await self.restore()).status_code, 200)
        self.assertEqual(await self.paused(sid), "loop")


class PurgeTests(Case):
    async def deleted(self) -> str:
        await self.boot()
        sid = await self.thread()
        await self.engine.deliver(sid, message(1))
        folder = attachments.attachments_root(self.project, create=True)
        (folder / ("att_" + "a" * 24)).mkdir()
        copies = self.project / XO_DIRNAME / "made" / f"thread-{TS}"
        copies.mkdir(parents=True)
        (copies / "shared.txt").write_text("a copy")
        refused = (await self.delete()).json()["detail"]
        self.assertEqual((await self.delete(confirm=refused["confirm"])).status_code, 200)
        return sid

    async def test_the_purge_waits_its_hour_then_removes_the_content_and_keeps_the_records(self) -> None:
        sid = await self.deleted()
        branch = self.branch()
        # What its turns did, which the purge keeps (the owner's choice of 11-19).
        await self.engine.write(lambda w: (
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at, ended_at) VALUES "
                      "('turn_kept', ?, 0, 'person', 'finished', ?, ?)", (sid, w.now, w.now)),
            w.append("step.tool", {"call_id": "c1", "tool": "Bash", "title": "ls"}, chat_id=CHAT, session_id=sid,
                     thread_ts=TS, turn_id="turn_kept"),
            w.append("turn.finished", {"status": "finished"}, chat_id=CHAT, session_id=sid, thread_ts=TS,
                     turn_id="turn_kept")))
        raw = turn_logs_dir() / "turn_kept.jsonl"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text('{"type":"assistant"}\n')
        self.assertEqual(await deletion.purge_due(self.chats), 0)
        self.kit.clock.advance(deletion.PURGE_AFTER_S + 1)
        self.assertEqual(await deletion.purge_due(self.chats), 1)

        self.assertEqual(self.kit.rows("SELECT count(*) FROM messages"), [(0,)])
        for table in ("cards", "hops", "wakes", "made", "thread_git"):
            self.assertEqual(self.kit.rows(f"SELECT count(*) FROM {table}"), [(0,)], table)
        left = {t for (t,) in self.kit.rows("SELECT DISTINCT type FROM events WHERE thread_ts = ? OR session_id = ?",
                                            (TS, sid))}
        self.assertLessEqual({t for t in left if not t.startswith(deletion.KEPT_EVENT_FAMILIES)},
                             {*deletion.KEPT_EVENTS, "session"})
        self.assertIn("thread_deleted", left)
        # What its turns did stays: its steps, its turns' events and their raw logs.
        self.assertEqual(self.kit.rows("SELECT type, json_extract(data, '$.title') FROM events WHERE "
                                       "turn_id = 'turn_kept' ORDER BY seq"),
                         [("step.tool", "ls"), ("turn.finished", None)])
        self.assertTrue(raw.exists())
        # ...and stays hidden with the thread: no replay or events page shows it.
        from services.cowork_agent.storm.stream.replay import visible
        page = await read_page(self.engine.store, Filter(chat=CHAT), limit=10_000)
        self.assertTrue(any(e.turn_id == "turn_kept" for e in page))           # stored
        self.assertFalse(any(e.turn_id == "turn_kept" for e in await visible(self.engine.store, page)))
        self.assertIn("thread_deleted", [e.type for e in await visible(self.engine.store, page)])

        # A content-free tombstone, its session retired.
        self.assertEqual(self.kit.rows("SELECT title, status_md, summary, deleted_at IS NOT NULL, "
                                       "purged_at IS NOT NULL FROM threads"), [(None, None, None, 1, 1)])
        self.assertEqual(self.kit.rows("SELECT retired_at IS NOT NULL FROM sessions WHERE id = ?", (sid,)), [(1,)])
        # The founder's records stay, moved through their writers: the ledger still verifies.
        self.assertEqual(self.kit.rows("SELECT state FROM tasks"), [("cancelled",)])
        self.assertEqual(self.kit.rows("SELECT text FROM task_reports"), [("half way",)])
        self.assertEqual(await self.engine.store.read(ledger.verify), [])
        # Files: the attachments and Made's copies go; the branch stays.
        self.assertFalse((self.project / XO_DIRNAME / ATTACHMENTS_DIRNAME / ("att_" + "a" * 24)).exists())
        self.assertFalse((self.project / XO_DIRNAME / "made" / f"thread-{TS}").exists())
        self.assertIn(branch, branches(self.project))
        # A client resuming from before the deletion still hears of it.
        self.assertIn(("thread_deleted", {"chat_id": CHAT, "ts": TS}), await self.streamed())
        # And it is gone for good.
        self.assertEqual((await self.restore()).status_code, 410)
        self.assertEqual((await self.delete()).json()["detail"]["code"], "thread_deleted")
        self.assertEqual(await deletion.purge_due(self.chats), 0)

    async def test_a_live_thread_and_one_still_undoable_are_never_purged(self) -> None:
        await self.boot()
        await self.thread()
        self.assertFalse(await deletion.purge(self.chats, CHAT, TS))
        self.assertEqual((await self.delete()).status_code, 200)
        self.assertFalse(await deletion.purge(self.chats, CHAT, TS))
        self.assertEqual(self.kit.rows("SELECT count(*) FROM messages"), [(2,)])


if __name__ == "__main__":
    unittest.main()
