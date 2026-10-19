"""A thread's worktree on disk (design ch 13 §1–§6), on real temp git
repositories: where it goes, creating and reusing it, the refusals, a rename
of the project folder, bringing it back after a removal, what removal would
lose, the removal itself, and the project's other worktrees."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.worktrees import git as wgit
from services.cowork_agent.storm.worktrees import trees
from tests.storm_worktrees_kit import branches, git_env, head, repo, sh, worktree_paths

CHAT = "chat_p0000000001"
TS = "1760518860.000100"


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = git_env()
        env.start()
        self.addCleanup(env.stop)
        self.project = repo(self.base / "demo")

    async def make(self, ts: str = TS, **kw) -> trees.Tree:
        return await trees.ensure(self.project, ts, chat_id=CHAT, **kw)

    async def found(self, tree: trees.Tree | Path) -> trees.Found:
        return await trees.find(self.project, getattr(tree, "path", tree))


class PlaceTests(Case):
    def test_a_git_project_places_the_thread_in_its_own_worktree(self) -> None:
        place = trees.place(self.project, TS)
        expected = str(self.project / ".xo" / "worktrees" / f"thread-{TS}")
        self.assertEqual(place, trees.Place(expected, expected, None))
        self.assertFalse(Path(expected).exists())                 # placing makes nothing

    def test_a_plain_folder_works_in_the_project_folder_with_a_note(self) -> None:
        plain = self.base / "notes"
        plain.mkdir()
        self.assertEqual(trees.place(plain, TS), trees.Place(str(plain), None, trees.NON_GIT_NOTE))
        self.assertFalse((plain / ".git").exists())               # XO never runs git init

    def test_a_ts_that_could_leave_the_folder_is_refused(self) -> None:
        for bad in ("../x", "a/b", "", ".hidden", "x" * 200):
            with self.subTest(ts=bad), self.assertRaises(trees.WorktreeRefused):
                trees.place(self.project, bad)

    def test_thread_of_reads_a_tree_path_back(self) -> None:
        path = trees.tree_path(self.project, TS)
        self.assertEqual(trees.thread_of(path), (self.project, TS))
        self.assertIsNone(trees.thread_of(self.project))
        self.assertIsNone(trees.thread_of(self.project / ".claude" / "worktrees" / "thread-1"))


class CreateTests(Case):
    async def test_a_new_tree_is_xos_in_every_way(self) -> None:
        start = head(self.project)
        tree = await self.make()
        self.assertEqual(tree.path, self.project / ".xo" / "worktrees" / f"thread-{TS}")
        self.assertRegex(tree.branch, r"^xo/thread-[0-9a-f]{6}$")
        self.assertEqual((tree.base_commit, tree.note), (start, None))
        self.assertEqual(sh(tree.path, "rev-parse", "--abbrev-ref", "HEAD").strip(), tree.branch)
        # Locked with XO's reason: no pid token, so no CLI sweep takes it for its own.
        (entry,) = [e for e in wgit.worktree_list(sh(self.project, "worktree", "list", "--porcelain", "-z"))[1:]]
        self.assertEqual(entry.locked, f"XO Space thread {TS}")
        self.assertNotIn("pid", entry.locked)
        # The marker in git's admin folder.
        admin = Path(sh(tree.path, "rev-parse", "--absolute-git-dir").strip())
        self.assertEqual(json.loads((admin / "XO_THREAD").read_text()),
                         {"thread": TS, "chat": CHAT, "base_commit": start})
        # Relative paths, both ways.
        self.assertFalse(os.path.isabs((tree.path / ".git").read_text().split(":", 1)[1].strip()))
        self.assertFalse(os.path.isabs((admin / "gitdir").read_text().strip()))
        # Kept out of git twice; the project's status doesn't show the checkout.
        self.assertEqual((self.project / ".xo" / ".gitignore").read_text(), "worktrees/\n")
        exclude = (self.project / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count("/.xo/worktrees/"), 1)
        status = sh(self.project, "status", "--porcelain", "--untracked-files=all")
        self.assertNotIn("worktrees", status)
        self.assertIn(".xo/.gitignore", status)                   # .xo/ itself still travels through git

    async def test_a_second_ensure_reuses_the_tree_and_writes_nothing_twice(self) -> None:
        first = await self.make()
        (first.path / "work.txt").write_text("in progress")
        again = await self.make(branch=first.branch, base_commit=first.base_commit)
        self.assertEqual(again, first)
        self.assertEqual((first.path / "work.txt").read_text(), "in progress")
        self.assertEqual(len([b for b in branches(self.project) if b.startswith("xo/thread-")]), 1)
        exclude = (self.project / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count("/.xo/worktrees/"), 1)

    async def test_a_lost_lock_is_put_back(self) -> None:
        tree = await self.make()
        sh(self.project, "worktree", "unlock", str(tree.path))
        await self.make()
        self.assertIn(f"locked XO Space thread {TS}", sh(self.project, "worktree", "list", "--porcelain"))

    async def test_a_branch_name_clash_gets_new_hex_and_the_old_branch_is_never_reset(self) -> None:
        sh(self.project, "branch", "xo/thread-aaaaaa")
        sh(self.project, "commit", "-q", "--allow-empty", "-m", "later")
        taken = sh(self.project, "rev-parse", "xo/thread-aaaaaa").strip()
        with mock.patch.object(trees.secrets, "token_hex", side_effect=["aaaaaa", "bbbbbb"]):
            tree = await self.make()
        self.assertEqual(tree.branch, "xo/thread-bbbbbb")
        self.assertEqual(sh(self.project, "rev-parse", "xo/thread-aaaaaa").strip(), taken)

    async def test_an_existing_gitignore_is_the_persons_and_is_left_alone(self) -> None:
        (self.project / ".xo").mkdir()
        (self.project / ".xo" / ".gitignore").write_text("scratch/\n")
        await self.make()
        self.assertEqual((self.project / ".xo" / ".gitignore").read_text(), "scratch/\n")
        self.assertNotIn("worktrees", sh(self.project, "status", "--porcelain", "--untracked-files=all"))

    async def test_an_old_git_gets_absolute_paths(self) -> None:
        with mock.patch.object(wgit, "_version", (2, 40)):
            tree = await self.make()
        self.assertTrue(os.path.isabs((tree.path / ".git").read_text().split(":", 1)[1].strip()))

    async def test_a_repository_without_a_commit_is_refused_plainly(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        sh(empty, "init", "-q", "-b", "main")
        with self.assertRaises(trees.WorktreeRefused) as caught:
            await trees.ensure(empty, TS, chat_id=CHAT)
        self.assertEqual(caught.exception.code, "no_commit")
        self.assertIn("Commit once", caught.exception.message)


class RefusalTests(Case):
    async def assert_refused(self, code: str) -> None:
        with self.assertRaises(trees.WorktreeRefused) as caught:
            await self.make()
        self.assertEqual(caught.exception.code, code)

    async def test_a_symlinked_xo_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".xo").symlink_to(elsewhere, target_is_directory=True)
        await self.assert_refused("unsafe_worktree_folder")
        self.assertEqual(list(elsewhere.iterdir()), [])

    async def test_a_symlinked_worktrees_folder_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".xo").mkdir()
        (self.project / ".xo" / "worktrees").symlink_to(elsewhere, target_is_directory=True)
        await self.assert_refused("unsafe_worktree_folder")
        self.assertEqual(list(elsewhere.iterdir()), [])

    async def test_a_folder_in_the_way_without_xos_marker_is_refused_and_kept(self) -> None:
        path = trees.tree_path(self.project, TS)
        path.mkdir(parents=True)
        (path / "mine.txt").write_text("the person's")
        await self.assert_refused("worktree_folder_taken")
        self.assertEqual((path / "mine.txt").read_text(), "the person's")

    async def test_an_empty_folder_in_the_way_is_used(self) -> None:
        trees.tree_path(self.project, TS).mkdir(parents=True)
        tree = await self.make()
        self.assertTrue((tree.path / ".git").is_file())

    async def test_a_worktree_someone_else_made_there_is_refused(self) -> None:
        path = trees.tree_path(self.project, TS)
        sh(self.project, "worktree", "add", "-q", "-b", "mine", str(path))
        await self.assert_refused("worktree_folder_taken")
        self.assertIn("mine", branches(self.project))

    async def test_another_threads_tree_is_refused(self) -> None:
        await self.make()
        with self.assertRaises(trees.WorktreeRefused) as caught:
            await trees.ensure(self.project, TS, chat_id="chat_other")
        self.assertEqual(caught.exception.code, "worktree_not_this_threads")

    async def test_a_create_interrupted_before_its_marker_is_cleared_and_made_again(self) -> None:
        path = trees.tree_path(self.project, TS)
        sh(self.project, "worktree", "add", "-q", "--lock", "--reason", f"XO Space thread {TS}", "-b",
           "xo/thread-cccccc", str(path))
        tree = await self.make()
        self.assertNotEqual(tree.branch, "xo/thread-cccccc")
        self.assertNotIn("xo/thread-cccccc", branches(self.project))
        self.assertTrue((await self.found(tree)).ours(CHAT, TS))

    async def test_an_interrupted_create_that_holds_work_is_refused(self) -> None:
        path = trees.tree_path(self.project, TS)
        sh(self.project, "worktree", "add", "-q", "--lock", "--reason", f"XO Space thread {TS}", "-b",
           "xo/thread-cccccc", str(path))
        (path / "draft.txt").write_text("work")
        await self.assert_refused("worktree_folder_taken")
        self.assertTrue((path / "draft.txt").exists())


class RenameTests(Case):
    async def test_the_tree_survives_a_rename_of_the_project_folder(self) -> None:
        tree = await self.make()
        (tree.path / "work.txt").write_text("x")
        moved = self.base / "renamed"
        self.project.rename(moved)
        self.project = moved
        moved_tree = trees.tree_path(moved, TS)
        self.assertIn("?? work.txt", sh(moved_tree, "status", "--porcelain"))
        self.assertIn(str(moved_tree), worktree_paths(moved))
        again = await self.make()                       # found and reused at its new place
        self.assertEqual((again.path, again.branch), (moved_tree, tree.branch))


class ReopenTests(Case):
    async def test_back_at_the_same_path_from_its_branch(self) -> None:
        tree = await self.make()
        (tree.path / "kept.txt").write_text("committed")
        sh(tree.path, "add", "kept.txt")
        sh(tree.path, "commit", "-q", "-m", "work")
        sh(self.project, "worktree", "unlock", str(tree.path))
        sh(self.project, "worktree", "remove", str(tree.path))
        back = await self.make(branch=tree.branch, base_commit=tree.base_commit)
        self.assertEqual((back.path, back.branch, back.base_commit), (tree.path, tree.branch, tree.base_commit))
        self.assertEqual((back.path / "kept.txt").read_text(), "committed")
        self.assertIn(tree.branch, back.note)
        self.assertTrue((await self.found(back)).ours(CHAT, TS))

    async def test_back_from_head_on_a_new_branch_when_its_branch_is_gone(self) -> None:
        tree = await self.make()
        sh(self.project, "worktree", "unlock", str(tree.path))
        sh(self.project, "worktree", "remove", str(tree.path))
        sh(self.project, "branch", "-D", tree.branch)
        sh(self.project, "commit", "-q", "--allow-empty", "-m", "moved on")
        back = await self.make(branch=tree.branch, base_commit=tree.base_commit)
        self.assertEqual(back.path, tree.path)
        self.assertNotEqual(back.branch, tree.branch)
        self.assertEqual(back.base_commit, head(self.project))
        self.assertIn(back.branch, back.note)
        self.assertIn("HEAD", back.note)

    async def test_a_registration_left_by_a_vanished_folder_is_dropped_and_the_tree_made_again(self) -> None:
        tree = await self.make()
        shutil.rmtree(tree.path)
        back = await self.make(branch=tree.branch, base_commit=tree.base_commit)
        self.assertEqual((back.path, back.branch), (tree.path, tree.branch))
        self.assertTrue((back.path / ".git").exists())


class LossTests(Case):
    async def loss(self, tree: trees.Tree) -> trees.Loss:
        return await trees.loss(await self.found(tree), tree.branch)

    async def test_a_clean_tree_loses_nothing_and_rebuildable_folders_dont_count(self) -> None:
        tree = await self.make()
        (tree.path / "node_modules" / "x").mkdir(parents=True)
        (tree.path / "node_modules" / "x" / "index.js").write_text("")
        self.assertTrue((await self.loss(tree)).empty)

    async def test_each_kind_of_work_keeps_the_tree(self) -> None:
        cases = {
            "an uncommitted file": lambda p: (p / "README.md").write_text("changed"),
            "an untracked file": lambda p: (p / "new.txt").write_text("new"),
            "an ignored .env": lambda p: (p / ".env").write_text("SECRET=1"),
        }
        for n, (name, change) in enumerate(cases.items()):
            with self.subTest(name):
                tree = await self.make(ts=f"t{n}")
                change(tree.path)
                lost = await self.loss(tree)
                self.assertFalse(lost.empty)
        tree = await self.make(ts="t9")
        (tree.path / "README.md").write_text("changed")
        (tree.path / "new.txt").write_text("new")
        (tree.path / ".env").write_text("SECRET=1")
        lost = await self.loss(tree)
        self.assertEqual((lost.changed, lost.untracked, lost.ignored), (("README.md",), ("new.txt",), (".env",)))
        self.assertEqual(lost.summary(), "1 uncommitted file, 1 untracked file, an ignored file (.env)")

    async def test_a_commit_only_on_the_branch_keeps_the_tree(self) -> None:
        tree = await self.make()
        (tree.path / "a.txt").write_text("a")
        sh(tree.path, "add", "a.txt")
        sh(tree.path, "commit", "-q", "-m", "a")
        lost = await self.loss(tree)
        self.assertEqual(lost.commits, 1)
        self.assertEqual(lost.summary(), f"1 commit only on {tree.branch}")
        sh(self.project, "branch", "copy", tree.branch)      # on another branch too: nothing would be lost
        self.assertTrue((await self.loss(tree)).empty)

    async def test_the_confirmation_changes_with_what_would_be_lost(self) -> None:
        tree = await self.make()
        (tree.path / "a.txt").write_text("a")
        first = trees.confirmation((await self.loss(tree)).what())
        self.assertEqual(trees.confirmation((await self.loss(tree)).what()), first)
        (tree.path / "b.txt").write_text("b")
        self.assertNotEqual(trees.confirmation((await self.loss(tree)).what()), first)


class RemoveTests(Case):
    async def test_a_clean_tree_is_removed_with_its_branch(self) -> None:
        tree = await self.make()
        (tree.path / "node_modules").mkdir()
        (tree.path / "node_modules" / "i.js").write_text("")
        self.assertIsNone(await trees.remove(self.project, await self.found(tree), tree.branch))
        self.assertFalse(tree.path.exists())
        self.assertNotIn(tree.branch, branches(self.project))
        self.assertEqual(worktree_paths(self.project), [str(self.project)])

    async def test_git_refusing_keeps_the_tree_locked(self) -> None:
        tree = await self.make()
        (tree.path / "new.txt").write_text("new")
        with self.assertRaises(trees.Kept):
            await trees.remove(self.project, await self.found(tree), tree.branch)
        self.assertTrue((tree.path / "new.txt").exists())
        self.assertIn(f"locked XO Space thread {TS}", sh(self.project, "worktree", "list", "--porcelain"))

    async def test_a_forced_removal_takes_the_work_and_the_branch(self) -> None:
        tree = await self.make()
        (tree.path / ".env").write_text("x")
        (tree.path / "a.txt").write_text("a")
        sh(tree.path, "add", "a.txt")
        sh(tree.path, "commit", "-q", "-m", "a")
        await trees.remove(self.project, await self.found(tree), tree.branch, force=True)
        self.assertFalse(tree.path.exists())
        self.assertNotIn(tree.branch, branches(self.project))

    async def test_only_xos_own_stale_registration_is_dropped(self) -> None:
        tree = await self.make()
        theirs = self.base / "theirs"
        sh(self.project, "worktree", "add", "-q", "-b", "theirs", str(theirs))
        shutil.rmtree(tree.path)
        shutil.rmtree(theirs)
        found = await self.found(tree)
        self.assertTrue(found.registered and not found.exists)
        await trees.remove(self.project, found, tree.branch)
        listed = sh(self.project, "worktree", "list", "--porcelain")
        self.assertNotIn(str(tree.path), listed)
        self.assertIn(str(theirs), listed)                 # the person's own entry, still there
        self.assertIn("prunable", listed)
        self.assertNotIn(tree.branch, branches(self.project))

    async def test_a_branch_that_moved_on_is_kept(self) -> None:
        tree = await self.make()
        (tree.path / "a.txt").write_text("a")
        sh(tree.path, "add", "a.txt")
        sh(tree.path, "commit", "-q", "-m", "a")
        sh(self.project, "branch", "elsewhere", tree.branch)       # its commit lives on elsewhere
        note = await trees.remove(self.project, await self.found(tree), tree.branch)
        self.assertIn(f"its branch {tree.branch} was kept", note)
        self.assertIn(tree.branch, branches(self.project))


class LockTests(Case):
    async def test_a_removal_waits_for_an_ensure_of_the_same_tree(self) -> None:
        tree = await self.make()
        order: list[str] = []
        real = trees.snapshot
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_snapshot(project):
            if not entered.is_set():                       # the ensure's own read, held
                order.append("ensure reads")
                entered.set()
                await release.wait()
            return await real(project)

        async def removal() -> None:
            async with trees.lock_for(tree.path):
                order.append("removal")
                await trees.remove(self.project, await self.found(tree), tree.branch)

        with mock.patch.object(trees, "snapshot", slow_snapshot):
            ensuring = asyncio.create_task(self.make(branch=tree.branch, base_commit=tree.base_commit))
            await entered.wait()
            removing = asyncio.create_task(removal())
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["ensure reads"])      # the removal is waiting
            release.set()
            again = await ensuring
        await removing
        self.assertEqual(order, ["ensure reads", "removal"])
        self.assertEqual(again.path, tree.path)
        self.assertFalse(tree.path.exists())               # removed after the ensure, never under it


class OtherWorktreeTests(Case):
    async def test_cli_worktrees_are_every_linked_tree_but_xos(self) -> None:
        await self.make()
        cli = self.project / ".claude" / "worktrees" / "agent-a0123456789abcdef"
        sh(self.project, "worktree", "add", "-q", "-b", "worktree-agent", str(cli))
        self.assertEqual(await trees.cli_worktrees(self.project), [str(cli)])

    async def test_the_survey_offers_clean_xo_trees_and_lists_what_blocks(self) -> None:
        clean = await self.make(ts="clean")
        dirty = await self.make(ts="dirty")
        (dirty.path / "draft.txt").write_text("x")
        theirs = self.project / ".claude" / "worktrees" / "mine"
        sh(self.project, "worktree", "add", "-q", "-b", "mine", str(theirs))
        survey = await trees.survey(self.project)
        self.assertEqual(survey["removable"], [{"path": ".xo/worktrees/thread-clean", "branch": clean.branch,
                                                "xo": True, "chat_id": CHAT, "thread_ts": "clean"}])
        self.assertEqual(sorted((b["path"], b["xo"], b["reason"]) for b in survey["blocking"]), [
            (".claude/worktrees/mine", False, "A worktree XO didn't make; remove it with `git worktree remove`."),
            (".xo/worktrees/thread-dirty", True, "Kept: 1 untracked file")])


if __name__ == "__main__":
    unittest.main()
