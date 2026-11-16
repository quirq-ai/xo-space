"""Space's project files (services/project_files): the places a save may
change, reading with a version, the guarded save, undo, the edit log, and
what an agent engine reads from it. Real files in a temp projects root."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.project_files import edits, read, save, writer_key
from services.project_files.address import (
    EDIT_MAX_BYTES,
    FileRefused,
    Place,
    Target,
    classify,
    locate,
    parent_dir,
    version_of,
)

BOM = b"\xef\xbb\xbf"


class ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.projects = self.base / "projects"
        self.project = self.projects / "demo"
        self.project.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        edits._pruned_at.clear()

    def put(self, rel: str, data: bytes | str) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def refused(self, code: str, fn, *args, **kwargs) -> FileRefused:
        with self.assertRaises(FileRefused) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception


class PlaceTests(ProjectCase):
    def test_every_place_by_its_path(self) -> None:
        cases = {
            "src/app.py": Place.ORDINARY,
            ".env": Place.ORDINARY,
            ".gitignore": Place.ORDINARY,
            ".github/workflows/ci.yml": Place.ORDINARY,
            ".git/config": Place.RUNS_CODE,
            ".git/hooks/pre-commit": Place.RUNS_CODE,
            "vendor/lib/.git/config": Place.RUNS_CODE,
            ".claude/settings.json": Place.RUNS_CODE,
            ".mcp.json": Place.RUNS_CODE,
            ".codex/config.toml": Place.RUNS_CODE,
            ".xo/project.json": Place.READ_ONLY,
            ".xo/todos.json": Place.READ_ONLY,
            ".xo/made/t1/report.pdf": Place.READ_ONLY,
            ".xo/attachments/a.png": Place.READ_ONLY,
            ".xo/worktrees/t1/.git": Place.READ_ONLY,
            ".xo/worktrees/t1/src/app.py": Place.ORDINARY,
            ".xo/worktrees/t1/.claude/settings.local.json": Place.RUNS_CODE,
            ".xo/worktrees/t1/.mcp.json": Place.RUNS_CODE,
            # Without case: a case-insensitive disk (macOS's default) opens .git/ for .GIT/.
            ".GIT/hooks/pre-commit": Place.RUNS_CODE,
            ".Claude/settings.json": Place.RUNS_CODE,
            ".XO/project.json": Place.READ_ONLY,
            ".xo/Worktrees/t1/.Git/config": Place.RUNS_CODE,
        }
        for rel, place in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(classify(rel), place)

    def test_a_link_is_judged_at_both_ends_and_never_leaves_the_project(self) -> None:
        self.put(".git/hooks/pre-commit", "#!/bin/sh\n")
        (self.project / "innocent.sh").symlink_to(self.project / ".git/hooks/pre-commit")
        self.assertEqual(locate("demo", "innocent.sh").place, Place.RUNS_CODE)
        outside = self.base / "secret.txt"
        outside.write_text("s")
        (self.project / "out.txt").symlink_to(outside)
        self.refused("outside_project", locate, "demo", "out.txt")
        for bad in ("../x", "/etc/passwd", "a/../../x", "", "a\x00b", "a//b"):
            with self.subTest(bad=bad):
                self.refused("invalid_relative_path", locate, "demo", bad)
        self.refused("project_not_found", locate, "nope", "a.txt")

    def test_a_folder_that_became_a_link_is_never_written_through(self) -> None:
        (self.project / "real").mkdir()
        (self.project / "swapped").symlink_to(self.project / "real")
        target = Target("demo", self.project.resolve(), "swapped/a.txt", "swapped/a.txt", Place.ORDINARY)
        with self.assertRaises(FileRefused) as caught:
            with parent_dir(target):
                pass
        self.assertEqual(caught.exception.code, "place_changed")


class ReadTests(ProjectCase):
    def test_text_comes_with_its_version_and_what_the_editor_may_do(self) -> None:
        data = b"one\r\ntwo\r\n"
        self.put("notes.txt", data)
        got = read.read_text("demo", "notes.txt")
        self.assertEqual((got.content, got.version, got.editable, got.place, got.line_endings, got.bom),
                         ("one\r\ntwo\r\n", version_of(data), True, "ordinary", "crlf", False))

    def test_text_is_judged_by_content_not_by_name(self) -> None:
        self.put("Makefile", "all:\n\techo hi\n")
        self.put(".env", "KEY=1\n")
        self.assertTrue(read.read_text("demo", "Makefile").editable)
        self.assertEqual(read.read_text("demo", ".env").content, "KEY=1\n")
        self.put("blob.dat", b"\x00\x01\x02binary")
        self.refused("preview_unsupported", read.read_text, "demo", "blob.dat")

    def test_a_bom_is_reported_and_left_out(self) -> None:
        self.put("bom.md", BOM + b"# Title\n")
        got = read.read_text("demo", "bom.md")
        self.assertEqual((got.content, got.bom, got.version), ("# Title\n", True, version_of(BOM + b"# Title\n")))

    def test_a_preview_is_cut_and_says_so_and_editing_reads_it_whole(self) -> None:
        data = b"x" * (read.PREVIEW_MAX_BYTES + 10)
        self.put("big.log", data)
        preview = read.read_text("demo", "big.log")
        self.assertEqual((preview.truncated, preview.editable, preview.not_editable_reason, preview.version),
                         (True, False, "truncated", None), "a cut copy has no version to save from")
        whole = read.read_text("demo", "big.log", purpose="edit")
        self.assertEqual((whole.truncated, whole.editable, len(whole.content)), (False, True, len(data)))
        self.put("huge.log", b"y" * (EDIT_MAX_BYTES + 1))
        self.refused("too_large_to_edit", read.read_text, "demo", "huge.log", purpose="edit")
        huge = read.read_text("demo", "huge.log")
        self.assertEqual((huge.version, huge.not_editable_reason), (None, "too_large"))

    def test_what_isnt_utf8_is_shown_but_not_editable(self) -> None:
        self.put("latin.txt", "café\n".encode("latin-1"))
        got = read.read_text("demo", "latin.txt")
        self.assertEqual((got.editable, got.not_editable_reason), (False, "not_utf8"))

    def test_xo_records_read_but_are_not_editable(self) -> None:
        self.put(".xo/project.json", "{}\n")
        got = read.read_text("demo", ".xo/project.json")
        self.assertEqual((got.place, got.editable, got.not_editable_reason), ("read_only", False, "read_only_place"))


class SaveTests(ProjectCase):
    def test_a_save_replaces_the_file_keeping_its_mode_and_logs_it(self) -> None:
        path = self.put("run.sh", "echo a\n")
        path.chmod(0o750)
        before = version_of(b"echo a\n")
        done = save.save("demo", "run.sh", "echo b\n", before)
        self.assertEqual(path.read_bytes(), b"echo b\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o750)
        self.assertEqual((done.version, done.unchanged), (version_of(b"echo b\n"), False))
        (edit,) = edits.entries("demo")
        self.assertEqual((edit.id, edit.kind, edit.from_version, edit.to_version, edit.path),
                         (done.edit_id, "save", before, done.version, "run.sh"))
        self.assertEqual(edits.kept_version("demo", before), b"echo a\n")
        self.assertEqual([p.name for p in self.project.iterdir()], ["run.sh"], "no temp file left behind")

    def test_a_stale_save_is_refused_with_what_is_there_now(self) -> None:
        self.put("a.md", "agent's newer\n")
        refused = self.refused("file_changed", save.save, "demo", "a.md", "mine\n", version_of(b"older\n"))
        self.assertEqual(refused.status, 409)
        self.assertEqual(refused.extra["current"]["content"], "agent's newer\n")
        self.assertEqual((self.project / "a.md").read_text(), "agent's newer\n")
        (self.project / "a.md").unlink()
        gone = self.refused("file_changed", save.save, "demo", "a.md", "mine\n", version_of(b"agent's newer\n"))
        self.assertIsNone(gone.extra["current"])

    def test_create_needs_the_name_free_and_the_folder_there(self) -> None:
        (self.project / "docs").mkdir()
        done = save.save("demo", "docs/new.md", "hi\n", None)
        self.assertEqual((self.project / "docs/new.md").read_text(), "hi\n")
        self.assertEqual(edits.find("demo", done.edit_id).kind, "create")
        self.refused("file_exists", save.save, "demo", "docs/new.md", "again\n", None)
        self.refused("folder_not_found", save.save, "demo", "nowhere/new.md", "x", None)

    def test_the_places_decide(self) -> None:
        self.put(".xo/project.json", "{}\n")
        self.refused("read_only_place", save.save, "demo", ".xo/project.json", "{}", version_of(b"{}\n"))
        self.put(".claude/settings.json", "{}\n")
        refused = self.refused("confirm_required", save.save, "demo", ".claude/settings.json", '{"a":1}\n',
                               version_of(b"{}\n"))
        self.assertEqual((refused.status, refused.extra), (428, {"reason": "runs_code"}))
        done = save.save("demo", ".claude/settings.json", '{"a":1}\n', version_of(b"{}\n"), confirm="runs_code")
        self.assertEqual(edits.find("demo", done.edit_id).confirmed, "runs_code")
        self.put(".env", "A=1\n")
        save.save("demo", ".env", "A=2\n", version_of(b"A=1\n"))
        self.assertEqual((self.project / ".env").read_text(), "A=2\n")

    def test_a_bom_is_kept_and_an_unchanged_save_writes_nothing(self) -> None:
        self.put("bom.txt", BOM + b"old\n")
        save.save("demo", "bom.txt", "new\n", version_of(BOM + b"old\n"))
        self.assertEqual((self.project / "bom.txt").read_bytes(), BOM + b"new\n")
        same = save.save("demo", "bom.txt", "new\n", version_of(BOM + b"new\n"))
        self.assertEqual((same.unchanged, same.edit_id), (True, None))
        self.assertEqual(len(edits.entries("demo")), 1)

    def test_what_cant_be_text_or_is_too_big_is_refused(self) -> None:
        self.put("latin.txt", "café".encode("latin-1"))
        self.refused("not_text", save.save, "demo", "latin.txt", "x", version_of("café".encode("latin-1")))
        self.put("ok.txt", "a")
        refused = self.refused("too_large", save.save, "demo", "ok.txt", "x" * (EDIT_MAX_BYTES + 1), version_of(b"a"))
        self.assertEqual(refused.status, 413)
        self.refused("invalid_content", save.save, "demo", "ok.txt", "\ud800", version_of(b"a"))


class UndoTests(ProjectCase):
    def test_undo_puts_back_the_earlier_version_and_can_itself_be_undone(self) -> None:
        self.put("a.txt", "one\r\n")
        first = save.save("demo", "a.txt", "two\r\n", version_of(b"one\r\n"))
        undone = save.undo("demo", first.edit_id)
        self.assertEqual((self.project / "a.txt").read_bytes(), b"one\r\n")
        self.assertEqual(edits.find("demo", undone.edit_id).undoes, first.edit_id)
        save.undo("demo", undone.edit_id)
        self.assertEqual((self.project / "a.txt").read_bytes(), b"two\r\n")

    def test_undo_refuses_when_the_file_moved_on_or_the_version_is_gone(self) -> None:
        self.put("a.txt", "one\n")
        first = save.save("demo", "a.txt", "two\n", version_of(b"one\n"))
        self.put("a.txt", "agent wrote\n")
        self.refused("file_changed", save.undo, "demo", first.edit_id)
        second = save.save("demo", "a.txt", "three\n", version_of(b"agent wrote\n"))
        edits._blob_path(edits.state_dir("demo"), version_of(b"agent wrote\n")).unlink()
        self.assertEqual(self.refused("not_undoable", save.undo, "demo", second.edit_id).status, 410)
        self.refused("edit_not_found", save.undo, "demo", "e_missing")

    def test_undoing_a_create_removes_the_file(self) -> None:
        made = save.save("demo", "new.txt", "x\n", None)
        save.undo("demo", made.edit_id)
        self.assertFalse((self.project / "new.txt").exists())


class EditLogTests(ProjectCase):
    def test_listing_says_whether_each_edit_still_stands(self) -> None:
        self.put("a.txt", "1")
        first = save.save("demo", "a.txt", "2", version_of(b"1"))
        second = save.save("demo", "a.txt", "3", version_of(b"2"))
        items = edits.listing("demo", self.project.resolve(), path="a.txt")
        self.assertEqual([(i["id"], i["still_current"], i["undoable"]) for i in items],
                         [(second.edit_id, True, True), (first.edit_id, False, False)])

    def test_edits_since_names_a_folder_s_own_files_relative_to_it(self) -> None:
        self.put(".xo/worktrees/t1/src/a.py", "a\n")
        self.put("README.md", "r\n")
        start = datetime.now(timezone.utc) - timedelta(seconds=1)
        save.save("demo", ".xo/worktrees/t1/src/a.py", "b\n", version_of(b"a\n"))
        save.save("demo", ".xo/worktrees/t1/src/a.py", "c\n", version_of(b"b\n"))
        save.save("demo", "README.md", "s\n", version_of(b"r\n"))
        (tree,) = edits.edits_since(self.project / ".xo/worktrees/t1", start)
        self.assertEqual((tree["path"], tree["edits"], tree["exists"]), ("src/a.py", 2, True))
        (own,) = edits.edits_since(self.project, start)
        self.assertEqual(own["path"], "README.md", "a project folder leaves its worktrees out")
        self.assertEqual(edits.edits_since(self.project, datetime.now(timezone.utc) + timedelta(seconds=5)), [])
        self.assertEqual(edits.edits_since(self.base, start), [])

    def test_written_by_person_holds_until_anything_else_writes_the_file(self) -> None:
        path = self.put(".claude/settings.json", "{}\n")
        self.assertFalse(edits.written_by_person(path))
        save.save("demo", ".claude/settings.json", '{"x":1}\n', version_of(b"{}\n"), confirm="runs_code")
        self.assertTrue(edits.written_by_person(path))
        path.write_text('{"x":2}\n')
        self.assertFalse(edits.written_by_person(path))

    def test_only_a_confirmed_save_of_that_very_path_counts(self) -> None:
        self.put("notes/x.json", "{}\n")
        save.save("demo", "notes/x.json", '{"hooks": 1}\n', version_of(b"{}\n"))       # ordinary: no confirm
        self.assertFalse(edits.written_by_person(self.project / "notes/x.json"), "not confirmed")
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.symlink_to(self.project / "notes/x.json")
        self.assertFalse(edits.written_by_person(settings), "a link to the person's file isn't the person's")
        settings.unlink()
        settings.write_text("{}\n")
        (self.project / "cfg").symlink_to(self.project / ".claude", target_is_directory=True)
        save.save("demo", "cfg/settings.json", '{"a": 1}\n', version_of(b"{}\n"), confirm="runs_code")
        self.assertFalse(edits.written_by_person(settings), "saved through a link, not under this name")

    def test_credential_files_keep_no_versions_on_disk(self) -> None:
        self.put(".env", "TOKEN=old\n")
        done = save.save("demo", ".env", "TOKEN=new\n", version_of(b"TOKEN=old\n"))
        blobs = edits.state_dir("demo") / "blobs"
        self.assertFalse(blobs.exists() and any(blobs.iterdir()), "a secret's versions reached the disk")
        save.undo("demo", done.edit_id)                  # right after: still possible, from memory
        self.assertEqual((self.project / ".env").read_text(), "TOKEN=old\n")
        self.assertFalse(blobs.exists() and any(blobs.iterdir()))
        with mock.patch.object(edits, "_volatile", {}):
            again = save.save("demo", ".env", "TOKEN=x\n", version_of(b"TOKEN=old\n"))
            edits._volatile.clear()
            self.assertEqual(self.refused("not_undoable", save.undo, "demo", again.edit_id).status, 410)
        for name, secret in ((".env", True), (".env.local", True), (".env.example", False), ("server.pem", True),
                             ("tls.key", True), ("id_ed25519", True), ("id_ed25519.pub", False), ("README.md", False)):
            with self.subTest(name=name):
                self.assertEqual(edits.is_secret_name(name), secret)

    def test_prune_drops_old_edits_and_the_versions_only_they_kept(self) -> None:
        self.put("a.txt", "1")
        save.save("demo", "a.txt", "2", version_of(b"1"))
        folder = edits.state_dir("demo")
        edits.prune(folder, now=datetime.now(timezone.utc) + timedelta(days=edits.RETENTION_DAYS + 1))
        self.assertEqual(edits.entries("demo"), [])
        self.assertEqual(list((folder / "blobs").iterdir()), [])


class WriterKeyTests(ProjectCase):
    def test_the_key_is_made_private_kept_and_remade_when_unsafe(self) -> None:
        path = writer_key.ensure()
        self.assertEqual(path, self.base / "state" / "secrets" / "ui-writer.key")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        key = writer_key.current()
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(writer_key.ensure(), path)
        self.assertEqual(writer_key.current(), key, "kept across restarts")
        self.assertTrue(writer_key.matches(key + "\n"))
        self.assertFalse(writer_key.matches("0" * 64))
        self.assertFalse(writer_key.matches(None))
        path.chmod(0o644)
        self.assertIsNone(writer_key.current(), "a key others can read is no key")
        writer_key.ensure()
        self.assertNotEqual(writer_key.current(), key)


if __name__ == "__main__":
    unittest.main()
