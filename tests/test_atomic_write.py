"""Tests for the two write primitives (syncplan §3 / T3, rule R-WRITE).

``write_json_owned`` is a key-scoped merge: it replaces exactly the keys
the caller declares, carries every other key forward, and deletes an
owned key the caller omitted. ``write_json_atomic_if_changed`` is the
full-ownership variant. Both skip the write when nothing but a volatile
path differs — and ``volatile`` is a *dotted* path because
``sessions.json`` stamps its timestamp at ``meta.generated_at``, not at
the top level.

Written as ``unittest.TestCase`` because the suite runs under
``unittest discover`` (pytest is installed but unconfigured until T16).
Every path here is a ``tempfile`` path — nothing touches ``~/xo-projects``
or ``~/.quirq``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_atomic,
    write_json_atomic_if_changed,
    write_json_owned,
)


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def path(self, name: str = "doc.json") -> Path:
        return self.root / name

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def seed(self, path: Path, payload: dict) -> None:
        write_json_atomic(path, payload)


class WriteJsonOwnedTests(_TmpDirTestCase):
    # ── Acceptance (syncplan §6, T3) ─────────────────────────────────────

    def test_preserves_a_key_the_writer_does_not_own(self) -> None:
        path = self.path()
        self.seed(path, {
            "schema": 1,
            "pid": "abc",
            "display_name": "The name the user typed",
            "category": "manual",
        })

        changed = write_json_owned(
            path,
            owns=frozenset({"schema", "pid", "name"}),
            values={"schema": 2, "pid": "abc", "name": "proj"},
        )

        self.assertTrue(changed)
        doc = self.read(path)
        self.assertEqual(doc["display_name"], "The name the user typed")
        self.assertEqual(doc["category"], "manual")
        self.assertEqual(doc["schema"], 2)
        self.assertEqual(doc["name"], "proj")

    def test_omitting_an_owned_key_deletes_it(self) -> None:
        path = self.path()
        self.seed(path, {"pid": "abc", "name": "proj", "foreign": "keep me"})

        changed = write_json_owned(
            path,
            owns=frozenset({"pid", "name"}),
            values={"pid": "abc"},  # name omitted -> delete
        )

        self.assertTrue(changed)
        doc = self.read(path)
        self.assertNotIn("name", doc)
        self.assertEqual(doc["pid"], "abc")
        self.assertEqual(doc["foreign"], "keep me")

    def test_returns_false_when_only_updated_at_differs(self) -> None:
        path = self.path()
        self.seed(path, {"pid": "abc", "updated_at": "2026-01-01T00:00:00Z"})

        changed = write_json_owned(
            path,
            owns=frozenset({"pid", "updated_at"}),
            values={"pid": "abc", "updated_at": "2026-09-07T12:00:00Z"},
        )

        self.assertFalse(changed)
        # Not written at all: the stale volatile value is still on disk.
        self.assertEqual(self.read(path)["updated_at"], "2026-01-01T00:00:00Z")

    def test_nested_volatile_path_is_not_a_change(self) -> None:
        # sessions.json stamps meta.generated_at, not a top-level field
        # (visualizer/session_telemetry.py:271). A top-level-only
        # exclusion would rewrite this file every tick, forever.
        path = self.path("sessions.json")
        self.seed(path, {
            "meta": {"generated_at": "2026-01-01T00:00:00Z", "sources": ["a"]},
            "sessions": [{"id": "s1"}],
        })

        changed = write_json_owned(
            path,
            owns=frozenset({"meta", "sessions"}),
            values={
                "meta": {
                    "generated_at": "2026-09-07T12:00:00Z",
                    "sources": ["a"],
                },
                "sessions": [{"id": "s1"}],
            },
            volatile=("updated_at", "meta.generated_at"),
        )

        self.assertFalse(changed)
        self.assertEqual(
            self.read(path)["meta"]["generated_at"], "2026-01-01T00:00:00Z"
        )

    # ── The case a naive nested implementation gets wrong ────────────────

    def test_real_change_beside_a_nested_volatile_path_still_writes(self) -> None:
        # meta.generated_at is volatile but meta.sources is not: dropping
        # the whole `meta` key from the comparison would silently swallow
        # this change.
        path = self.path("sessions.json")
        self.seed(path, {
            "meta": {"generated_at": "2026-01-01T00:00:00Z", "sources": ["a"]},
        })

        changed = write_json_owned(
            path,
            owns=frozenset({"meta"}),
            values={
                "meta": {
                    "generated_at": "2026-09-07T12:00:00Z",
                    "sources": ["a", "b"],
                },
            },
            volatile=("meta.generated_at",),
        )

        self.assertTrue(changed)
        doc = self.read(path)
        self.assertEqual(doc["meta"]["sources"], ["a", "b"])
        self.assertEqual(doc["meta"]["generated_at"], "2026-09-07T12:00:00Z")

    # ── Edges: absent, corrupt, real change, order ───────────────────────

    def test_absent_file_is_created_from_values_alone(self) -> None:
        path = self.root / "nested" / "dir" / "project.json"

        changed = write_json_owned(
            path,
            owns=frozenset({"pid", "name"}),
            values={"pid": "abc", "name": "proj"},
        )

        self.assertTrue(changed)
        self.assertTrue(path.is_file())
        self.assertEqual(self.read(path), {"pid": "abc", "name": "proj"})

    def test_corrupt_file_raises_instead_of_being_treated_as_empty(self) -> None:
        # Treating corrupt-as-empty is the bug class T1 fixes: the merge
        # base is gone, so every unowned key would be dropped and a fresh
        # pid minted over a document that already had one.
        path = self.path()
        path.write_text('{"pid": "abc", "name":', encoding="utf-8")

        with self.assertRaises(CorruptDocumentError) as ctx:
            write_json_owned(
                path,
                owns=frozenset({"pid"}),
                values={"pid": "def"},
            )

        self.assertIn(str(path), str(ctx.exception))
        # The damaged bytes are left exactly as found, for repair.
        self.assertEqual(path.read_text(encoding="utf-8"), '{"pid": "abc", "name":')

    def test_empty_and_non_object_documents_are_corrupt_too(self) -> None:
        truncated = self.path("truncated.json")
        truncated.write_text("", encoding="utf-8")
        with self.assertRaises(CorruptDocumentError):
            write_json_owned(truncated, owns=frozenset({"a"}), values={"a": 1})

        as_list = self.path("list.json")
        as_list.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(CorruptDocumentError):
            write_json_owned(as_list, owns=frozenset({"a"}), values={"a": 1})

        # A top-level JSON *string* parses fine but is still not an
        # object to merge into — and must be reported as a wrong shape,
        # not mistaken for a parse-failure message.
        as_string = self.path("string.json")
        as_string.write_text('"invalid JSON: not really"', encoding="utf-8")
        with self.assertRaises(CorruptDocumentError) as ctx:
            write_json_owned(as_string, owns=frozenset({"a"}), values={"a": 1})
        self.assertIn("top-level str", str(ctx.exception))

    def test_real_content_change_returns_true(self) -> None:
        path = self.path()
        self.seed(path, {"pid": "abc", "name": "old"})

        changed = write_json_owned(
            path,
            owns=frozenset({"pid", "name"}),
            values={"pid": "abc", "name": "new"},
        )

        self.assertTrue(changed)
        self.assertEqual(self.read(path)["name"], "new")

    def test_identical_document_in_a_different_key_order_is_not_a_change(self) -> None:
        # Comparison is on parsed objects, not serialised text — a key
        # reorder must not read as a change forever.
        path = self.path()
        self.seed(path, {"b": 2, "a": 1, "foreign": True})

        changed = write_json_owned(
            path,
            owns=frozenset({"a", "b"}),
            values={"a": 1, "b": 2},
        )

        self.assertFalse(changed)

    def test_a_value_outside_the_declared_ownership_set_is_rejected(self) -> None:
        path = self.path()
        self.seed(path, {"pid": "abc"})

        with self.assertRaises(ValueError):
            write_json_owned(
                path,
                owns=frozenset({"pid"}),
                values={"pid": "abc", "smuggled": 1},
            )

    def test_merge_does_not_reshuffle_the_document(self) -> None:
        path = self.path()
        self.seed(path, {"schema": 1, "pid": "abc", "display_name": "Name"})

        write_json_owned(
            path,
            owns=frozenset({"schema", "pid"}),
            values={"schema": 2, "pid": "abc"},
        )

        self.assertEqual(
            list(self.read(path).keys()), ["schema", "pid", "display_name"]
        )


class WriteJsonAtomicIfChangedTests(_TmpDirTestCase):
    def test_skips_the_write_when_only_updated_at_differs(self) -> None:
        path = self.path()
        self.seed(path, {"count": 3, "updated_at": "2026-01-01T00:00:00Z"})
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        changed = write_json_atomic_if_changed(
            path, {"count": 3, "updated_at": "2026-09-07T12:00:00Z"}
        )

        self.assertFalse(changed)
        # Genuinely not written: same bytes, same inode timestamp.
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_real_change_writes(self) -> None:
        path = self.path()
        self.seed(path, {"count": 3, "updated_at": "2026-01-01T00:00:00Z"})

        changed = write_json_atomic_if_changed(
            path, {"count": 4, "updated_at": "2026-09-07T12:00:00Z"}
        )

        self.assertTrue(changed)
        self.assertEqual(self.read(path)["count"], 4)

    def test_absent_file_is_written(self) -> None:
        path = self.root / "made" / "up" / "stats.json"
        self.assertTrue(write_json_atomic_if_changed(path, {"count": 1}))
        self.assertEqual(self.read(path), {"count": 1})

    def test_corrupt_file_is_repaired_rather_than_raising(self) -> None:
        # Full ownership: there is nothing in the file worth preserving,
        # so overwriting it is the repair. This is the deliberate
        # asymmetry with write_json_owned.
        path = self.path()
        path.write_text("{not json", encoding="utf-8")

        self.assertTrue(write_json_atomic_if_changed(path, {"count": 1}))
        self.assertEqual(self.read(path), {"count": 1})

    def test_nested_volatile_path_is_not_a_change(self) -> None:
        path = self.path("sessions.json")
        self.seed(path, {"meta": {"generated_at": "t1", "sources": ["a"]}})

        changed = write_json_atomic_if_changed(
            path,
            {"meta": {"generated_at": "t2", "sources": ["a"]}},
            ("updated_at", "meta.generated_at"),
        )

        self.assertFalse(changed)
        self.assertEqual(self.read(path)["meta"]["generated_at"], "t1")

    def test_real_change_beside_a_nested_volatile_path_still_writes(self) -> None:
        path = self.path("sessions.json")
        self.seed(path, {"meta": {"generated_at": "t1", "sources": ["a"]}})

        changed = write_json_atomic_if_changed(
            path,
            {"meta": {"generated_at": "t2", "sources": ["a", "b"]}},
            ("meta.generated_at",),
        )

        self.assertTrue(changed)
        self.assertEqual(self.read(path)["meta"]["sources"], ["a", "b"])

    def test_previous_in_memory_skips_the_read(self) -> None:
        # The caller's cached payload is the baseline; the file is never
        # consulted. Proved by a file whose contents disagree with both.
        path = self.path()
        path.write_text("{corrupt", encoding="utf-8")
        before_bytes = path.read_bytes()

        changed = write_json_atomic_if_changed(
            path,
            {"count": 1, "updated_at": "t2"},
            previous={"count": 1, "updated_at": "t1"},
        )

        self.assertFalse(changed)
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_previous_none_means_there_was_no_document(self) -> None:
        path = self.path()
        self.seed(path, {"count": 1})

        changed = write_json_atomic_if_changed(path, {"count": 1}, previous=None)

        self.assertTrue(changed)

    def test_key_reorder_is_not_a_change(self) -> None:
        path = self.path()
        self.seed(path, {"a": 1, "b": 2})

        self.assertFalse(write_json_atomic_if_changed(path, {"b": 2, "a": 1}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
