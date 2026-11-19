"""The shared command log writes an unchanged output once per file, and
``scripts/command_log_expand.py`` puts every one back: expanding the log is,
byte for byte, the log written with every output in full."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import utils.commands as commands
from utils.commands import CommandResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from command_log_expand import MARKER, expand  # noqa: E402


def result(output: str, code: int = 0) -> CommandResult:
    return CommandResult(argv=["git", "log"], returncode=code, output=output, duration_seconds=0.01)


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_root = Path(self._tmp.name) / ".quirq"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.state_root)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("QUIRQ_COMMAND_LOG", "QUIRQ_COMMAND_LOG_PATH", "QUIRQ_COMMAND_LOG_ARCHIVE",
                    "QUIRQ_COMMAND_LOG_DEDUPE"):
            os.environ.pop(key, None)
        commands._LAST_OUTPUTS.clear()
        self.addCleanup(commands._LAST_OUTPUTS.clear)
        self.log = self.state_root / "inbox" / "activity" / "commands.log"
        self.n = 0

    def emit(self, output: str, *, code: int = 0, cwd: str = "/repo", argv=("git", "log")) -> None:
        self.n += 1
        commands._emit_logs(ts=f"2026-11-19T00:00:{self.n:02d}+00:00", label="", argv=list(argv), cwd=cwd,
                            result=result(output, code), log_path=None)

    def run_sequence(self) -> None:
        for output, code, cwd in (("a\nb\n", 0, "/repo"), ("a\nb\n", 0, "/repo"), ("a\nb\n", 0, "/other"),
                                  ("a\nb\n", 0, "/repo"), ("a\nb\nc\n", 0, "/repo"), ("a\nb\nc\n", 0, "/repo"),
                                  ("a\nb\n", 0, "/repo"), ("boom\n", 1, "/repo"), ("boom\n", 1, "/repo"),
                                  ("a\nb\n", 0, "/repo")):
            self.emit(output, code=code, cwd=cwd)

    def written_in_full(self) -> bytes:
        """The same sequence, every output in full (QUIRQ_COMMAND_LOG_DEDUPE=off)."""
        commands._LAST_OUTPUTS.clear()
        full = self.log.with_name("full.log")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(full), "QUIRQ_COMMAND_LOG_DEDUPE": "off"}):
            self.n = 0
            self.run_sequence()
        return full.read_bytes()

    def test_an_unchanged_output_is_written_once_and_expands_back_byte_for_byte(self) -> None:
        self.run_sequence()
        deduped = self.log.read_bytes()
        refs = [m for m in MARKER.finditer(deduped) if int(m.group(3)) == m.start()]
        # repeats of the same command, folder and output (exit 0): entries 2, 4, 6, 10
        self.assertEqual(len(refs), 4)
        self.assertEqual(deduped.count(b"boom\n"), 2)            # a failure always writes its output
        expanded, problems = expand(deduped)
        self.assertEqual(problems, [])
        self.assertEqual(expanded, self.written_in_full())

    def test_a_rotated_file_holds_every_output_it_refers_to(self) -> None:
        with patch.object(commands, "_COMMAND_LOG_MAX_BYTES", 300):
            for _ in range(12):
                self.emit("same output, line one\nline two\n")
        files = [*sorted((self.log.parent / "archive").iterdir()), self.log]
        self.assertGreater(len(files), 2)
        for path in files:
            data = path.read_bytes()
            expanded, problems = expand(data)
            self.assertEqual(problems, [], path)
            self.assertNotIn(b"output unchanged", expanded, path)
            self.assertTrue(data.count(b"same output") >= 1, path)   # the first entry of each file is full

    def test_a_copy_of_a_reference_inside_an_output_is_left_as_it_is(self) -> None:
        self.emit("x\n")
        self.emit("x\n")
        log_text = self.log.read_text(encoding="utf-8")
        self.emit(log_text, argv=("tail", "commands.log"))           # an agent reads the log
        self.emit(log_text, argv=("tail", "commands.log"))
        expanded, problems = expand(self.log.read_bytes())
        self.assertEqual(problems, [])
        self.assertEqual(expanded, self.full_of(lambda: (self.emit("x\n"), self.emit("x\n"),
                                                         self.emit(log_text, argv=("tail", "commands.log")),
                                                         self.emit(log_text, argv=("tail", "commands.log")))))

    def full_of(self, run) -> bytes:
        commands._LAST_OUTPUTS.clear()
        full = self.log.with_name("full2.log")
        with patch.dict(os.environ, {"QUIRQ_COMMAND_LOG_PATH": str(full), "QUIRQ_COMMAND_LOG_DEDUPE": "off"}):
            self.n = 0
            run()
        return full.read_bytes()

    def test_another_writer_between_two_runs_never_breaks_a_reference(self) -> None:
        """A second process appends to the same file (its own memory): the
        reference still names this process's output, found by its digest."""
        self.emit("mine\n")
        with open(self.log, "ab") as f:                              # the other process's entry, same command
            f.write(b"\n=== 2026-11-19T00:00:50+00:00 ===\n$ git log\ncwd: /repo\n[0; 0.010s]\ntheirs\n")
        self.emit("mine\n")
        data = self.log.read_bytes()
        self.assertEqual(sum(1 for m in MARKER.finditer(data) if int(m.group(3)) == m.start()), 1)
        expanded, problems = expand(data)
        self.assertEqual(problems, [])
        self.assertEqual(expanded.count(b"mine\n"), 2)

    def test_dedupe_off_or_without_the_archive_writes_every_output(self) -> None:
        for env in ({"QUIRQ_COMMAND_LOG_DEDUPE": "off"}, {"QUIRQ_COMMAND_LOG_ARCHIVE": "false"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                commands._LAST_OUTPUTS.clear()
                self.log.unlink(missing_ok=True)
                self.emit("same\n")
                self.emit("same\n")
                self.assertNotIn(b"output unchanged", self.log.read_bytes())
                self.assertEqual(self.log.read_bytes().count(b"same\n"), 2)

    def test_a_new_file_at_the_same_path_starts_over(self) -> None:
        self.emit("same\n")
        self.log.rename(self.log.with_name("moved.log"))             # rotated by someone else
        self.emit("same\n")
        self.assertNotIn(b"output unchanged", self.log.read_bytes())


if __name__ == "__main__":
    unittest.main()
