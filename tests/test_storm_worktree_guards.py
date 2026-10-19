"""The guards today's code gains for thread worktrees (design ch 13 §7): a
backup never carries a checkout (W3), ``ensure_xo_structure`` and the
doctor never look inside ``.xo/worktrees/``, and project removal offers XO's
clean worktrees first, then lists what blocks it. On real temp git
repositories (git is not an agent CLI)."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import project_management, xo_structure
from services.cowork_agent import coder_identity
from services.cowork_agent.storm.worktrees import trees
from services.cowork_agent.xo_projects_sync.tarball import build_tarball
from tests.doctor_sandbox import DoctorSandbox
from tests.storm_worktrees_kit import git_env, repo, sh

CHAT = "chat_p0000000001"


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "projects"
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state"),
                                           "QUIRQ_COMMAND_LOG": "off", "XO_SCHEDULER_ENABLED": "false"})
        env.start()
        self.addCleanup(env.stop)
        git = git_env()
        git.start()
        self.addCleanup(git.stop)
        owner = mock.patch.object(coder_identity, "resolve_user_id", return_value="local")
        owner.start()
        self.addCleanup(owner.stop)
        self.project = repo(self.root / "demo")
        xo_structure.ensure_xo_structure("demo")

    async def tree(self, ts: str) -> trees.Tree:
        return await trees.ensure(self.project, ts, chat_id=CHAT)


class BackupTests(Case):
    async def members(self) -> list[str]:
        out = self.base / "snapshot.tar.gz"
        await build_tarball(self.project, out)
        with tarfile.open(out) as tar:
            return sorted(tar.getnames())

    async def test_w3_a_backup_of_a_project_with_a_live_worktree_carries_no_checkout(self) -> None:
        tree = await self.tree("live")
        (tree.path / "README.md").write_text("edited in the thread")
        (tree.path / "draft.txt").write_text("untracked")
        (tree.path / "config.local").write_text("secret-ish")
        (tree.path / "node_modules").mkdir()
        (tree.path / "node_modules" / "i.js").write_text("")
        members = await self.members()
        self.assertEqual([m for m in members if "worktrees" in m], [])
        self.assertIn(".xo/project.json", members)            # .xo/ itself always travels
        self.assertIn(".xo/.gitignore", members)
        self.assertIn("README.md", members)

    async def test_a_checkout_anywhere_else_under_xo_is_skipped_too(self) -> None:
        stray = repo(self.project / ".xo" / "copy")          # a checkout misplaced inside .xo/
        (stray / "code.py").write_text("x")
        (self.project / ".xo" / "notes").mkdir()
        (self.project / ".xo" / "notes" / "keep.md").write_text("x")
        members = await self.members()
        self.assertNotIn(".xo/copy/code.py", members)
        self.assertIn(".xo/notes/keep.md", members)


class StructureTests(Case):
    async def test_xos_own_entries_are_left_as_they_are_and_never_looked_into(self) -> None:
        tree = await self.tree("t1")
        (tree.path / "work.txt").write_text("x")
        gitignore = (self.project / ".xo" / ".gitignore").read_bytes()
        sealed = self.project / ".xo" / "worktrees" / "sealed"
        sealed.mkdir()
        sealed.chmod(0o000)
        self.addCleanup(sealed.chmod, 0o755)
        report = xo_structure.ensure_xo_structure("demo")
        self.assertTrue(report.ok, report)
        self.assertEqual(report.created, ())
        self.assertEqual((self.project / ".xo" / ".gitignore").read_bytes(), gitignore)
        self.assertEqual((tree.path / "work.txt").read_text(), "x")

    async def test_xo_still_shows_in_git_but_its_worktrees_dont(self) -> None:
        await self.tree("t1")
        status = sh(self.project, "status", "--porcelain", "--untracked-files=all")
        for name in xo_structure.CANONICAL_FILES:
            self.assertIn(f".xo/{name}", status)
        self.assertNotIn("worktrees/", status)
        # The .xo/.gitignore alone keeps them out, for a project that commits .xo/.
        exclude = self.project / ".git" / "info" / "exclude"
        exclude.write_text("")
        self.assertNotIn("worktrees/", sh(self.project, "status", "--porcelain", "--untracked-files=all"))


class DoctorTests(DoctorSandbox):
    def test_the_xo_temp_scan_skips_thread_checkouts(self) -> None:
        xo = self.projects / "sample-project" / ".xo"
        checkout = xo / "worktrees" / "thread-1"
        checkout.mkdir(parents=True)
        old = self.now - 86400
        for path in (checkout / "build.tmp", xo / "todos.json.tmp"):
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (old, old))
        found = sorted(f["subject"] for f in self.problems() if f["id"] == "tmp.stale")
        self.assertEqual(found, ["sample-project/.xo/todos.json.tmp"])


class ProjectRemovalTests(Case):
    async def test_clean_xo_trees_are_offered_first_then_what_blocks_is_listed(self) -> None:
        clean = await self.tree("clean")
        dirty = await self.tree("dirty")
        (dirty.path / "draft.txt").write_text("x")
        theirs = self.project / ".claude" / "worktrees" / "agent"
        sh(self.project, "worktree", "add", "-q", "-b", "agent", str(theirs))
        status = await project_management.removal_status("demo")
        self.assertFalse(status["can_remove"])
        self.assertEqual(status["blockers"][0]["code"], "linked_worktree")     # today's blocker, kept
        self.assertEqual(status["worktrees"]["removable"],
                         [{"path": ".xo/worktrees/thread-clean", "branch": clean.branch, "xo": True,
                           "chat_id": CHAT, "thread_ts": "clean"}])
        self.assertEqual(sorted((b["path"], b["xo"]) for b in status["worktrees"]["blocking"]),
                         [(".claude/worktrees/agent", False), (".xo/worktrees/thread-dirty", True)])

    async def test_once_the_trees_are_gone_the_project_can_be_removed(self) -> None:
        tree = await self.tree("clean")
        await trees.remove(self.project, await trees.find(self.project, tree.path), tree.branch)
        status = await project_management.removal_status("demo")
        self.assertTrue(status["can_remove"], status)
        self.assertEqual(status["worktrees"], {"removable": [], "blocking": []})
        await project_management.remove_project("demo", "demo")
        self.assertFalse(self.project.exists())

    async def test_a_plain_project_keeps_its_shape(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        status = await project_management.removal_status("plain")
        self.assertEqual(status["worktrees"], {"removable": [], "blocking": []})
        self.assertTrue(status["can_remove"])


if __name__ == "__main__":
    unittest.main()
