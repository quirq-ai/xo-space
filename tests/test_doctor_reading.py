"""xo-doctor reads a file into exactly one outcome and never returns its content."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from services.doctor import model
from services.doctor.reading import classify, measure_tree, readable_dir

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

    def test_an_unreadable_subdirectory_leaves_the_age_unknown(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        blocked = self.dir / "sub"
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        tree = measure_tree(self.dir)
        self.assertIsNone(tree.newest)
        self.assertFalse(tree.truncated)


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


if __name__ == "__main__":
    unittest.main()
