"""xo-doctor reads a file into exactly one outcome and never returns its content."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.doctor import model, reading
from services.doctor.reading import ReadResult, Tree, classify, measure_tree, read_tail, readable_dir

ONE = frozenset({1})


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.later = time.time() + 3600  # every file written here is an hour old

    def write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_absent(self) -> None:
        self.assertEqual(classify(self.dir / "nope.json", now=self.later, accepted=ONE).outcome, "absent")

    def test_empty(self) -> None:
        path = self.write("a.json", b"  \n")
        self.assertEqual(classify(path, now=self.later, accepted=ONE).outcome, "empty")

    def test_invalid_json_and_not_utf8(self) -> None:
        bad = self.write("a.json", b'{"token": "SECRET-VALUE",')
        result = classify(bad, now=self.later, accepted=ONE)
        self.assertEqual(result.outcome, "invalid_json")
        self.assertNotIn("SECRET-VALUE", result.detail)
        latin = self.write("b.json", b'{"a": "\xff"}')
        self.assertEqual(classify(latin, now=self.later, accepted=ONE).outcome, "invalid_json")

    def test_a_bad_file_written_just_now_is_not_judged(self) -> None:
        path = self.write("a.json", b"")
        self.assertEqual(classify(path, now=time.time(), accepted=ONE).outcome, "recent")

    def test_a_good_file_written_just_now_is_ok(self) -> None:
        path = self.write("a.json", b'{"schema": 1}')
        self.assertEqual(classify(path, now=time.time(), accepted=ONE).outcome, "ok")

    def test_a_bad_file_dated_in_the_future_is_still_judged(self) -> None:
        path = self.write("a.json", b"{not json")
        os.utime(path, (self.later + 86400, self.later + 86400))
        self.assertEqual(classify(path, now=self.later, accepted=ONE).outcome, "invalid_json")

    def classify_within(self, path: Path, seconds: float = 5) -> ReadResult:
        """classify() in a daemon thread, so a read that blocks fails the test
        instead of hanging the suite."""
        box: list = []
        worker = threading.Thread(target=lambda: box.append(classify(path, now=self.later, accepted=ONE)), daemon=True)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            self.fail(f"classify() was still reading {path.name} after {seconds}s")
        return box[0]

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_pipe_is_never_opened(self) -> None:
        # Opening a pipe for reading waits for a writer that never comes.
        path = self.dir / "a.json"
        os.mkfifo(path)
        self.assertEqual(self.classify_within(path).outcome, "special")

    @unittest.skipUnless(os.path.exists("/dev/null"), "needs /dev/null")
    def test_a_link_to_a_device_is_never_opened(self) -> None:
        # /dev/null, not /dev/zero: both are character devices, so the
        # guard is exercised the same way, but if it ever regresses this
        # reads nothing instead of reading without end and exhausting the
        # machine's memory (which a /dev/zero version of this test did).
        path = self.dir / "a.json"
        path.symlink_to("/dev/null")
        self.assertEqual(self.classify_within(path).outcome, "special")

    def test_a_file_over_the_read_limit_is_not_read(self) -> None:
        path = self.write("a.json", b'{"schema": 1, "pad": "' + b"a" * 200 + b'"}')
        with patch.object(reading, "MAX_READ_BYTES", 64):
            result = classify(path, now=self.later, accepted=ONE)
        self.assertEqual((result.outcome, result.value), ("file_too_large", None))

    def test_a_read_never_asks_for_more_than_one_chunk_at_a_time(self) -> None:
        # One read(MAX_READ_BYTES + 1) reserves the whole 50 MB up front, even
        # for a 12-byte file; under strict memory overcommit that fails with a
        # MemoryError, which is not an OSError and would escape classify().
        path = self.write("a.json", b'{"schema": 1}')
        with patch.object(reading.os, "read", wraps=os.read) as spy:
            result = classify(path, now=self.later, accepted=ONE)
        self.assertEqual(result.outcome, "ok")
        self.assertTrue(spy.called, "classify() must read through os.read in chunks")
        self.assertLessEqual(max(call.args[1] for call in spy.call_args_list), reading.READ_CHUNK_BYTES)

    def test_a_file_at_the_read_limit_is_read(self) -> None:
        data = b'{"schema": 1}'
        with patch.object(reading, "MAX_READ_BYTES", len(data)):
            result = classify(self.write("a.json", data), now=self.later, accepted=ONE)
        self.assertEqual(result.outcome, "ok")

    def test_json_nested_too_deeply_to_parse_is_invalid_json(self) -> None:
        result = classify(self.write("a.json", b"[" * 100_000), now=self.later, accepted=ONE)
        self.assertEqual(result.outcome, "invalid_json")

    def test_a_number_too_long_to_convert_is_invalid_json(self) -> None:
        result = classify(self.write("a.json", b'{"schema": 1' + b"0" * 5000 + b"}"), now=self.later, accepted=ONE)
        self.assertEqual(result.outcome, "invalid_json")

    def test_wrong_type(self) -> None:
        result = classify(self.write("a.json", b"[]"), now=self.later, accepted=ONE)
        self.assertEqual((result.outcome, result.detail), ("wrong_type", "list"))

    def test_exempt_file_needs_no_schema(self) -> None:
        result = classify(self.write("a.json", b'{"x": 1}'), now=self.later, accepted=None)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.value, {"x": 1})

    def test_schema_newer_older_and_missing(self) -> None:
        two = frozenset({2})
        cases = {b'{"schema": 3}': "newer", b'{"schema": 1}': "older",
                 b'{"x": 1}': "missing", b'{"schema": "2"}': "missing", b'{"schema": true}': "missing"}
        for data, detail in cases.items():
            with self.subTest(data=data):
                result = classify(self.write("a.json", data), now=self.later, accepted=two)
                self.assertEqual((result.outcome, result.detail), ("schema_unsupported", detail))
                self.assertIsInstance(result.value, dict)

    def test_an_accepted_older_version_is_ok(self) -> None:
        result = classify(self.write("a.json", b'{"schema": 1}'), now=self.later, accepted=frozenset({1, 2}))
        self.assertEqual((result.outcome, result.schema), ("ok", 1))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_unreadable_is_not_corruption(self) -> None:
        path = self.write("a.json", b'{"schema": 1}')
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        self.assertEqual(classify(path, now=self.later, accepted=ONE).outcome, "unreadable")

    def test_a_directory_is_unreadable(self) -> None:
        (self.dir / "d.json").mkdir()
        self.assertEqual(classify(self.dir / "d.json", now=self.later, accepted=ONE).outcome, "unreadable")


class MeasureTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "tree"
        (self.dir / "sub").mkdir(parents=True)
        (self.dir / "a").write_bytes(b"12345")
        (self.dir / "sub" / "b").write_bytes(b"123")

    def test_counts_bytes_files_and_newest(self) -> None:
        tree = measure_tree(self.dir)
        self.assertEqual((tree.bytes, tree.files, tree.truncated), (8, 2, False))
        self.assertGreaterEqual(tree.newest, (self.dir / "sub" / "b").lstat().st_mtime)

    def test_symlinks_are_not_followed(self) -> None:
        outside = Path(self.dir.parent) / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(b"x" * 1000)
        (self.dir / "link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(measure_tree(self.dir).bytes, 8)

    def test_stops_at_the_limit(self) -> None:
        tree = measure_tree(self.dir, limit=1)
        self.assertTrue(tree.truncated)
        self.assertIsNone(tree.newest)

    def test_readable_dir(self) -> None:
        self.assertTrue(readable_dir(self.dir))
        self.assertFalse(readable_dir(self.dir / "a"))
        self.assertFalse(readable_dir(self.dir / "missing"))

    def test_a_missing_root_is_undated_not_a_crash(self) -> None:
        tree = measure_tree(self.dir / "missing")
        self.assertEqual(tree, Tree(0, 0, None, False))

    def test_an_unreadable_subdirectory_leaves_the_age_unknown(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        blocked = self.dir / "sub"
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        tree = measure_tree(self.dir)
        self.assertIsNone(tree.newest)
        self.assertFalse(tree.truncated)


class PrintableTests(unittest.TestCase):
    def test_valid_text_is_unchanged(self) -> None:
        for text in ("plain", "é€", "emoji \U0001F600", "back\\slash", "\x07ctl"):
            with self.subTest(text=text):
                self.assertEqual(model.printable(text), text)

    def test_undecodable_name_bytes_and_lone_surrogates_are_escaped(self) -> None:
        self.assertEqual(model.printable("bad\udcff"), "bad\\xff")
        self.assertEqual(model.printable("x\ud800"), "x\\ud800")

    def test_containers_are_cleaned_all_the_way_down(self) -> None:
        cleaned = model.printable({"k\udcff": ["a\udcff", ("b\udcfe",), {"c\udcfd"}], "n": 3})
        self.assertEqual(cleaned, {"k\\xff": ["a\\xff", ["b\\xfe"], ["c\\xfd"]], "n": 3})


class ModelTests(unittest.TestCase):
    def test_worst(self) -> None:
        self.assertEqual(model.worst([]), model.OK)
        self.assertEqual(model.worst([model.WARN, model.FAIL, model.OK]), model.FAIL)
        self.assertEqual(model.worst([model.FAIL, model.ERROR]), model.ERROR)

    def test_finding_key_and_dict(self) -> None:
        finding = model.Finding("read.empty", model.WARN, "cache/stats.json", "/s/cache/stats.json", "o", "w")
        self.assertEqual(finding.key, "read.empty:cache/stats.json")
        self.assertNotIn("action", finding.to_dict())
        finding.action = {"kind": "x"}
        self.assertEqual(finding.to_dict()["action"], {"kind": "x"})

    def test_ago_and_size(self) -> None:
        self.assertEqual(model.ago(1), "1 second")
        self.assertEqual(model.ago(7200), "2 hours")
        self.assertEqual(model.ago(3 * 86400 + 5), "3 days")
        self.assertEqual(model.size(512), "512 B")
        self.assertEqual(model.size(42996121), "41.0 MB")


class EvidenceAndTailTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_every_outcome_after_stat_carries_size_and_mtime(self) -> None:
        path = self.dir / "a.json"
        path.write_text("{bad", encoding="utf-8")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        result = classify(path, now=2_000_000_000, accepted=None)
        self.assertEqual((result.outcome, result.size, result.mtime), ("invalid_json", 4, 1_000_000_000))
        self.assertIsNone(classify(self.dir / "absent.json", now=0, accepted=None).size)

    def test_read_tail_is_bounded_and_says_whether_it_seeked(self) -> None:
        path = self.dir / "t.jsonl"
        path.write_bytes(b"0123456789")
        self.assertEqual(read_tail(path, 4), (b"6789", True))
        self.assertEqual(read_tail(path, 100), (b"0123456789", False))

    def test_read_tail_refuses_what_is_not_a_regular_file(self) -> None:
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        self.assertIsNone(read_tail(fifo, 10))  # must return at once, never block
        self.assertIsNone(read_tail(self.dir, 10))
        self.assertIsNone(read_tail(self.dir / "absent", 10))


if __name__ == "__main__":
    unittest.main()
