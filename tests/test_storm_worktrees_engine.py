"""Storm's worktrees in the engine's life (design ch 13 §2, §4–§6) on the fake
claude CLI and a real temp git project: the preparer makes and records a
thread's tree before its process starts (and its failure paths), a tree
removed while the thread was resolved comes back at the same path, a
worktree a CLI made is reported once, the 7-day sweep, and the routes."""

from __future__ import annotations

import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.worktrees import git as wgit
from services.cowork_agent.storm.worktrees import place, trees
from services.cowork_agent.storm.worktrees.service import Worktrees
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit, message
from services.errors import ServiceError
from tests.storm_worktrees_kit import branches, git_env, head, repo, sh
from utils.commands import CommandResult

CHAT = "chat_p0000000001"
TS = "1760518860.000100"
DAY = 24 * 3600


def started(*after: dict) -> list[dict]:
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


def one_turn() -> list[dict]:
    return started(*claude.text_turn(SID, "done"), claude.idle(SID))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        env = git_env()
        env.start()
        self.addCleanup(env.stop)
        self.project = repo(self.kit.work)
        self.trees = Worktrees()
        self.engine = None

    async def asyncTearDown(self) -> None:
        await self.trees.stop()
        await self.kit.close()

    async def boot(self, steps: list[dict] | None = None) -> None:
        self.kit.scenario(steps if steps is not None else one_turn())
        self.engine = await self.kit.boot(preparers=[self.trees])
        self.trees.start(self.engine)

    async def thread(self, ts: str = TS, *, state: str = "working", resolved_days: float | None = None) -> str:
        """A thread of the project's chat, with its session (cold, never run)."""
        cwd = place(self.project, ts).cwd
        resolved = iso(self.kit.clock.time() - resolved_days * DAY) if resolved_days is not None else None

        def add(w: Writes) -> None:
            w.execute("INSERT OR IGNORE INTO chats (id, kind, project_pid, project_name, created_at) "
                      "VALUES (?, 'project', 'pid-demo', 'demo', ?)", (CHAT, w.now))
            w.execute("INSERT INTO threads (chat_id, ts, title, state, resolved_at, cwd, created_at, updated_at) "
                      "VALUES (?, ?, 'a thread', ?, ?, ?, ?, ?)", (CHAT, ts, state, resolved, cwd, w.now, w.now))
        await self.engine.write(add)
        return await self.engine.create_session(
            c.SessionSpec(cwd=cwd, permissions=c.PermissionPolicy(mode="ask")), agent=AGENT, role="thread",
            preset="thread", chat_id=CHAT, thread_ts=ts,
            also=lambda w, sid: w.execute("UPDATE threads SET session_id = ? WHERE chat_id = ? AND ts = ?",
                                          (sid, CHAT, ts)))

    def row(self, ts: str = TS) -> dict:
        names = ("worktree_path", "branch", "base_commit", "worktrees_seen")
        (values,) = self.kit.rows(f"SELECT {', '.join(names)} FROM threads WHERE chat_id = ? AND ts = ?", (CHAT, ts))
        return dict(zip(names, values))

    def events(self, type_: str, ts: str = TS) -> list:
        return [e for e in self.kit.recorder.of(type_) if e.thread_ts == ts]

    async def run_turn(self, sid: str, n: int, *, count: int = 1) -> None:
        await self.engine.deliver(sid, message(n))
        await self.kit.recorder.seen("turn.finished", session_id=sid, count=count)
        await self.settled()

    async def release(self, sid: str, count: int = 1) -> None:
        await self.kit.idle_timer()
        self.kit.clock.advance(600)
        await self.kit.recorder.seen("session", session_id=sid, status="cold", reason="idle", count=count)
        await self.kit.recorder.wait(lambda: not self.kit.rows("SELECT pid FROM procs"))

    async def settled(self) -> None:
        """The listener's background look at the project's worktrees is done."""
        await self.kit.until(lambda: not self.trees._tasks)


class PreparerTests(Case):
    async def test_the_first_start_makes_the_tree_and_records_it_on_the_thread(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.run_turn(sid, 1)
        path = trees.tree_path(self.project, TS)
        (run,) = self.kit.runs()
        self.assertEqual(run["cwd"], str(path))                       # the CLI ran in the tree
        row = self.row()
        self.assertEqual((row["worktree_path"], row["base_commit"]), (str(path), head(self.project)))
        self.assertRegex(row["branch"], r"^xo/thread-[0-9a-f]{6}$")
        self.assertEqual(json.loads(row["worktrees_seen"]), [])
        (event,) = self.events("thread")
        self.assertEqual(event.data, {"ts": TS, "worktree_path": str(path), "branch": row["branch"],
                                      "base_commit": row["base_commit"]})
        self.assertEqual((event.chat_id, event.session_id), (CHAT, None))

    async def test_a_later_start_reuses_the_tree_and_records_nothing_new(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.run_turn(sid, 1)
        path = trees.tree_path(self.project, TS)
        (path / "draft.txt").write_text("work in progress")
        await self.release(sid)
        await self.run_turn(sid, 2, count=2)
        first, second = self.kit.runs()
        self.assertEqual((first["cwd"], second["cwd"]), (str(path), str(path)))
        self.assertIn("--resume", second["argv"])
        self.assertEqual((path / "draft.txt").read_text(), "work in progress")
        self.assertEqual(len(self.events("thread")), 1)

    async def test_a_plain_session_in_the_project_folder_gets_no_tree(self) -> None:
        await self.boot()
        plain = await self.kit.session()                  # cwd: the project folder itself
        await self.run_turn(plain, 1)
        self.assertFalse((self.project / ".xo" / "worktrees").exists())

    async def test_a_symlinked_xo_holds_the_session_for_the_person_and_is_not_retried(self) -> None:
        elsewhere = self.kit.root / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".xo").symlink_to(elsewhere, target_is_directory=True)
        await self.boot()
        sid = await self.thread()
        await self.engine.deliver(sid, message(1))
        failed = await self.kit.recorder.seen("session", session_id=sid, reason="open_failed")
        self.assertFalse(failed.data["retryable"])
        self.assertIn("symlink", failed.data["error"])
        for _ in range(60):                                    # an hour, a minute at a time
            self.kit.clock.advance(60)
            await asyncio.sleep(0.005)
        self.assertEqual(self.kit.runs(), [])                  # never opened, never retried
        self.assertEqual(self.kit.rows("SELECT state FROM wakes"), [("queued",)])
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue((await self.engine.status())["sessions"]["backing_off"][sid]["until_next_message"])
        # The person fixes it and writes again.
        (self.project / ".xo").unlink()
        await self.engine.deliver(sid, message(2))
        await self.kit.recorder.seen("turn.finished", session_id=sid)

    async def test_a_git_failure_is_retried_with_the_backoff(self) -> None:
        await self.boot()
        sid = await self.thread()
        locked = CommandResult(argv=["git"], returncode=128, output="", duration_seconds=0,
                               stderr="fatal: Unable to create '.git/index.lock': File exists.")
        real = trees.ensure
        calls = []

        async def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise wgit.GitFailed("add the thread's worktree", locked)
            return await real(*args, **kwargs)
        with mock.patch.object(trees, "ensure", flaky):
            await self.engine.deliver(sid, message(1))
            failed = await self.kit.recorder.seen("session", session_id=sid, reason="open_failed")
            self.assertTrue(failed.data["retryable"])
            self.assertIn("index.lock", failed.data["error"])
            await self.kit.until(lambda: f"retry:{sid}" in self.engine._timers)
            self.kit.clock.advance(2)
            await self.kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(len(calls), 2)

    async def test_a_tree_removed_while_resolved_comes_back_at_the_same_path_and_the_cli_resumes(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.run_turn(sid, 1)
        before = self.row()
        native = (await self.engine.get_session(sid))["native_id"]
        await self.release(sid)
        await self.trees.remove(CHAT, TS)                      # clean: removed, its merged branch deleted
        path = trees.tree_path(self.project, TS)
        self.assertFalse(path.exists())
        self.assertNotIn(before["branch"], branches(self.project))
        await self.run_turn(sid, 2, count=2)
        first, second = self.kit.runs()
        self.assertEqual(second["cwd"], str(path))
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], native)   # same path, same transcript
        after = self.row()
        self.assertNotEqual(after["branch"], before["branch"])
        self.assertEqual(after["base_commit"], before["base_commit"])
        # Only what changed is in the thread's event; the agent is told how its tree came back.
        self.assertEqual(self.events("thread")[-1].data, {"ts": TS, "branch": after["branch"]})
        text = second["stdin"][-1]["message"]["content"][0]["text"]
        self.assertIn(after["branch"], text)
        self.assertIn("hello", text)


class ReportedTests(Case):
    async def test_a_worktree_a_cli_made_during_a_turn_is_reported_once(self) -> None:
        await self.boot(started({"sleep": 1.0}, *claude.text_turn(SID, "one"), claude.idle(SID),
                                claude.user_message(), *claude.text_turn(SID, "two", message_id="m2"),
                                claude.idle(SID)))
        sid = await self.thread()
        await self.engine.deliver(sid, message(1))
        await self.kit.recorder.seen("turn.started", session_id=sid)
        made = self.project / ".claude" / "worktrees" / "agent-a0123456789abcdef"
        sh(self.project, "worktree", "add", "-q", "-b", "worktree-agent-a0123456789abcdef", str(made))
        reported = await self.kit.recorder.seen("worktree.reported")
        self.assertEqual((reported.data, reported.chat_id, reported.thread_ts), ({"path": str(made)}, CHAT, TS))
        await self.settled()
        await self.run_turn(sid, 2, count=2)
        self.assertEqual(len(self.kit.recorder.of("worktree.reported")), 1)
        self.assertEqual(json.loads(self.row()["worktrees_seen"]), [str(made)])
        self.assertTrue(made.exists())                         # never touched


class SweepTests(Case):
    async def made(self, ts: str, *, resolved_days: float) -> str:
        """A resolved thread whose tree exists and whose session is cold."""
        sid = await self.thread(ts, state="resolved", resolved_days=resolved_days)
        tree = await trees.ensure(self.project, ts, chat_id=CHAT)

        def record(w: Writes) -> None:
            w.execute("UPDATE threads SET worktree_path = ?, branch = ?, base_commit = ? WHERE chat_id = ? AND ts = ?",
                      (str(tree.path), tree.branch, tree.base_commit, CHAT, ts))
        await self.engine.write(record)
        return sid

    async def test_a_tree_resolved_over_7_days_ago_with_a_cold_session_is_removed(self) -> None:
        await self.boot()
        await self.made("old", resolved_days=8)
        await self.made("recent", resolved_days=6)
        await self.trees.sweep()
        self.assertFalse(trees.tree_path(self.project, "old").exists())
        self.assertTrue(trees.tree_path(self.project, "recent").exists())
        (removed,) = self.kit.recorder.of("worktree.removed")
        self.assertEqual((removed.thread_ts, removed.data), ("old", {"by": "sweep", "forced": False}))

    async def test_a_live_session_keeps_its_tree_whatever_its_age(self) -> None:
        await self.boot()
        sid = await self.made("old", resolved_days=30)
        await self.run_turn(sid, 1)                           # warm now
        self.assertEqual((await self.engine.get_session(sid))["state"], "warm")
        await self.trees.sweep()
        self.assertTrue(trees.tree_path(self.project, "old").exists())
        self.assertEqual(self.kit.recorder.of("worktree.removed"), [])

    async def test_a_tree_holding_work_is_kept_and_the_thread_says_why_once(self) -> None:
        await self.boot()
        await self.made("old", resolved_days=8)
        (trees.tree_path(self.project, "old") / ".env").write_text("TOKEN=x")
        await self.trees.sweep()
        await self.trees.sweep()
        self.assertTrue(trees.tree_path(self.project, "old").exists())
        (kept,) = self.kit.recorder.of("worktree.kept")
        self.assertEqual(kept.data, {"reason": "Kept: an ignored file (.env)"})
        (trees.tree_path(self.project, "old") / "notes.txt").write_text("more")
        await self.trees.sweep()
        self.assertEqual(self.kit.recorder.of("worktree.kept")[-1].data,
                         {"reason": "Kept: 1 untracked file, an ignored file (.env)"})

    async def test_a_thread_reopened_while_the_sweep_waits_keeps_its_tree(self) -> None:
        await self.boot()
        await self.made("old", resolved_days=8)
        path = trees.tree_path(self.project, "old")
        async with trees.lock_for(path):                    # an ensure or a removal holds the tree
            sweeping = asyncio.create_task(self.trees.sweep())
            await asyncio.sleep(0.2)
            await self.engine.write(lambda w: w.execute(
                "UPDATE threads SET state = 'working', resolved_at = NULL WHERE ts = 'old'"))   # a reply reopens it
        await sweeping
        self.assertTrue(path.exists())
        self.assertEqual(self.kit.recorder.of("worktree.removed"), [])

    async def test_a_process_left_in_the_registry_keeps_the_tree(self) -> None:
        await self.boot()
        sid = await self.made("old", resolved_days=8)
        await self.engine.write(lambda w: w.execute(
            "INSERT INTO procs (pid, start_ticks, session_id, epoch, argv0, started_at) VALUES (4242, 1, ?, 1, "
            "'cli', ?)", (sid, w.now)))
        self.assertEqual((await self.engine.get_session(sid))["state"], "cold")
        await self.trees.sweep()
        self.assertTrue(trees.tree_path(self.project, "old").exists())
        with self.assertRaises(ServiceError) as caught:
            await self.trees.remove(CHAT, "old")
        self.assertEqual(caught.exception.code, "session_busy")
        await self.engine.write(lambda w: w.execute("DELETE FROM procs WHERE pid = 4242"))     # not a real process

    async def test_a_tree_whose_marker_and_record_disagree_is_left_alone(self) -> None:
        await self.boot()
        await self.made("old", resolved_days=8)
        admin = Path(sh(trees.tree_path(self.project, "old"), "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(json.dumps({"thread": "someone-else", "chat": CHAT, "base_commit": "0"}))
        await self.trees.sweep()
        self.assertTrue(trees.tree_path(self.project, "old").exists())
        self.assertEqual(self.kit.recorder.of("worktree.removed"), [])

    async def test_xos_stale_registration_is_dropped_alone(self) -> None:
        await self.boot()
        await self.made("old", resolved_days=8)
        theirs = self.kit.root / "theirs"
        sh(self.project, "worktree", "add", "-q", "-b", "theirs", str(theirs))
        shutil.rmtree(trees.tree_path(self.project, "old"))
        shutil.rmtree(theirs)
        await self.trees.sweep()
        listed = sh(self.project, "worktree", "list", "--porcelain")
        self.assertNotIn("thread-old", listed)
        self.assertIn(str(theirs), listed)
        self.assertEqual(len(self.kit.recorder.of("worktree.removed")), 1)


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    return application


class RouteTests(Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def tree(self) -> tuple[str, Path]:
        sid = await self.thread()
        await self.run_turn(sid, 1)
        await self.release(sid)
        return sid, trees.tree_path(self.project, TS)

    async def test_the_person_removes_a_tree_after_seeing_exactly_what_would_be_lost(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        (path / "draft.txt").write_text("only copy")
        url = f"/api/chat/worktrees/{CHAT}/{TS}"

        refused = await self.client.delete(url)
        self.assertEqual(refused.status_code, 409)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "would_lose_work")
        self.assertEqual(detail["what"]["untracked"], {"count": 1, "paths": ["draft.txt"]})
        self.assertEqual(detail["what"]["summary"], "1 untracked file")
        self.assertIn(detail["confirm"], detail["message"])

        stale = await self.client.delete(url, params={"confirm": "0" * 12})
        self.assertEqual(stale.json()["detail"]["code"], "would_lose_work")
        (path / "more.txt").write_text("changed since")         # what changed: the old confirmation fails
        changed = await self.client.delete(url, params={"confirm": detail["confirm"]})
        self.assertEqual(changed.status_code, 409)
        self.assertNotEqual(changed.json()["detail"]["confirm"], detail["confirm"])
        self.assertTrue(path.exists())

        confirm = changed.json()["detail"]["confirm"]
        done = await self.client.delete(url, params={"confirm": confirm})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual((done.json()["removed"], done.json()["forced"]), (True, True))
        self.assertFalse(path.exists())
        (event,) = self.kit.recorder.of("worktree.removed")
        self.assertEqual(event.data, {"by": "person", "forced": True, "lost": changed.json()["detail"]["what"]})
        self.assertEqual((await self.client.delete(url)).json()["detail"]["code"], "no_worktree")

    async def test_a_tree_whose_only_work_sits_in_a_build_folder_can_be_removed_by_the_person(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        (path / "docs" / "build").mkdir(parents=True)
        (path / "docs" / "build" / "notes.md").write_text("only copy")
        url = f"/api/chat/worktrees/{CHAT}/{TS}"
        refused = (await self.client.delete(url)).json()["detail"]
        self.assertEqual((refused["code"], refused["what"]["untracked"]["paths"]),
                         ("would_lose_work", ["docs/build/notes.md"]))
        done = await self.client.delete(url, params={"confirm": refused["confirm"]})
        self.assertEqual((done.status_code, done.json()["forced"]), (200, True))
        self.assertFalse(path.exists())

    async def test_a_clean_tree_is_removed_at_once(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        done = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual(done.json(), {"removed": True, "forced": False, "lost": None, "note": None})
        self.assertFalse(path.exists())

    async def test_a_session_that_isnt_cold_is_busy(self) -> None:
        await self.boot()
        sid = await self.thread()
        await self.run_turn(sid, 1)                            # warm
        busy = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual((busy.status_code, busy.json()["detail"]["code"]), (409, "session_busy"))
        self.assertTrue(trees.tree_path(self.project, TS).exists())

    async def test_marker_and_record_disagreeing_leaves_it_alone(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        admin = Path(sh(path, "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(json.dumps({"thread": "other", "chat": CHAT, "base_commit": "0"}))
        refused = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "not_xo_worktree"))
        self.assertTrue(path.exists())

    async def test_a_recorded_path_that_isnt_an_xo_tree_is_never_removed(self) -> None:
        await self.boot()
        await self.thread()
        await self.engine.write(lambda w: w.execute("UPDATE threads SET worktree_path = ? WHERE ts = ?",
                                                    (str(self.project), TS)))
        refused = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "not_xo_worktree"))
        self.assertTrue((self.project / "README.md").exists())

    async def test_unknown_threads_and_threads_without_a_tree_are_404(self) -> None:
        await self.boot()
        await self.thread()                                    # never started: no tree yet
        missing = await self.client.delete(f"/api/chat/worktrees/{CHAT}/nope")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "thread_not_found"))
        none = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual((none.status_code, none.json()["detail"]["code"]), (404, "no_worktree"))

    async def test_the_listing_shows_xos_trees_and_the_reported_ones(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        (path / "draft.txt").write_text("x")
        made = self.project / ".claude" / "worktrees" / "agent-a0123456789abcdef"
        sh(self.project, "worktree", "add", "-q", "-b", "agent", str(made))
        await self.engine.write(lambda w: w.append("worktree.reported", {"path": str(made)}, chat_id=CHAT,
                                                   thread_ts=TS))
        listing = (await self.client.get("/api/chat/worktrees")).json()
        (tree,) = listing["worktrees"]
        self.assertEqual({k: tree[k] for k in ("chat_id", "thread_ts", "path", "exists", "xo", "session_state",
                                               "removable")},
                         {"chat_id": CHAT, "thread_ts": TS, "path": str(path), "exists": True, "xo": True,
                          "session_state": "cold", "removable": False})
        self.assertEqual(tree["would_lose"]["summary"], "1 untracked file")
        (reported,) = listing["reported"]
        self.assertEqual((reported["path"], reported["thread_ts"], reported["managed"]), (str(made), TS, False))
        self.assertEqual((await self.client.get("/api/chat/worktrees", params={"chat": "other"})).json(),
                         {"worktrees": [], "reported": []})

    async def test_routes_answer_503_without_an_engine(self) -> None:
        answer = await self.client.get("/api/chat/worktrees")
        self.assertEqual(answer.status_code, 503)


if __name__ == "__main__":
    unittest.main()
