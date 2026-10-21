"""The integrity check on disk (design ch 16 §2.1–§2.2; S16-2, the watched-path
corpus): real git in temp repositories, no engine and no model.

For every watched group (each CLI's project config, ``.xo/``, git's own), each
kind of change must be found: an edit, a new file, a delete, a rename, a
symlink at the file and at a parent folder, a mode change, a commit on the
thread's branch, an ignored file, and git's ``core.hooksPath`` and
``core.fsmonitor``. An unwatched change isn't reported; links are never
followed; the index can't hide an edit; XO never runs a filter the agent
configured; a non-git folder and a plain session use the snapshot; a check
that can't run says why.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.review import integrity as i
from services.cowork_agent.storm.review.integrity import CheckFailed, Watch
from tests.storm_worktrees_kit import git_env, sh
from utils.commands import CommandResult

#: Every CLI's declared project config (what the engine's union holds once
#: all three runtimes exist), so each group is proven.
ALL_CLIS = (".agents/**", ".claude/**", ".codex/**", ".mcp.json")
WATCH = Watch(ALL_CLIS)
#: One file per group that the base commit has, and a new file's place in it.
GROUPS = {
    ".claude/settings.json": ".claude/agents/new.md",
    ".mcp.json": None,
    ".codex/config.toml": ".codex/rules/new.rules",
    ".agents/hooks.json": ".agents/new.json",
    ".xo/notes.md": ".xo/new.md",
}
BASE_FILES = {**{path: f"base {path}\n" for path in GROUPS}, ".claude/hooks/pre.sh": "echo hi\n",
              "README.md": "a project\n", "src/app.py": "print(1)\n", ".xo/todos.json": "{}\n",
              ".gitignore": ".claude/local.json\n"}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = git_env()
        env.start()
        self.addCleanup(env.stop)
        self.project = self.root / "demo"
        self.project.mkdir()
        sh(self.project, "init", "-q", "-b", "main")
        for path, text in BASE_FILES.items():
            write(self.project / path, text)
        sh(self.project, "add", "-A")
        sh(self.project, "commit", "-q", "-m", "base")
        self.base = sh(self.project, "rev-parse", "HEAD").strip()
        self.trees = 0

    def tree(self) -> Path:
        """A fresh thread worktree from the base commit, as storm makes one."""
        self.trees += 1
        path = self.project / ".xo" / "worktrees" / f"thread-{self.trees}"
        sh(self.project, "worktree", "add", "-q", "-b", f"xo/thread-{self.trees:06x}", str(path), self.base)
        return path

    async def baseline(self, folder: Path, *, worktree: bool = True) -> dict:
        first = await i.inspect(folder, WATCH, base_commit=self.base if worktree else None, baseline=None)
        self.assertEqual(first.changes, ())
        return first.snapshot

    async def changes(self, folder: Path, baseline: dict, *, worktree: bool = True) -> dict[str, str]:
        found = await i.inspect(folder, WATCH, base_commit=self.base if worktree else None, baseline=baseline)
        return {ch.path: ch.change for ch in found.changes}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class WorktreeCorpus(Case):
    """A thread's worktree against its base commit: every group, every change."""

    async def test_an_edit_in_each_group_is_found(self) -> None:
        for path in GROUPS:
            with self.subTest(path=path):
                tree = self.tree()
                baseline = await self.baseline(tree)
                write(tree / path, "changed\n")
                self.assertEqual(await self.changes(tree, baseline), {path: "modified"})

    async def test_a_new_file_in_each_group_is_found(self) -> None:
        for new in (n for n in GROUPS.values() if n):
            with self.subTest(path=new):
                tree = self.tree()
                baseline = await self.baseline(tree)
                write(tree / new, "new\n")
                self.assertEqual(await self.changes(tree, baseline), {new: "added"})

    async def test_a_delete_in_each_group_is_found(self) -> None:
        for path in GROUPS:
            with self.subTest(path=path):
                tree = self.tree()
                baseline = await self.baseline(tree)
                (tree / path).unlink()
                self.assertEqual(await self.changes(tree, baseline), {path: "deleted"})

    async def test_a_rename_is_found_at_both_names(self) -> None:
        for path in GROUPS:
            with self.subTest(path=path):
                tree = self.tree()
                baseline = await self.baseline(tree)
                renamed = f"{path}.moved"
                (tree / path).rename(tree / renamed)
                expected = {path: "deleted", renamed: "added"} if "/" in path else {path: "deleted"}
                self.assertEqual(await self.changes(tree, baseline), expected)      # .mcp.json.moved isn't watched

    async def test_a_symlink_at_the_file_is_found_and_never_followed(self) -> None:
        outside = self.root / "outside.json"
        for path in GROUPS:
            with self.subTest(path=path):
                tree = self.tree()
                baseline = await self.baseline(tree)
                write(outside, (tree / path).read_text())        # the same content, behind a link
                (tree / path).unlink()
                os.symlink(outside, tree / path)
                self.assertEqual(await self.changes(tree, baseline), {path: "modified"})
                files, _own = i.scan(tree, WATCH)
                self.assertEqual((files[path].kind, files[path].mode), ("link", i.LINK_MODE))

    async def test_a_symlink_at_a_parent_folder_is_found_and_nothing_behind_it_is_read(self) -> None:
        for folder in (".claude", ".codex", ".agents", ".xo"):
            with self.subTest(folder=folder):
                tree = self.tree()
                baseline = await self.baseline(tree)
                copy = self.root / f"copy-{self.trees}"
                shutil.copytree(tree / folder, copy, symlinks=True)      # identical files behind the link
                shutil.rmtree(tree / folder)
                os.symlink(copy, tree / folder)
                found = await self.changes(tree, baseline)
                self.assertEqual(found.pop(folder), "added")             # the link itself
                self.assertTrue(found and set(found.values()) == {"deleted"}, found)
                self.assertTrue(all(p.startswith(folder + "/") for p in found))

    async def test_a_symlink_at_a_deeper_parent_folder_is_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        shutil.rmtree(tree / ".claude" / "hooks")
        os.symlink("/etc", tree / ".claude" / "hooks")
        self.assertEqual(await self.changes(tree, baseline), {".claude/hooks": "added",
                                                              ".claude/hooks/pre.sh": "deleted"})

    async def test_a_mode_change_is_found(self) -> None:
        for path in GROUPS:
            with self.subTest(path=path):
                tree = self.tree()
                baseline = await self.baseline(tree)
                (tree / path).chmod(0o755)
                self.assertEqual(await self.changes(tree, baseline), {path: "modified"})

    async def test_a_commit_on_the_thread_branch_is_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        write(tree / ".claude" / "settings.json", '{"hooks": {"Stop": "curl evil"}}\n')
        sh(tree, "commit", "-q", "-am", "the agent commits its own change")
        self.assertEqual(sh(tree, "status", "--porcelain"), "")              # git sees nothing to report
        self.assertEqual(await self.changes(tree, baseline), {".claude/settings.json": "modified"})

    async def test_an_ignored_file_is_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        write(tree / ".claude" / "local.json", "{}\n")
        self.assertIn(".claude/local.json", sh(tree, "status", "--porcelain", "--ignored"))
        self.assertEqual(await self.changes(tree, baseline), {".claude/local.json": "added"})

    async def test_the_index_cant_hide_an_edit(self) -> None:
        """``assume-unchanged`` and ``skip-worktree`` hide an edit from ``git
        diff`` and ``git status``; the check compares objects, not the index."""
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                tree = self.tree()
                baseline = await self.baseline(tree)
                sh(tree, "update-index", flag, ".claude/settings.json")
                write(tree / ".claude" / "settings.json", "hidden\n")
                self.assertEqual(sh(tree, "diff", "--name-only", self.base), "")
                self.assertEqual(await self.changes(tree, baseline), {".claude/settings.json": "modified"})

    async def test_xo_never_runs_a_filter_the_agent_configured(self) -> None:
        """A clean filter is a command in git's config: ``git diff`` would run
        it (the design's cost line); the check reads objects only."""
        tree = self.tree()
        baseline = await self.baseline(tree)
        ran = self.root / "filter-ran"
        write(tree / ".gitattributes", "* filter=evil\n")
        sh(tree, "config", "filter.evil.clean", f"sh -c 'touch {ran}; cat'")
        write(tree / ".claude" / "settings.json", "changed\n")
        found = await self.changes(tree, baseline)
        self.assertFalse(ran.exists(), "XO ran a filter from the repository's config")
        self.assertEqual(found, {".claude/settings.json": "modified", i.GIT_CONFIG: "modified"})
        sh(tree, "diff", "--name-only", self.base)                 # what the design's command would have done
        self.assertTrue(ran.exists())


class GitOwnCorpus(Case):
    """git's own files: a hash snapshot taken when the worktree is made."""

    async def test_hooks_path_and_fsmonitor_are_found(self) -> None:
        for key, value in (("core.hooksPath", "/tmp/hooks"), ("core.fsmonitor", "/tmp/monitor")):
            with self.subTest(key=key):
                tree = self.tree()
                baseline = await self.baseline(tree)
                sh(tree, "config", key, value)
                self.assertEqual(await self.changes(tree, baseline), {i.GIT_CONFIG: "modified"})

    async def test_a_new_hook_and_an_edited_sample_are_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        hooks = self.project / ".git" / "hooks"
        write(hooks / "pre-commit", "#!/bin/sh\ncurl evil\n")
        sample = sorted(hooks.iterdir())[0]
        sample.chmod(0o644)
        found = await self.changes(tree, baseline)
        self.assertEqual(found, {f"{i.GIT_HOOKS}/pre-commit": "added", f"{i.GIT_HOOKS}/{sample.name}": "modified"})

    async def test_a_symlinked_hooks_folder_is_found_and_not_read_through(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        hooks = self.project / ".git" / "hooks"
        copy = self.root / "hooks-copy"
        shutil.copytree(hooks, copy)
        shutil.rmtree(hooks)
        os.symlink(copy, hooks)
        found = await self.changes(tree, baseline)
        self.assertEqual(found.pop(i.GIT_HOOKS), "added")
        self.assertTrue(found and set(found.values()) == {"deleted"}, found)

    async def test_config_worktree_is_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        admin = Path(sh(tree, "rev-parse", "--absolute-git-dir").strip())
        write(admin / "config.worktree", "[core]\n\thooksPath = /tmp/h\n")
        self.assertEqual(await self.changes(tree, baseline), {i.GIT_WORKTREE_CONFIG: "added"})

    async def test_a_repointed_dotgit_file_is_found(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        other = self.root / "other"
        sh(self.root, "init", "-q", str(other))
        write(tree / ".git", f"gitdir: {other / '.git'}\n")
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        # The other repository's fresh config and sample hooks are byte for byte git's defaults.
        self.assertEqual({ch.path: ch.change for ch in found.changes}, {i.DOTGIT: "modified"})
        self.assertIn("couldn't read the thread's base commit", found.failed)    # the other repository lacks it


class MainCheckout(Case):
    """What a runtime reads in the project's main checkout while it runs in a
    worktree (claude: ``.claude/settings.local.json``, live S16-1): a hash
    snapshot when the worktree is made, "nothing there" included."""

    MAIN = Watch((".claude/settings.local.json",), core=())

    async def inspect(self, tree: Path, baseline: dict | None) -> i.Inspection:
        return await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline, main=(self.project, self.MAIN))

    async def test_a_local_settings_file_appearing_or_changing_in_the_main_checkout_is_found(self) -> None:
        tree = self.tree()
        first = await self.inspect(tree, None)
        self.assertNotIn(f"{i.MAIN}.claude/settings.local.json", first.snapshot)      # nothing there
        local = self.project / ".claude" / "settings.local.json"
        write(local, '{"permissions": {"allow": ["Bash"]}}\n')
        found = await self.inspect(tree, first.snapshot)
        self.assertEqual({ch.path: ch.change for ch in found.changes}, {f"{i.MAIN}.claude/settings.local.json": "added"})
        (shown,) = await i.describe(tree, found.changes, found.tree)
        self.assertIn('+{"permissions"', shown["diff"])
        later = self.tree()
        second = await self.inspect(later, None)                    # a later thread: its baseline has it
        write(local, '{"env": {"X": "1"}}\n')
        found = await self.inspect(later, second.snapshot)
        self.assertEqual({ch.path: ch.change for ch in found.changes}, {f"{i.MAIN}.claude/settings.local.json": "modified"})

    async def test_a_symlinked_main_checkout_claude_folder_is_found(self) -> None:
        tree = self.tree()
        first = await self.inspect(tree, None)
        shutil.rmtree(self.project / ".claude")
        os.symlink(self.root, self.project / ".claude")
        found = await self.inspect(tree, first.snapshot)
        self.assertEqual({ch.path: ch.change for ch in found.changes}, {f"{i.MAIN}.claude": "added"})

    async def test_the_rest_of_the_main_checkout_is_not_watched(self) -> None:
        tree = self.tree()
        first = await self.inspect(tree, None)
        write(self.project / ".claude" / "settings.json", "the person's own edit\n")
        write(self.project / ".xo" / "notes.md", "XO's own\n")
        self.assertEqual((await self.inspect(tree, first.snapshot)).changes, ())


class Unwatched(Case):
    async def test_unwatched_changes_are_not_reported(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        write(tree / "README.md", "edited\n")
        write(tree / "src" / "new.py", "x = 1\n")
        write(tree / ".xo" / "todos.json", '{"sessions": {}}\n')             # XO's own store writes it
        write(tree / ".xo" / "worktrees" / "thread-9" / "file", "a nested checkout\n")
        write(tree / ".claude-not" / "x", "a look-alike folder\n")
        sh(tree, "commit", "-q", "-am", "ordinary work")
        self.assertEqual(await self.changes(tree, baseline), {})

    async def test_a_cli_home_and_the_state_root_are_never_watched(self) -> None:
        home = self.root / "home"
        write(home / ".claude" / "settings.json", "{}\n")
        write(home / ".xo" / "notes.md", "n\n")
        watch = Watch(ALL_CLIS, skip=(Path(os.path.realpath(home / ".claude")),))
        first = await i.inspect(home, watch, base_commit=None, baseline=None)
        self.assertEqual(sorted(first.snapshot), [".xo/notes.md"])
        write(home / ".claude" / "settings.json", "changed by the CLI itself\n")
        found = await i.inspect(home, watch, base_commit=None, baseline=first.snapshot)
        self.assertEqual(found.changes, ())


class Snapshots(Case):
    """A non-git thread and a plain session: a hash snapshot at the first turn."""

    async def test_a_non_git_folder_is_compared_with_its_snapshot(self) -> None:
        folder = self.root / "plain"
        write(folder / ".claude" / "settings.json", "{}\n")
        write(folder / ".xo" / "notes.md", "n\n")
        baseline = await self.baseline(folder, worktree=False)
        self.assertEqual({path: group for path, (_sha, group) in baseline.items()},
                         {".claude/settings.json": i.PROJECT_CONFIG, ".xo/notes.md": i.XO})
        write(folder / ".claude" / "settings.json", '{"hooks": 1}\n')
        write(folder / ".mcp.json", "{}\n")
        (folder / ".xo" / "notes.md").unlink()
        self.assertEqual(await self.changes(folder, baseline, worktree=False),
                         {".claude/settings.json": "modified", ".mcp.json": "added", ".xo/notes.md": "deleted"})

    async def test_a_plain_session_in_a_git_checkout_snapshots_git_s_own_files_too(self) -> None:
        baseline = await self.baseline(self.project, worktree=False)
        self.assertIn(i.GIT_CONFIG, baseline)
        self.assertIn(".claude/settings.json", baseline)
        sh(self.project, "config", "core.hooksPath", "/tmp/h")
        (self.project / ".mcp.json").chmod(0o755)
        self.assertEqual(await self.changes(self.project, baseline, worktree=False),
                         {i.GIT_CONFIG: "modified", ".mcp.json": "modified"})

    async def test_git_init_in_a_non_git_folder_is_found(self) -> None:
        folder = self.root / "plain"
        folder.mkdir()
        baseline = await self.baseline(folder, worktree=False)
        sh(folder, "init", "-q")
        found = await self.changes(folder, baseline, worktree=False)
        self.assertEqual((found[i.DOTGIT], found[i.GIT_CONFIG]), ("added", "added"))


class Reports(Case):
    async def test_a_diff_shows_both_versions_through_redact(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        write(tree / ".claude" / "settings.json", '{"hooks": "curl evil"}\n')
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        with mock.patch.object(i, "redact", side_effect=lambda text: text.replace("evil", "[redacted]")) as red:
            (shown,) = await i.describe(tree, found.changes, found.tree)
        red.assert_called_once()
        self.assertIn("-base .claude/settings.json", shown["diff"])
        self.assertIn('+{"hooks": "curl [redacted]"}', shown["diff"])
        self.assertEqual((shown["change"], shown["against"], shown["group"]),
                         ("modified", "base_commit", i.PROJECT_CONFIG))

    async def test_a_snapshot_change_shows_what_is_there_now_and_says_why_not_before(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        sh(tree, "config", "core.fsmonitor", "/tmp/monitor")
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        (shown,) = await i.describe(tree, found.changes, found.tree)
        self.assertIn("only a hash of the earlier version", shown["diff"])
        self.assertIn("+\tfsmonitor = /tmp/monitor", shown["diff"])

    async def test_a_link_s_target_and_a_mode_change_are_shown(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        (tree / ".codex" / "config.toml").chmod(0o755)
        (tree / ".mcp.json").unlink()
        os.symlink("/etc/passwd", tree / ".mcp.json")
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        shown = {d["path"]: d["diff"] for d in await i.describe(tree, found.changes, found.tree)}
        self.assertIn("(mode 100644 → 100755)", shown[".codex/config.toml"])
        self.assertIn("+symlink to /etc/passwd", shown[".mcp.json"])
        self.assertNotIn("root:", shown[".mcp.json"])                    # never read through the link

    async def test_a_long_diff_is_cut_after_redaction(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        write(tree / ".claude" / "settings.json", "x" * (i.MAX_DIFF_CHARS * 2) + "\n")
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        (shown,) = await i.describe(tree, found.changes, found.tree)
        self.assertLess(len(shown["diff"]), i.MAX_DIFF_CHARS + 100)
        self.assertIn("more characters not shown", shown["diff"])


class Failures(Case):
    async def test_a_gone_folder_is_reported(self) -> None:
        tree = self.tree()
        await self.baseline(tree)
        shutil.rmtree(tree)
        with self.assertRaisesRegex(CheckFailed, "is gone"):
            await i.inspect(tree, WATCH, base_commit=self.base, baseline={})

    async def test_a_worktree_without_its_dotgit_is_reported_with_git_s_own_change(self) -> None:
        tree = self.tree()
        baseline = await self.baseline(tree)
        (tree / ".git").unlink()
        found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=baseline)
        self.assertIn("no longer a git checkout", found.failed)
        changed = {ch.path: ch.change for ch in found.changes}
        self.assertEqual((changed[i.DOTGIT], changed[i.GIT_CONFIG]), ("deleted", "deleted"))    # nothing to find them by

    async def test_git_missing_is_reported(self) -> None:
        tree = self.tree()
        missing = CommandResult(argv=["git"], returncode=-1, output="", duration_seconds=0.0, binary_missing=True)
        with mock.patch.object(i, "git", return_value=missing):
            found = await i.inspect(tree, WATCH, base_commit=self.base, baseline=None)
        self.assertIn("git isn't installed", found.failed)

    async def test_an_unreadable_base_commit_is_reported(self) -> None:
        tree = self.tree()
        found = await i.inspect(tree, WATCH, base_commit="0" * 40, baseline=None)
        self.assertIn("couldn't read the thread's base commit", found.failed)

    async def test_a_tree_too_large_to_check_is_reported(self) -> None:
        tree = self.tree()
        for n in range(12):
            write(tree / ".xo" / "many" / f"{n}.md", "n\n")
        with mock.patch.object(i, "MAX_ENTRIES", 10), self.assertRaisesRegex(CheckFailed, "more than 10 entries"):
            await i.inspect(tree, WATCH, base_commit=self.base, baseline=None)

    async def test_a_fifo_is_hashed_never_opened(self) -> None:
        folder = self.root / "plain"
        folder.mkdir()
        baseline = await self.baseline(folder, worktree=False)
        (folder / ".claude").mkdir()
        os.mkfifo(folder / ".claude" / "settings.json")           # opening it to read would block forever
        found = await asyncio.wait_for(self.changes(folder, baseline, worktree=False), 5)
        self.assertEqual(found, {".claude/settings.json": "added"})


class Cost(Case):
    async def test_the_walk_and_hashes_run_off_the_event_loop(self) -> None:
        tree = self.tree()
        ticks = 0

        async def ticking() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        real = i.scan

        def slow_scan(folder, watch):
            time.sleep(0.3)                     # a slow disk: blocking, in the scan's thread
            return real(folder, watch)
        ticker = asyncio.create_task(ticking())
        try:
            with mock.patch.object(i, "scan", slow_scan):
                await i.inspect(tree, WATCH, base_commit=self.base, baseline=None)
        finally:
            ticker.cancel()
        self.assertGreater(ticks, 10)


if __name__ == "__main__":
    unittest.main()
