"""A thread branch's git state (``worktrees/state.py``) on real temp
repositories, its pull request through a fake ``gh``, and the tracker in the
engine's life: the listing shows it, a removed thread keeps it, and each
change is one ``thread.git`` event."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.worktrees import state, tracking, trees
from tests.storm_worktrees_kit import git_env, head, repo, sh
import httpx

from tests.test_storm_worktrees_engine import CHAT, TS, Case, app
from utils.commands import CommandResult

BRANCH = "xo/thread-abc123"


def commit(cwd: Path, name: str, text: str = "x\n", message: str | None = None) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    sh(cwd, "add", name)
    sh(cwd, "commit", "-q", "-m", message or f"add {name}")
    return head(cwd)


def gh_result(items: list[dict] | None, *, ok: bool = True) -> CommandResult:
    return CommandResult(argv=["gh"], returncode=0 if ok else 1, output=json.dumps(items or []),
                         duration_seconds=0.1, stderr="" if ok else "gh: not logged in")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = git_env()
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = repo(Path(tmp.name) / "demo")
        self.base = head(self.project)
        self.tree = Path(tmp.name) / "tree"
        sh(self.project, "worktree", "add", "-q", "-b", BRANCH, str(self.tree))

    async def read(self, previous=None) -> dict:
        return await state.read(self.project, BRANCH, self.base, previous=previous)

    async def test_a_branch_without_commits_of_its_own_is_not_merged_and_has_no_last_commit(self) -> None:
        got = await self.read()
        self.assertEqual({k: got[k] for k in ("branch_exists", "base", "ahead", "behind", "commits", "last_commit",
                                              "merged", "unpushed")},
                         {"branch_exists": True, "base": "main", "ahead": 0, "behind": 0, "commits": 0,
                          "last_commit": None, "merged": False, "unpushed": None})
        self.assertEqual(got["upstream"], {"ref": None, "pushed": False, "ahead": None, "behind": None,
                                           "gone": False})

    async def test_commits_ahead_and_behind_and_the_last_one(self) -> None:
        commit(self.tree, "a.txt")
        sha = commit(self.tree, "b.txt", message="Add the b file")
        commit(self.project, "main.txt")
        got = await self.read()
        self.assertEqual((got["ahead"], got["behind"], got["commits"], got["merged"]), (2, 1, 2, False))
        self.assertEqual((got["last_commit"]["sha"], got["last_commit"]["subject"]), (sha, "Add the b file"))
        self.assertRegex(got["last_commit"]["at"], r"^\d{4}-\d\d-\d\dT")

    async def test_a_merge_into_main_is_merged_at_the_merge_commits_time(self) -> None:
        commit(self.tree, "a.txt")
        commit(self.project, "main.txt")
        sh(self.project, "merge", "-q", "--no-ff", "-m", "Merge the thread", BRANCH)
        merge_at = sh(self.project, "show", "-s", "--format=%cI", "HEAD").strip()
        got = await self.read()
        self.assertEqual((got["merged"], got["merged_by"], got["merged_at"], got["ahead"]),
                         (True, "ancestor", merge_at, 0))

    async def test_a_fast_forward_is_merged_at_the_tips_own_time(self) -> None:
        tip = commit(self.tree, "a.txt")
        sh(self.project, "merge", "-q", "--ff-only", BRANCH)
        commit(self.project, "later.txt")
        tip_at = sh(self.project, "show", "-s", "--format=%cI", tip).strip()
        got = await self.read()
        self.assertEqual((got["merged"], got["merged_at"]), (True, tip_at))

    async def test_cherry_picked_work_is_merged_as_patches(self) -> None:
        tip = commit(self.tree, "a.txt")
        commit(self.project, "main.txt")
        sh(self.project, "cherry-pick", tip)
        got = await self.read()
        self.assertEqual((got["merged"], got["merged_by"], got["merged_at"]), (True, "patches", None))

    async def test_pushed_upstream_and_unpushed_commits(self) -> None:
        remote = self.project.parent / "remote.git"
        sh(self.project.parent, "init", "-q", "--bare", str(remote))
        sh(self.project, "remote", "add", "origin", str(remote))
        sh(self.project, "push", "-q", "origin", "main")
        sh(self.project, "remote", "set-head", "origin", "main")
        commit(self.tree, "a.txt")
        sh(self.tree, "push", "-q", "-u", "origin", BRANCH)
        commit(self.tree, "b.txt")
        got = await self.read()
        self.assertEqual(got["upstream"], {"ref": f"origin/{BRANCH}", "pushed": True, "ahead": 1, "behind": 0,
                                           "gone": False})
        self.assertEqual((got["unpushed"], got["base"]), (1, "main"))

    async def test_a_gone_branch_keeps_what_was_read_of_it(self) -> None:
        commit(self.tree, "a.txt")
        sh(self.project, "merge", "-q", "--no-ff", "-m", "Merge", BRANCH)
        before = await self.read()
        sh(self.project, "worktree", "remove", str(self.tree))
        sh(self.project, "branch", "-d", BRANCH)
        got = await self.read(previous=before)
        self.assertEqual({**got, "branch_exists": True}, before)
        self.assertFalse(got["branch_exists"])

    async def test_a_merged_pull_request_marks_a_squash_merge_merged(self) -> None:
        commit(self.tree, "a.txt")
        got = state.merge_pr(await self.read(), {"number": 37, "url": "https://github.com/o/r/pull/37",
                                                 "state": "MERGED", "draft": False,
                                                 "merged_at": "2026-11-02T10:00:00Z"})
        self.assertEqual((got["merged"], got["merged_by"], got["merged_at"], got["pr"]["number"]),
                         (True, "pr", "2026-11-02T10:00:00Z", 37))
        again = await self.read(previous=got)          # a later read keeps what the forge said
        self.assertEqual((again["merged"], again["merged_by"], again["pr"]["number"]), (True, "pr", 37))


class PullRequestTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, result: CommandResult) -> tuple:
        with mock.patch.object(state, "gh_available", return_value=True), \
                mock.patch.object(state, "run", mock.AsyncMock(return_value=result)) as ran:
            got = await state.pr_lookup(Path("/tmp"), BRANCH)
        self.assertEqual(ran.await_args.args[0][:4], ["gh", "pr", "list", "--head"])
        return got

    async def test_the_open_one_is_shown_before_an_older_closed_one(self) -> None:
        answered, pr = await self.lookup(gh_result([
            {"number": 3, "url": "u3", "state": "CLOSED", "isDraft": False, "mergedAt": None,
             "updatedAt": "2026-11-03T00:00:00Z", "headRefName": BRANCH},
            {"number": 5, "url": "u5", "state": "OPEN", "isDraft": True, "mergedAt": None,
             "updatedAt": "2026-11-01T00:00:00Z", "headRefName": BRANCH},
            {"number": 9, "url": "u9", "state": "OPEN", "isDraft": False, "mergedAt": None,
             "updatedAt": "2026-11-04T00:00:00Z", "headRefName": "someone/else"}]))
        self.assertEqual((answered, pr), (True, {"number": 5, "url": "u5", "state": "OPEN", "draft": True,
                                                 "merged_at": None}))

    async def test_none_found_and_gh_that_cant_tell_differ(self) -> None:
        self.assertEqual(await self.lookup(gh_result([])), (True, None))
        self.assertEqual(await self.lookup(gh_result(None, ok=False)), (False, None))

    async def test_without_gh_nothing_is_asked(self) -> None:
        with mock.patch.object(state, "gh_available", return_value=False):
            self.assertEqual(await state.pr_lookup(Path("/tmp"), BRANCH), (False, None))

    def test_when_the_forge_is_asked_again(self) -> None:
        now = 1_800_000_000.0
        pushed = {"upstream": {"pushed": True}, "pr": None}
        self.assertFalse(tracking.pr_due({"upstream": {"pushed": False}, "pr": None}, None, now))
        self.assertTrue(tracking.pr_due(pushed, None, now))
        self.assertFalse(tracking.pr_due(pushed, iso(now - 60), now))
        self.assertTrue(tracking.pr_due(pushed, iso(now - tracking.PR_OPEN_S), now))
        closed = {**pushed, "pr": {"state": "CLOSED"}}
        self.assertFalse(tracking.pr_due(closed, iso(now - tracking.PR_OPEN_S), now))
        self.assertTrue(tracking.pr_due(closed, iso(now - tracking.PR_CLOSED_S), now))
        self.assertFalse(tracking.pr_due({**pushed, "pr": {"state": "MERGED"}}, None, now))


class TrackedRouteTests(Case):
    """The listing's ``git`` and ``removed``, on the engine kit."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        gh = mock.patch.object(state, "gh_available", return_value=False)
        gh.start()
        self.addCleanup(gh.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def tree(self) -> tuple[str, Path]:
        sid = await self.thread()
        await self.run_turn(sid, 1)
        await self.release(sid)
        return sid, trees.tree_path(self.project, TS)

    async def test_a_turns_end_reads_the_branch_and_the_listing_shows_it(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        await self.kit.until(lambda: self.kit.recorder.of("thread.git"))
        (event,) = self.kit.recorder.of("thread.git")
        self.assertEqual((event.chat_id, event.thread_ts, event.data["ts"]), (CHAT, TS, TS))
        branch = event.data["git"]["branch"]
        listing = (await self.client.get("/api/chat/worktrees")).json()
        (tree,) = listing["worktrees"]
        self.assertEqual((tree["git"]["branch"], tree["git"]["commits"]), (branch, 0))
        self.assertEqual(listing["removed"], [])
        commit(path, "work.txt", message="Write the work")
        # Fresh (read under a minute ago): shown as stored, not read again.
        tree = (await self.client.get("/api/chat/worktrees")).json()["worktrees"][0]
        self.assertEqual(tree["git"]["commits"], 0)
        self.kit.clock.advance(tracking.FRESH_S)
        tree = (await self.client.get("/api/chat/worktrees")).json()["worktrees"][0]
        self.assertEqual((tree["git"]["commits"], tree["git"]["ahead"], tree["git"]["last_commit"]["subject"]),
                         (1, 1, "Write the work"))
        self.assertEqual(len(self.kit.recorder.of("thread.git")), 2)     # unchanged reads add no event

    async def test_a_removed_thread_still_says_its_work_was_merged(self) -> None:
        await self.boot()
        _sid, path = await self.tree()
        commit(path, "work.txt")
        sh(self.project, "merge", "-q", "--no-ff", "-m", "Merge the thread", trees.xo_branch(
            self.row()["branch"]))
        done = await self.client.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
        self.assertEqual(done.status_code, 200, done.text)
        listing = (await self.client.get("/api/chat/worktrees")).json()
        self.assertEqual(listing["worktrees"], [])
        (gone,) = listing["removed"]
        self.assertEqual((gone["thread_ts"], gone["removed_by"]), (TS, "person"))
        self.assertIsNotNone(gone["removed_at"])
        self.assertEqual((gone["git"]["merged"], gone["git"]["merged_by"], gone["git"]["branch_exists"],
                          gone["git"]["commits"]), (True, "ancestor", False, 1))


if __name__ == "__main__":
    unittest.main()
