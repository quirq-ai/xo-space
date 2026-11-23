"""Tests for Space's file version store, watcher, events, file ops with undo, version routes, and /paths, /search."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.coworker import finder
from services.project_files import edits, events, ops, save, watch, writer_key
from services.project_files.address import FileRefused, version_of

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.projects = self.base / "projects"
        self.project = self.projects / "demo"
        self.project.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state"), **GIT_ENV})
        env.start()
        self.addCleanup(env.stop)
        edits._pruned_at.clear()
        self.clock = T0
        clock = mock.patch.object(edits, "_now", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.root = self.project.resolve()

    def tick(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def put(self, rel: str, data: str | bytes) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def watched(self, *rels: str, by=None, added: tuple[str, ...] = ()) -> None:
        """What the watcher does with one batch of changed paths."""
        w = watch.Watch()
        w.attributor = by
        w.process("demo", self.root, [str(self.root / r) for r in rels], {str(self.root / r) for r in added})

    def refused(self, code: str, fn, *args, **kwargs) -> FileRefused:
        with self.assertRaises(FileRefused) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def records(self, real: str | None = None) -> list[edits.Edit]:
        return edits.history("demo", real)


# ── the store ─────────────────────────────────────────────────────────────────


class MergeWindowTests(Case):
    def test_three_saves_a_second_apart_are_one_version_and_a_later_one_is_another(self) -> None:
        self.put("a.md", "0")
        v = [version_of(str(i).encode()) for i in range(5)]
        first = save.save("demo", "a.md", "1", v[0])
        self.tick(1)
        second = save.save("demo", "a.md", "2", v[1])
        self.tick(1)
        third = save.save("demo", "a.md", "3", v[2])
        (one,) = self.records("a.md")
        self.assertEqual({first.edit_id, second.edit_id, third.edit_id}, {one.id}, "the record keeps its id")
        self.assertEqual((one.from_version, one.to_version, one.kind), (v[0], v[3], "edit"))
        self.assertEqual(edits.kept_version("demo", v[0]), b"0", "its start is kept for undo")
        self.tick(11)
        later = save.save("demo", "a.md", "4", v[3])
        newest, older = self.records("a.md")
        self.assertEqual((newest.id, newest.from_version, older.id), (later.edit_id, v[3], one.id))

    def test_another_writer_starts_a_new_version(self) -> None:
        self.put("a.md", "0")
        save.save("demo", "a.md", "1", version_of(b"0"))
        self.tick(1)
        self.put("a.md", "2")
        self.watched("a.md")
        disk, ui = self.records("a.md")
        self.assertEqual((ui.by_kind, disk.by_kind, disk.from_version), ("ui", "disk", version_of(b"1")))

    def test_an_agent_s_writes_merge_per_session(self) -> None:
        self.put("a.md", "0")
        self.watched("a.md", by=lambda _p: {"session_id": "s1", "turn_id": "t1"})
        self.tick(2)
        self.put("a.md", "1")
        self.watched("a.md", by=lambda _p: {"session_id": "s1", "turn_id": "t1"})
        self.assertEqual(len(self.records("a.md")), 1)
        self.tick(2)
        self.put("a.md", "2")
        self.watched("a.md", by=lambda _p: {"session_id": "s2", "turn_id": "t9"})
        self.assertEqual([r.by["session_id"] for r in self.records("a.md")], ["s2", "s1"])


class NoDoubleRecordTests(Case):
    def test_the_watcher_doesnt_record_the_person_s_own_save_again(self) -> None:
        self.put("a.md", "old\n")
        done = save.save("demo", "a.md", "new\n", version_of(b"old\n"))
        self.tick(30)
        self.watched("a.md")
        (only,) = self.records("a.md")
        self.assertEqual((only.id, only.by_kind), (done.edit_id, "ui"))

    def test_identical_bytes_are_not_a_version(self) -> None:
        self.put("a.md", "x")
        self.watched("a.md")
        self.tick(30)
        self.put("a.md", "x")
        self.watched("a.md")
        self.assertEqual(len(self.records("a.md")), 1)


class FailedWriteTests(Case):
    def test_a_failed_append_leaves_nothing_behind(self) -> None:
        self.put("a.txt", "one\n")
        self.watched("a.txt", added=("a.txt",))
        log = edits.state_dir("demo") / edits.LOG_NAME
        before = log.read_bytes()
        self.put("a.txt", "two\n")
        real_fsync = os.fsync

        def full_disk(fd: int) -> None:                                 # only the log's own write fails
            if os.readlink(f"/proc/self/fd/{fd}").endswith(edits.LOG_NAME):
                raise OSError(28, "No space left on device")
            real_fsync(fd)
        with mock.patch.object(edits.os, "fsync", side_effect=full_disk):
            with self.assertRaises(OSError):
                edits.observe("demo", [edits.Seen("a.txt", edits.content_at(self.root / "a.txt"),
                                                  edits.writer(edits.DISK))])
        self.assertEqual(log.read_bytes(), before)                      # no half line
        self.assertEqual([r.kind for r in self.records("a.txt")], ["create"])   # no phantom in the cache
        self.watched("a.txt")                                           # the change is still seen
        self.assertEqual(self.records("a.txt")[0].to_version, edits.content_at(self.root / "a.txt").version)


class StaleStateTests(Case):
    def test_a_save_is_recorded_even_when_the_watcher_missed_a_change(self) -> None:
        self.put("a.txt", "A")
        self.watched("a.txt")
        self.put("a.txt", "B")                                  # nobody watching
        self.tick(30)
        done = save.save("demo", "a.txt", "A", version_of(b"B"))
        self.assertIsNotNone(done.edit_id)
        newest = self.records("a.txt")[0]
        self.assertEqual((newest.by_kind, newest.from_version, newest.to_version),
                         ("ui", version_of(b"B"), version_of(b"A")))


class AttributionTests(Case):
    def test_a_running_turn_makes_it_the_agent_s_and_none_the_disk_s(self) -> None:
        self.put("w/a.md", "a")
        self.watched("w/a.md", added=("w/a.md",),
                     by=lambda p: {"session_id": "s1", "thread_ts": "1.1", "turn_id": "t1"})
        self.put("b.md", "b")
        self.watched("b.md", added=("b.md",))
        (agent,) = self.records("w/a.md")
        (disk,) = self.records("b.md")
        self.assertEqual((agent.kind, agent.by["kind"], agent.by["session_id"], agent.by["turn_id"]),
                         ("create", "agent", "s1", "t1"))
        self.assertEqual((disk.kind, disk.by["kind"]), ("create", "disk"))

    def test_turn_end_sets_the_reply_and_creates_what_the_watcher_missed(self) -> None:
        self.put("a.md", "a")
        self.watched("a.md", by=lambda p: {"session_id": "s1", "turn_id": "t1"})
        by = edits.writer(edits.AGENT, session_id="s1", thread_ts="1.1", turn_id="t1", message_id="m1")
        record, outcome = edits.attribute("demo", "a.md", since=T0 - timedelta(seconds=5), by=by,
                                          current=edits.content_at(self.root / "a.md"))
        self.assertEqual((outcome, record.by["message_id"]), ("attributed", "m1"))
        self.assertEqual(self.records("a.md")[0].by, by)
        again = edits.attribute("demo", "a.md", since=T0 - timedelta(seconds=5), by=by,
                                current=edits.content_at(self.root / "a.md"))
        self.assertEqual(again[1], "unchanged")
        self.put("new.md", "n")                                 # no watcher saw it
        created, outcome = edits.attribute("demo", "new.md", since=T0, by=by,
                                           current=edits.content_at(self.root / "new.md"), created=True)
        self.assertEqual((outcome, created.kind, created.by["message_id"]), ("new", "create", "m1"))

    def test_turn_end_never_takes_a_later_turn_s_write(self) -> None:
        self.put("a.md", "t1")
        self.watched("a.md", by=lambda p: {"session_id": "s1", "turn_id": "t1"})
        self.tick(30)
        self.put("a.md", "t2")
        self.watched("a.md", by=lambda p: {"session_id": "s1", "turn_id": "t2"})
        by = edits.writer(edits.AGENT, session_id="s1", turn_id="t1", message_id="m1")
        edits.attribute("demo", "a.md", since=T0 - timedelta(seconds=1), until=T0 + timedelta(seconds=2), by=by,
                        current=edits.content_at(self.root / "a.md"))
        newest, older = self.records("a.md")
        self.assertEqual((newest.by["turn_id"], newest.by["message_id"]), ("t2", None))
        self.assertEqual((older.by["turn_id"], older.by["message_id"]), ("t1", "m1"))
        self.put("b.md", "later")
        self.watched("b.md", by=lambda p: {"session_id": "s1", "turn_id": "t2"})
        found, outcome = edits.attribute("demo", "b.md", since=T0 - timedelta(seconds=1),
                                         until=T0 + timedelta(seconds=2), by=by,
                                         current=edits.content_at(self.root / "b.md"))
        self.assertEqual((found, outcome), (None, "unchanged"), "a later turn's file isn't made this turn's")

    def test_the_person_s_save_is_never_handed_to_an_agent(self) -> None:
        self.put("a.md", "a")
        save.save("demo", "a.md", "b", version_of(b"a"))
        by = edits.writer(edits.AGENT, session_id="s1", turn_id="t1", message_id="m1")
        edits.attribute("demo", "a.md", since=T0 - timedelta(seconds=5), by=by,
                        current=edits.content_at(self.root / "a.md"))
        self.assertEqual([r.by_kind for r in self.records("a.md")], ["ui"])


class WatcherRecordTests(Case):
    def test_delete_and_a_rename_seen_as_a_pair(self) -> None:
        self.put("a.md", "same")
        self.watched("a.md")
        self.tick(20)
        os.rename(self.project / "a.md", self.project / "b.md")
        self.watched("a.md", "b.md", added=("b.md",))
        (renamed,) = self.records("b.md")
        self.assertEqual((renamed.kind, renamed.old_path), ("rename", "a.md"))
        self.tick(20)
        (self.project / "b.md").unlink()
        self.watched("b.md")
        self.assertEqual(self.records("b.md")[0].kind, "delete")

    def test_noise_and_paths_through_links_are_skipped(self) -> None:
        self.put("node_modules/x/i.js", "1")
        self.put(".git/config", "1")
        self.put(".xo/project.json", "{}")
        self.put(".xo/worktrees/t1/src/a.py", "1")
        self.put(".xo/worktrees/t1/.xo/own.json", "1")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "o.txt").write_text("o")
        (self.project / "link").symlink_to(outside)
        self.watched("node_modules/x/i.js", ".git/config", ".xo/project.json", ".xo/worktrees/t1/src/a.py",
                     ".xo/worktrees/t1/.xo/own.json", "link/o.txt")
        self.assertEqual([r.path for r in self.records()], [".xo/worktrees/t1/src/a.py"])
        self.assertEqual(self.records()[0].worktree, "t1")

    def test_a_folder_that_appears_or_vanishes_at_once(self) -> None:
        self.put("docs/a.md", "a")
        self.put("docs/sub/b.md", "b")
        self.watched("docs")
        self.assertEqual(sorted(r.path for r in self.records()), ["docs/a.md", "docs/sub/b.md"])
        self.tick(20)
        shutil.move(str(self.project / "docs"), str(self.base / "elsewhere"))
        self.watched("docs")
        self.assertEqual(sorted(r.kind for r in self.records()[:2]), ["delete", "delete"])

    def test_a_credential_file_s_versions_never_reach_the_disk(self) -> None:
        self.put(".env", "TOKEN=1\n")
        self.watched(".env")
        blobs = edits.state_dir("demo") / "blobs"
        self.assertFalse(blobs.exists() and any(blobs.iterdir()))
        (record,) = self.records(".env")
        self.assertTrue(edits.is_kept("demo", record), "held in memory for a while")
        with mock.patch.object(edits, "_volatile", {}):
            self.assertFalse(edits.is_kept("demo", record), "content_kept: false once forgotten")

    def test_too_many_changes_at_once_ask_for_a_rescan(self) -> None:
        for i in range(5):
            self.put(f"f{i}.txt", str(i))
        with mock.patch.object(watch, "OVERFLOW_PATHS", 3):
            w = watch.Watch()
            overflow = w.process("demo", self.root, [str(self.root / f"f{i}.txt") for i in range(5)], set())
        self.assertTrue(overflow)
        self.assertEqual(self.records(), [], "nothing guessed")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class CheckoutTests(Case):
    """A new file matching git's bytes is a checkout's, not anyone's new version."""

    def setUp(self) -> None:
        super().setUp()
        sh(self.project, "init", "-q", "-b", "main")
        for i in range(30):
            self.put(f"src/m{i}.py", f"print({i})\n")
        self.put("README.md", "hello\n")
        sh(self.project, "add", "-A")
        sh(self.project, "commit", "-q", "-m", "first")

    def add_worktree(self, name: str) -> str:
        rel = f".xo/worktrees/{name}"
        sh(self.project, "worktree", "add", "-q", "-b", name, str(self.project / rel))
        return rel

    def checked_out(self, rel: str) -> tuple[str, ...]:
        """Each file of a new worktree as the watcher sees them (the folder itself is noise)."""
        return tuple(sorted(p.relative_to(self.project).as_posix()
                            for p in (self.project / rel).rglob("*") if p.is_file() and p.name != ".git"))

    def test_a_new_thread_worktree_records_nothing(self) -> None:
        rel = self.add_worktree("t1")
        files = self.checked_out(rel)
        self.assertEqual(len(files), 31)
        self.watched(*files, added=files, by=lambda _p: {"session_id": "cse_1"})
        self.assertEqual(self.records(), [])

    def test_what_changes_in_it_afterwards_is_still_recorded(self) -> None:
        rel = self.add_worktree("t1")
        files = self.checked_out(rel)
        self.watched(*files, added=files)
        self.put(f"{rel}/src/m1.py", "print('edited')\n")
        self.put(f"{rel}/new.py", "brand new\n")
        self.watched(f"{rel}/src/m1.py", f"{rel}/new.py", added=(f"{rel}/new.py",))
        self.assertEqual(sorted((r.path, r.kind) for r in self.records()),
                         [(f"{rel}/new.py", "create"), (f"{rel}/src/m1.py", "edit")])

    def test_a_known_file_put_back_to_the_committed_bytes_is_a_change(self) -> None:
        self.put("README.md", "changed\n")
        self.watched("README.md")
        self.tick(20)
        (self.project / "README.md").unlink()
        self.watched("README.md")
        self.tick(20)
        sh(self.project, "checkout", "--", "README.md")
        self.watched("README.md", added=("README.md",))
        self.assertEqual([r.kind for r in self.records("README.md")], ["create", "delete", "edit"])

    def test_bytes_git_doesn_t_hold_are_recorded(self) -> None:
        self.put("src/m2.py", "print(2)\r\n")                  # a filter's or anyone's other bytes
        self.put("src/extra.py", "x\n")
        (self.project / "src" / "m3.py").unlink()
        self.watched("src/m2.py", "src/extra.py", added=("src/m2.py", "src/extra.py"))
        self.assertEqual(sorted(r.path for r in self.records()), ["src/extra.py", "src/m2.py"])

    def test_a_file_without_a_repository_is_recorded(self) -> None:
        shutil.rmtree(self.project / ".git")
        self.put("loose.txt", "1\n")
        self.watched("loose.txt", added=("loose.txt",))
        self.assertEqual([r.path for r in self.records()], ["loose.txt"])

    def test_it_waits_while_git_writes_the_checkout(self) -> None:
        from services.project_files import checkout
        lock = self.project / ".git" / "index.lock"
        self.put("late.py", "late\n")
        lock.write_text("")

        def git_finishes() -> None:                      # git writes the index, then lets it go
            subprocess.run(["git", "add", "late.py"], cwd=self.project, check=True,
                           env={**os.environ, **GIT_ENV, "GIT_INDEX_FILE": str(self.project / ".git" / "idx")})
            os.replace(self.project / ".git" / "idx", self.project / ".git" / "index")
            lock.unlink()
        timer = __import__("threading").Timer(0.3, git_finishes)
        timer.start()
        self.addCleanup(timer.cancel)
        content = edits.content_at(self.project / "late.py")
        self.assertEqual(checkout.held_by_git(self.root, {"late.py": content}), {"late.py"})


# ── rename, delete, mkdir ─────────────────────────────────────────────────────


class OpsTests(Case):
    def test_rename_moves_records_and_undoes(self) -> None:
        self.put("a.md", "hello\n")
        moved = ops.rename("demo", "a.md", "docs/b.md")
        self.assertEqual((moved.relative_path, moved.old_relative_path, moved.kind, moved.version),
                         ("docs/b.md", "a.md", "file", version_of(b"hello\n")))
        self.assertFalse((self.project / "a.md").exists())
        (record,) = self.records("docs/b.md")
        self.assertEqual((record.id, record.kind, record.old_path), (moved.edit_id, "rename", "a.md"))
        self.tick(20)
        self.watched("a.md", "docs/b.md", added=("docs/b.md",))
        self.assertEqual(len(self.records()), 1, "the watcher sees nothing new after a rename")
        back = save.undo("demo", moved.edit_id)
        self.assertEqual(((self.project / "a.md").read_text(), back.relative_path), ("hello\n", "a.md"))
        self.assertEqual(self.records("a.md")[0].kind, "undo")

    def test_a_folder_rename_is_one_record_per_file_under_one_id(self) -> None:
        self.put("src/a.py", "a")
        self.put("src/lib/b.py", "b")
        self.put("src/node_modules/x.js", "x")
        moved = ops.rename("demo", "src", "app")
        self.assertEqual((moved.kind, moved.version), ("folder", None))
        group = [r for r in self.records() if r.group == moved.edit_id]
        self.assertEqual(sorted((r.path, r.old_path) for r in group),
                         [("app/a.py", "src/a.py"), ("app/lib/b.py", "src/lib/b.py")])
        self.refused("invalid_path", ops.rename, "demo", "app", "app/inner")
        save.undo("demo", group[0].id)                         # any of its records undoes the whole move
        self.assertTrue((self.project / "src/lib/b.py").exists())
        self.assertTrue((self.project / "src/node_modules/x.js").exists())

    def test_rename_won_t_replace_what_is_there(self) -> None:
        self.put("a.md", "a")
        self.put("b.md", "b")
        refused = self.refused("exists", ops.rename, "demo", "a.md", "b.md")
        self.assertEqual(refused.extra["current"], {"version": version_of(b"b")})
        self.refused("not_found", ops.rename, "demo", "nope.md", "c.md")

    def test_the_places_rules_hold_at_both_ends(self) -> None:
        self.put("hook.sh", "#!/bin/sh\n")
        self.put(".claude/settings.json", "{}")
        self.put(".xo/project.json", "{}")
        self.put("tools/.git/HEAD", "ref")
        cases = [
            ("rename into a place that runs code", ops.rename, ("demo", "hook.sh", ".git/hooks/pre-commit"), "confirm_required"),
            ("rename out of one", ops.rename, ("demo", ".claude/settings.json", "settings.json"), "confirm_required"),
            ("a folder holding a repository", ops.rename, ("demo", "tools", "tools2"), "confirm_required"),
            ("rename into XO's records", ops.rename, ("demo", "hook.sh", ".xo/hook.sh"), "read_only_place"),
            ("rename XO's records", ops.rename, ("demo", ".xo/project.json", "p.json"), "read_only_place"),
            ("delete one that runs code", ops.delete, ("demo", ".claude/settings.json"), "confirm_required"),
            ("delete XO's records", ops.delete, ("demo", ".xo"), "read_only_place"),
            ("mkdir in XO's records", ops.mkdir, ("demo", ".xo/new"), "read_only_place"),
        ]
        for label, fn, args, code in cases:
            with self.subTest(label):
                refused = self.refused(code, fn, *args)
                if code == "confirm_required":
                    self.assertEqual((refused.status, refused.extra), (428, {"reason": "runs_code"}))
        moved = ops.rename("demo", ".claude/settings.json", "settings.json", confirm="runs_code")
        self.assertEqual(self.records("settings.json")[0].confirmed, "runs_code")
        self.assertTrue((self.project / "settings.json").exists(), moved)
        outside = self.base / "outside"
        outside.mkdir()
        (self.project / "out").symlink_to(outside)
        self.refused("outside_project", ops.rename, "demo", "hook.sh", "out/hook.sh")

    def test_delete_goes_to_the_trash_and_comes_back(self) -> None:
        self.put("docs/a.md", "a\n")
        done = ops.delete("demo", "docs/a.md", base_version=version_of(b"a\n"))
        self.assertEqual((done.kind, done.count), ("file", 1))
        self.assertFalse((self.project / "docs/a.md").exists())
        trash = edits.state_dir("demo") / edits.TRASH_DIRNAME / done.edit_id
        self.assertTrue((trash / ops.TRASH_ITEM).exists(), "kept in Space's state, never the project")
        (record,) = self.records("docs/a.md")
        self.assertEqual((record.kind, record.from_version, record.to_version), ("delete", version_of(b"a\n"), None))
        self.put("docs/a.md", "squatter")
        self.refused("exists", save.undo, "demo", done.edit_id)
        (self.project / "docs/a.md").unlink()
        save.undo("demo", done.edit_id)
        self.assertEqual((self.project / "docs/a.md").read_text(), "a\n")
        self.assertFalse(trash.exists())
        self.assertEqual(self.records("docs/a.md")[0].kind, "undo")

    def test_a_failed_restore_leaves_no_credential_in_the_trash(self) -> None:
        self.put(".env", "SECRET=1\n")
        done = ops.delete("demo", ".env")
        trash = edits.state_dir("demo") / edits.TRASH_DIRNAME / done.edit_id
        self.assertFalse(os.path.lexists(trash / ops.TRASH_ITEM), "held in memory, not in the trash")
        self.put(".env", "NEW=1\n")
        self.refused("exists", save.undo, "demo", done.edit_id)
        self.assertFalse(os.path.lexists(trash / ops.TRASH_ITEM), "not left behind by the refused undo")
        (self.project / ".env").unlink()
        save.undo("demo", done.edit_id)
        self.assertEqual((self.project / ".env").read_text(), "SECRET=1\n")
        with mock.patch.object(edits, "_volatile", {}):
            self.put("k.pem", "key")
            gone = ops.delete("demo", "k.pem")
            edits._volatile.clear()
            self.assertEqual(self.refused("not_undoable", save.undo, "demo", gone.edit_id).status, 410)

    def test_a_restore_from_the_trash_is_listed_as_not_undoable(self) -> None:
        self.put("a.md", "a")
        done = ops.delete("demo", "a.md")
        back = save.undo("demo", done.edit_id)
        items = {i["id"]: i for i in edits.listing("demo", self.root, path="a.md")}
        self.assertEqual((items[back.edit_id]["kind"], items[back.edit_id]["undoable"]), ("undo", False))
        self.refused("not_undoable", save.undo, "demo", back.edit_id)

    def test_delete_checks_the_version_it_was_shown(self) -> None:
        self.put("a.md", "now")
        refused = self.refused("file_changed", ops.delete, "demo", "a.md", base_version=version_of(b"then"))
        self.assertEqual(refused.extra["current"]["version"], version_of(b"now"))
        self.assertTrue((self.project / "a.md").exists())

    def test_a_folder_s_credentials_stay_out_of_the_trash(self) -> None:
        self.put("cfg/.env", "TOKEN=1\n")
        self.put("cfg/app.toml", "a=1\n")
        done = ops.delete("demo", "cfg")
        self.assertEqual((done.kind, done.count), ("folder", 2))
        trash = edits.state_dir("demo") / edits.TRASH_DIRNAME / done.edit_id / ops.TRASH_ITEM
        self.assertEqual(sorted(p.name for p in trash.iterdir()), ["app.toml"])
        save.undo("demo", done.edit_id)
        self.assertEqual((self.project / "cfg/.env").read_text(), "TOKEN=1\n")

    def test_mkdir(self) -> None:
        self.assertEqual(ops.mkdir("demo", "a/b/c"), "a/b/c")
        self.assertTrue((self.project / "a/b/c").is_dir())
        self.refused("exists", ops.mkdir, "demo", "a/b")
        self.refused("invalid_path", ops.mkdir, "demo", "../x")

    def test_nothing_remakes_a_removed_thread_worktrees_folder(self) -> None:
        # A worktree removed (swept, or its thread deleted) comes back through git at the same path; a folder made
        # there by hand would stand in its way.
        self.put(".xo/worktrees/thread-t1/a.md", "a")
        done = ops.delete("demo", ".xo/worktrees/thread-t1/a.md")
        self.put("b.md", "b")
        shutil.rmtree(self.project / ".xo" / "worktrees" / "thread-t1")
        for fn, args in ((ops.mkdir, ("demo", ".xo/worktrees/thread-t1/new")),
                         (ops.rename, ("demo", "b.md", ".xo/worktrees/thread-t1/b.md")),
                         (save.undo, ("demo", done.edit_id))):
            with self.subTest(fn.__name__):
                self.refused("worktree_not_found", fn, *args)
                self.assertFalse((self.project / ".xo" / "worktrees" / "thread-t1").exists())
        self.assertTrue((self.project / "b.md").exists())
        (self.project / ".xo" / "worktrees" / "thread-t1").mkdir()        # back: inside it, folders are made
        self.assertEqual(ops.mkdir("demo", ".xo/worktrees/thread-t1/x/y"), ".xo/worktrees/thread-t1/x/y")

    def test_person_edits_tell_of_renames_and_deletes(self) -> None:
        start = datetime.now(timezone.utc) - timedelta(days=1)
        self.put("a.md", "a")
        self.put("b.md", "b")
        with mock.patch.object(edits, "_now", lambda: datetime.now(timezone.utc)):
            ops.rename("demo", "a.md", "c.md")
            ops.delete("demo", "b.md")
        found = {e["path"]: e for e in edits.edits_since(self.project, start)}
        self.assertEqual((found["c.md"]["from"], found["b.md"]["exists"]), ("a.md", False))
        self.assertNotIn("a.md", found)


# ── the events ────────────────────────────────────────────────────────────────


class EventTests(Case, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.got: list[events.Event] = []

        async def collect(batch):
            self.got.extend(batch)
        events.start(asyncio.get_running_loop())
        events.subscribe(collect)
        self.addAsyncCleanup(events.stop)
        self.addCleanup(events.unsubscribe, collect)

    async def settle(self, seconds: float = 0.05) -> None:
        await asyncio.sleep(seconds)

    async def test_versions_and_attributions_go_out(self) -> None:
        self.put("a.md", "a")
        await asyncio.to_thread(self.watched, "a.md", by=lambda p: {"session_id": "s1", "turn_id": "t1"})
        await self.settle()
        ((type_, pid, data),) = self.got
        self.assertEqual((type_, pid, data["path"], data["kind"], data["by"]["kind"], data["by"]["message_id"]),
                         ("file.version", "demo", "a.md", "edit", "agent", None))
        self.assertEqual(set(data), {"project_id", "path", "worktree", "id", "kind", "old_path", "version",
                                     "prev_version", "by", "at", "size_bytes", "content_kept", "restores"})
        await asyncio.sleep(events.MIN_GAP_S)
        by = edits.writer(edits.AGENT, session_id="s1", turn_id="t1", message_id="m1")
        await asyncio.to_thread(edits.attribute, "demo", "a.md", since=T0 - timedelta(seconds=9), by=by,
                                current=edits.content_at(self.root / "a.md"))
        await self.settle()
        self.assertEqual(self.got[-1], ("file.version.attributed", "demo",
                                        {"project_id": "demo", "path": "a.md", "id": data["id"], "by": by}))

    async def test_a_burst_folds_into_its_newest(self) -> None:
        self.put("a.md", "0")
        for i in range(4):
            self.put("a.md", str(i))
            self.tick(1)
            await asyncio.to_thread(self.watched, "a.md")
        await asyncio.sleep(events.MIN_GAP_S + 0.1)
        sent = [d for t, _p, d in self.got if t == "file.version"]
        self.assertEqual(len(sent), 2, "the first at once, the rest folded into one")
        self.assertEqual(sent[-1]["version"], version_of(b"3"))
        self.assertEqual({d["id"] for d in sent}, {self.records("a.md")[0].id}, "one record through its window")


class WatcherLiveTests(Case, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.got: list[events.Event] = []

        async def collect(batch):
            self.got.extend(batch)
        events.start(asyncio.get_running_loop())
        events.subscribe(collect)
        self.addAsyncCleanup(events.stop)
        self.addCleanup(events.unsubscribe, collect)
        self.watch = watch.Watch()
        self.watch.start(asyncio.get_running_loop())
        self.addAsyncCleanup(self.watch.stop)

    async def wait_for(self, check, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not check():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out")
            await asyncio.sleep(0.05)

    async def test_it_records_what_lands_on_disk_and_says_when_it_lost_track(self) -> None:
        self.watch.touch("demo")
        await self.wait_for(lambda: self.watch.watching("demo"))
        await self.wait_for(lambda: any(t == "file.rescan" for t, _p, _d in self.got))
        self.assertEqual(self.got[0][2], {"project_id": "demo", "worktree": None, "reason": "restart"})
        await asyncio.sleep(0.3)                                # the watch is up
        self.put("notes.md", "hi\n")
        await self.wait_for(lambda: bool(self.records("notes.md")))
        self.assertEqual(self.records("notes.md")[0].by_kind, "disk")
        with mock.patch.object(watch, "OVERFLOW_PATHS", 3):
            for i in range(8):
                self.put(f"many/f{i}.txt", str(i))
            await self.wait_for(lambda: any(d.get("reason") == "overflow" for t, _p, d in self.got
                                            if t == "file.rescan"))


    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    async def test_a_real_worktree_checkout_under_the_live_watch_records_nothing(self) -> None:
        sh(self.project, "init", "-q", "-b", "main")
        for i in range(400):
            self.put(f"pkg{i % 20}/f{i}.py", f"x = {i}\n" * 50)
        sh(self.project, "add", "-A")
        sh(self.project, "commit", "-q", "-m", "first")
        self.watch.touch("demo")
        await self.wait_for(lambda: self.watch.watching("demo"))
        await asyncio.sleep(0.3)                                # the watch is up
        await asyncio.to_thread(sh, self.project, "worktree", "add", "-q", "-b", "t1",
                                str(self.project / ".xo" / "worktrees" / "t1"))
        self.put(".xo/worktrees/t1/mine.py", "the agent's own\n")
        await self.wait_for(lambda: bool(self.records(".xo/worktrees/t1/mine.py")))
        await asyncio.sleep(0.5)
        self.assertEqual([r.path for r in self.records()], [".xo/worktrees/t1/mine.py"])


# ── the routes ────────────────────────────────────────────────────────────────


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RouteTests(Case):
    def setUp(self) -> None:
        super().setUp()
        from routers.cowork_agent.bff.xo_project_files import router as files_router
        from routers.cowork_agent.coworker.finder import router as finder_router
        from routers.cowork_agent.bff.xo_projects import router as projects_router

        app = FastAPI()
        app.include_router(projects_router)
        app.include_router(files_router)
        app.include_router(finder_router)
        self.client = TestClient(app, base_url="http://127.0.0.1:5002")
        self.key = (writer_key.ensure(), writer_key.current())[1]
        sh(self.project, "init", "-q", "-b", "main")
        self.put(".gitignore", "build/\n*.log\n")
        self.put(".xo/.gitignore", "worktrees/\n")              # a real project's layout: .xo/ itself is git's
        self.put(".xo/project.json", '{"name": "answer"}\n')
        self.put("docs/a.md", "one\n")
        self.put("js/app.js", "const answer = 42;\nconst Answer = 'x';\n")
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "base")
        sh(self.project, "worktree", "add", "-q", ".xo/worktrees/t1", "-b", "xo/t1")
        self.tree = self.project / ".xo/worktrees/t1"

    def post(self, path: str, body: dict, key: bool = True):
        return self.client.post(f"/api/xo-projects/demo/{path}", json=body,
                                headers={"X-XO-UI-Writer": self.key} if key else {})

    def test_versions_count_from_the_oldest_and_say_which_is_current(self) -> None:
        got = self.client.get("/api/xo-projects/demo/versions", params={"relative_path": "docs/a.md"}).json()
        head = got["head"]
        self.assertEqual((got["versions"], head["version"], got["more"]), ([], version_of(b"one\n"), False))
        self.assertEqual(len(head["commit"]), 40)
        res = self.client.put("/api/xo-projects/demo/file", headers={"X-XO-UI-Writer": self.key},
                              json={"relative_path": "docs/a.md", "content": "two\n", "base_version": head["version"]})
        self.tick(20)
        self.put("docs/a.md", "three\n")
        self.watched("docs/a.md")
        got = self.client.get("/api/xo-projects/demo/versions", params={"relative_path": "docs/a.md"}).json()
        self.assertEqual([(v["n"], v["by"]["kind"], v["current"]) for v in got["versions"]],
                         [(2, "disk", True), (1, "ui", False)])
        self.assertEqual(got["versions"][1]["id"], res.json()["edit_id"])
        page = self.client.get("/api/xo-projects/demo/versions",
                               params={"relative_path": "docs/a.md", "limit": 1}).json()
        self.assertTrue(page["more"])
        older = self.client.get("/api/xo-projects/demo/versions", params={
            "relative_path": "docs/a.md", "limit": 1, "before": page["versions"][0]["id"]}).json()
        self.assertEqual(([v["n"] for v in older["versions"]], older["more"]), ([1], False))
        since = self.client.get("/api/xo-projects/demo/versions",
                                params={"since": (T0 - timedelta(minutes=1)).isoformat()}).json()
        self.assertEqual([v["path"] for v in since["versions"]], ["docs/a.md", "docs/a.md"])
        recent = self.client.get("/api/xo-projects/demo/versions", params={
            "relative_path": "docs/a.md", "since": (T0 + timedelta(seconds=10)).isoformat()}).json()
        self.assertEqual([v["n"] for v in recent["versions"]], [2], "n counts from the oldest kept")

    def test_a_past_version_reads_back_read_only(self) -> None:
        head = version_of(b"one\n")
        self.client.put("/api/xo-projects/demo/file", headers={"X-XO-UI-Writer": self.key},
                        json={"relative_path": "docs/a.md", "content": "two\n", "base_version": head})
        old = self.client.get("/api/xo-projects/demo/file",
                              params={"relative_path": "docs/a.md", "version": head}).json()
        self.assertEqual((old["content"], old["version"], old["editable"], old["not_editable_reason"]),
                         ("one\n", head, False, "historical"))
        gone = self.client.get("/api/xo-projects/demo/file",
                               params={"relative_path": "docs/a.md", "version": version_of(b"never")})
        self.assertEqual((gone.status_code, gone.json()["detail"]["code"]), (404, "version_not_found"))

    def test_restore_of_a_version_is_said_so(self) -> None:
        res = self.client.put("/api/xo-projects/demo/file", json={
            "relative_path": "docs/a.md", "content": "two\n", "base_version": version_of(b"one\n")},
            headers={"X-XO-UI-Writer": self.key})
        first = res.json()["edit_id"]
        self.tick(1)
        back = self.client.put("/api/xo-projects/demo/file", json={
            "relative_path": "docs/a.md", "content": "one\n", "base_version": version_of(b"two\n")},
            headers={"X-XO-UI-Writer": self.key, "X-XO-Restore-Of": first})
        self.assertEqual(back.status_code, 200, back.text)
        newest = self.client.get("/api/xo-projects/demo/versions",
                                 params={"relative_path": "docs/a.md"}).json()["versions"][0]
        self.assertEqual((newest["kind"], newest["restores"]), ("restore", first))
        bad = self.client.put("/api/xo-projects/demo/file", json={
            "relative_path": "docs/a.md", "content": "x\n", "base_version": version_of(b"one\n")},
            headers={"X-XO-UI-Writer": self.key, "X-XO-Restore-Of": "../etc"})
        self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "invalid_restore"))

    def test_file_operations_need_the_writer_key_and_answer_with_codes(self) -> None:
        for path, body in (("files/rename", {"relative_path": "docs/a.md", "new_relative_path": "b.md"}),
                           ("files/delete", {"relative_path": "docs/a.md"}),
                           ("files/mkdir", {"relative_path": "new"})):
            with self.subTest(path=path):
                res = self.post(path, body, key=False)
                self.assertEqual((res.status_code, res.json()["detail"]["code"]), (403, "ui_only"))
        moved = self.post("files/rename", {"relative_path": "docs/a.md", "new_relative_path": "notes/b.md"})
        self.assertEqual(moved.json(), {"relative_path": "notes/b.md", "old_relative_path": "docs/a.md",
                                        "kind": "file", "edit_id": moved.json()["edit_id"],
                                        "version": version_of(b"one\n")})
        for path, body, status, code in (
                ("files/rename", {"relative_path": "nope.md", "new_relative_path": "x.md"}, 404, "not_found"),
                ("files/delete", {"relative_path": "nope.md"}, 404, "not_found"),
                ("files/rename", {"relative_path": "../x", "new_relative_path": "x.md"}, 400, "invalid_path"),
                ("files/mkdir", {"relative_path": "notes"}, 409, "exists")):
            with self.subTest(path=path, code=code):
                res = self.post(path, body)
                self.assertEqual((res.status_code, res.json()["detail"]["code"]), (status, code))
        missing = self.client.get("/api/xo-projects/nope/versions", params={"relative_path": "a.md"})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "project_not_found"))
        deleted = self.post("files/delete", {"relative_path": "notes"})
        self.assertEqual(deleted.json()["kind"], "folder")
        undo = self.client.post(f"/api/xo-projects/demo/edits/{deleted.json()['edit_id']}/undo",
                                headers={"X-XO-UI-Writer": self.key})
        self.assertEqual(undo.status_code, 200, undo.text)
        self.assertEqual((self.project / "notes/b.md").read_text(), "one\n")

    def test_paths_of_a_worktree_list_untracked_files_and_leave_ignored_ones_out(self) -> None:
        (self.tree / "fresh.md").write_text("new\n")
        (self.tree / "debug.log").write_text("ignored\n")
        (self.tree / "build").mkdir()
        (self.tree / "build" / "out.js").write_text("ignored\n")
        got = self.client.get("/api/coworker/xo-projects/demo/paths", params={"worktree": "t1"}).json()
        self.assertEqual((got["root"], got["paths"], got["dirs"], got["truncated"]),
                         (".xo/worktrees/t1", [".gitignore", "docs/a.md", "fresh.md", "js/app.js"], ["docs", "js"],
                          False))
        same = self.client.get("/api/coworker/xo-projects/demo/paths", params={"worktree": "t1", "stamp": got["stamp"]})
        self.assertEqual(same.json(), {"unchanged": True, "stamp": got["stamp"]})
        project = self.client.get("/api/coworker/xo-projects/demo/paths").json()
        self.assertEqual((project["root"], project["paths"]), ("", [".gitignore", "docs/a.md", "js/app.js"]))
        self.assertFalse([p for p in project["paths"] + got["paths"] if p.startswith(".xo/")],
                         "XO's own records are never offered")
        nope = self.client.get("/api/coworker/xo-projects/demo/paths", params={"worktree": "nope"})
        self.assertEqual((nope.status_code, nope.json()["detail"]["code"]), (404, "not_found"))

    def test_paths_outside_git_walk_without_the_noise(self) -> None:
        plain = self.projects / "plain"
        (plain / "src").mkdir(parents=True)
        (plain / "src" / "a.py").write_text("a")
        (plain / "node_modules" / "x").mkdir(parents=True)
        (plain / "node_modules" / "x" / "i.js").write_text("x")
        got = self.client.get("/api/coworker/xo-projects/plain/paths").json()
        self.assertEqual(got["paths"], ["src/a.py"])

    def test_search(self) -> None:
        def search(**params):
            return self.client.get("/api/coworker/xo-projects/demo/search", params=params)
        got = search(q="answer").json()
        self.assertEqual([(m["path"], m["line"], m["col"], m["ranges"]) for m in got["matches"]],
                         [("js/app.js", 1, 7, [[6, 12]]), ("js/app.js", 2, 7, [[6, 12]])])
        self.assertEqual((got["root"], got["files"], got["truncated"]), ("", 1, False),
                         "XO's .xo/project.json holds 'answer' too, and isn't searched")
        self.assertEqual(len(search(q="answer", case=True).json()["matches"]), 1)
        self.assertEqual(search(q="Answ", word=True).json()["matches"], [])
        self.assertEqual(search(q=r"answer\s=\s\d+", regex=True).json()["matches"][0]["ranges"], [[6, 17]])
        self.assertEqual(search(q="one", glob="*.js").json()["matches"], [])
        self.assertEqual([m["path"] for m in search(q="one", glob="*.md").json()["matches"]], ["docs/a.md"])
        (self.tree / "only-here.md").write_text("answer in a worktree\n")
        self.assertEqual([m["path"] for m in search(q="answer", worktree="t1").json()["matches"]][-1],
                         "only-here.md")
        for params, code in (({"q": "("}, None), ({"q": "(", "regex": True}, "bad_regex"), ({"q": " "}, "query_empty"),
                             ({"q": "a\nb"}, "invalid_query")):
            with self.subTest(params=params):
                res = search(**params)
                if code is None:
                    self.assertEqual(res.status_code, 200, "a fixed string is never a regex")
                else:
                    self.assertEqual((res.status_code, res.json()["detail"]["code"]), (400, code))
        with mock.patch.object(finder, "SEARCH_BUDGET_S", 0):
            self.assertTrue(search(q="answer").json()["truncated"], "the time budget cuts it short")
        self.assertTrue(search(q="answer", limit=1).json()["truncated"])

    def test_a_long_line_is_cut_around_its_match(self) -> None:
        self.put("long.txt", "x" * 1000 + "needle" + "y" * 1000 + "\n")
        sh(self.project, "add", "long.txt")
        (m,) = self.client.get("/api/coworker/xo-projects/demo/search", params={"q": "needle"}).json()["matches"]
        self.assertEqual(len(m["text"]), finder.TEXT_MAX)
        a, b = m["ranges"][0]
        self.assertEqual((m["text"][a:b], m["col"]), ("needle", 1001))

    def test_a_backtracking_regex_never_holds_up_the_server(self) -> None:
        # (a*)*c takes Python minutes on a line of a's; git's PCRE finds the lines, the child is killed at the budget.
        self.put("slow.txt", "c " + "a" * 40 + "\n")
        sh(self.project, "add", "slow.txt")
        plain = self.projects / "plain"
        plain.mkdir()
        (plain / "slow.txt").write_text("a" * 40 + "\n")
        for project in ("demo", "plain"):
            with self.subTest(project=project), mock.patch.object(finder, "SEARCH_BUDGET_S", 1.0):
                began = time.monotonic()
                got = self.client.get(f"/api/coworker/xo-projects/{project}/search",
                                      params={"q": "(a*)*c", "regex": True}).json()
                self.assertLess(time.monotonic() - began, 4.0)
                self.assertTrue(got["truncated"])

    def test_a_regex_outside_git_is_found_in_the_child(self) -> None:
        plain = self.projects / "plain"
        (plain / "src").mkdir(parents=True)
        (plain / "src" / "a.py").write_text("x = 1\nanswer = 42\n")
        got = self.client.get("/api/coworker/xo-projects/plain/search", params={"q": r"answer\s=\s\d+", "regex": True})
        self.assertEqual([(m["path"], m["line"], m["ranges"]) for m in got.json()["matches"]], [("src/a.py", 2, [[0, 11]])])


# ── storm's side ──────────────────────────────────────────────────────────────


class BridgeTests(Case, unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        from services.cowork_agent.storm.file_versions import FileVersions, _Turn

        bridge = FileVersions()
        project = str(self.root)
        tree = str(self.root / ".xo/worktrees/t1")
        bridge._running = {
            "coord": _Turn("coord", "s-coord", "chat1", None, project, T0.isoformat()),
            "thread": _Turn("thread", "s-thread", "chat1", "1.1", tree, T0.isoformat()),
        }
        return bridge

    async def test_a_finished_turn_is_never_learned_back(self) -> None:
        from services.cowork_agent.storm.file_versions import _Turn
        from services.cowork_agent.storm.store import StoredEvent

        bridge = self.bridge()
        stale = {"thread": _Turn("thread", "s-thread", "chat1", "1.1", str(self.root), T0.isoformat())}

        async def read_running(_turn_id):
            await asyncio.sleep(0)
            return stale
        bridge._read_running = read_running
        learning = asyncio.ensure_future(bridge._learn("thread"))
        bridge.committed([StoredEvent(2, T0.isoformat(), "turn.finished", {}, "chat1", "s-thread", "1.1", "thread")])
        await learning
        self.assertNotIn("thread", bridge._running)

    async def test_the_deepest_running_folder_is_the_writer(self) -> None:
        bridge = self.bridge()
        self.assertEqual(bridge.attributor(self.root / ".xo/worktrees/t1/a.md")["session_id"], "s-thread")
        self.assertEqual(bridge.attributor(self.root / "a.md")["session_id"], "s-coord")
        self.assertIsNone(bridge.attributor(self.base / "elsewhere.md"))
        self.assertTrue(bridge.busy("demo"))
        self.assertFalse(bridge.busy("other"))

    async def test_events_land_on_the_project_s_chat(self) -> None:
        bridge = self.bridge()
        appended: list[tuple] = []

        class C:
            def execute(self, sql, args):
                class R:
                    def fetchone(_self):
                        return ("chat_p",) if args == ("demo",) else None
                return R()

        class W:
            def append(self, type_, data, chat_id=None):
                appended.append((type_, data, chat_id))

        class S:
            async def read(self, fn):
                return fn(C())

        class E:
            store = S()

            async def write(self, fn):
                return fn(W())
        bridge.engine = E()
        made = {"fresh": "chat_new"}

        async def project_chat(name):
            return made.get(name)
        with mock.patch("services.cowork_agent.storm.file_versions._project_chat", project_chat):
            await bridge.deliver([("file.version", "demo", {"id": "v_1"}), ("file.rescan", "fresh", {"x": 1}),
                                  ("file.rescan", "nochat", {"x": 2})])
        self.assertEqual(appended, [("file.version", {"id": "v_1"}, "chat_p"),
                                    ("file.rescan", {"x": 1}, "chat_new")],
                         "a project without a chat gets one; one that can't have one is skipped")

    async def test_turn_end_attributes_what_the_turn_wrote(self) -> None:
        from services.cowork_agent.storm.file_versions import FileVersions
        from services.cowork_agent.storm.store import StoredEvent

        self.put("a.md", "agent\n")
        bridge = FileVersions()

        class S:
            async def read(self, fn):
                return (T0.isoformat(),)

        class E:
            store = S()
        bridge.engine = E()
        event = StoredEvent(1, T0.isoformat(), "turn.finished", {}, "chat1", "s1", "1.1", "t1", None, 1)
        with mock.patch("services.cowork_agent.storm.file_versions.SETTLE_S", 0):
            await bridge.after_turn(event, [(self.project / "a.md", "create_file")], "m1")
        (record,) = self.records("a.md")
        self.assertEqual((record.kind, record.by), ("create", edits.writer(
            edits.AGENT, session_id="s1", thread_ts="1.1", turn_id="t1", message_id="m1")))


if __name__ == "__main__":
    unittest.main()
