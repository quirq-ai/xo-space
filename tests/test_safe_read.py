"""State files are read whole only when they are small regular files.

Live test D4: with scheduler/jobs.json -> /dev/zero the server allocated about
1 GB per watcher tick until MemoryError.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import safe_read
from utils.commands import scheduler
from utils.safe_read import FileTooLarge, NotARegularFile, read_text_guarded


class ReadTextGuardedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_a_regular_file_and_a_link_to_one_read_as_before(self) -> None:
        target = self.dir / "a.json"
        target.write_text('{"ok": true}', encoding="utf-8")
        link = self.dir / "link.json"
        link.symlink_to(target)
        self.assertEqual(read_text_guarded(target), '{"ok": true}')
        self.assertEqual(read_text_guarded(link), '{"ok": true}')

    def test_a_missing_file_is_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_text_guarded(self.dir / "absent.json")

    def test_a_device_is_refused_without_reading(self) -> None:
        link = self.dir / "jobs.json"
        link.symlink_to("/dev/zero")
        with self.assertRaises(NotARegularFile) as caught:
            read_text_guarded(link)
        self.assertIsInstance(caught.exception, OSError)

    def test_a_fifo_is_refused_instead_of_waiting_forever(self) -> None:
        fifo = self.dir / "inbox.json"
        os.mkfifo(fifo)
        outcome: list = []
        worker = threading.Thread(target=lambda: outcome.append(self._attempt(fifo)), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "reading a FIFO blocked")
        self.assertIs(outcome[0], NotARegularFile)

    @staticmethod
    def _attempt(path: Path):
        try:
            read_text_guarded(path)
        except Exception as exc:  # noqa: BLE001
            return type(exc)
        return None

    def test_a_folder_is_refused(self) -> None:
        with self.assertRaises(NotARegularFile):
            read_text_guarded(self.dir)

    def test_size_is_capped_unless_the_caller_reads_a_log(self) -> None:
        big = self.dir / "big.json"
        big.write_bytes(b"x" * 101)
        with self.assertRaises(FileTooLarge):
            read_text_guarded(big, max_bytes=100)
        self.assertEqual(len(read_text_guarded(big, max_bytes=None)), 101)
        self.assertEqual(safe_read.MAX_STATE_FILE_BYTES, 50 * 1024 * 1024)


class SchedulerRefusesDevicesTests(unittest.TestCase):
    def test_jobs_json_linked_to_dev_zero_is_a_scheduler_error_not_a_memory_blowup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.json"
            jobs.symlink_to("/dev/zero")
            with patch.object(scheduler, "jobs_file", return_value=jobs):
                with self.assertRaises(scheduler.SchedulerError) as caught:
                    scheduler.list_jobs()
            self.assertIn("not readable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
