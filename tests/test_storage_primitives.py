"""``Document`` and ``EventLog``: the two primitives every store builds on."""

from __future__ import annotations

import asyncio
import json
import unittest

from services.storage.document import CorruptDocument, Document, UnsupportedSchema
from services.storage.eventlog import EventLog, follow_many
from tests.support import Sandbox


def _doc(path):
    return Document(path, schema=2, empty=lambda: {"items": {}},
                    normalize=lambda d: {**d, "items": d.get("items") or {}}, name="things.json")


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self, copy_fixtures=False)
        self.path = self.sandbox.state / "things" / "things.json"

    def test_absent_reads_empty_and_a_read_never_creates_the_file(self) -> None:
        document, ok = _doc(self.path).read()
        self.assertEqual((document, ok), ({"items": {}}, True))
        self.assertFalse(self.path.exists())

    def test_modify_writes_only_when_something_changed_and_stamps(self) -> None:
        doc = _doc(self.path)
        doc.modify(lambda d: False)
        self.assertFalse(self.path.exists(), "an unchanged document is never written")
        doc.modify(lambda d: d["items"].update({"a": {"n": 1}}) or True)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["schema"], 2)
        self.assertEqual(written["items"], {"a": {"n": 1}})
        self.assertTrue(written["updated_at"].endswith("Z"))

    def test_unknown_keys_survive_a_write(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"schema": 2, "items": {}, "someone_elses": [1, 2]}), encoding="utf-8")
        _doc(self.path).modify(lambda d: d["items"].update({"b": {}}) or True)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["someone_elses"], [1, 2])

    def test_a_corrupt_file_is_served_empty_and_never_rewritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        document, ok = _doc(self.path).read()
        self.assertEqual((document, ok), ({"items": {}}, False))
        with self.assertRaises(CorruptDocument) as caught:
            _doc(self.path).modify(lambda d: True)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "corrupt_document")
        self.assertIn("things.json", caught.exception.message)
        self.assertNotIn(str(self.path), caught.exception.message, "the path goes to the log, not the wire")
        self.assertIn(str(self.path), caught.exception.log)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_newer_schema_is_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"schema": 3, "items": {}}), encoding="utf-8")
        with self.assertRaises(UnsupportedSchema):
            _doc(self.path).read()
        with self.assertRaises(UnsupportedSchema):
            _doc(self.path).modify(lambda d: True)

    def test_private_documents_are_owner_only(self) -> None:
        doc = Document(self.path, schema=1, empty=dict, private=True)
        doc.write({"token": "x"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self, copy_fixtures=False)
        self.path = self.sandbox.state / "things" / "events.jsonl"

    def _line(self, n: int, kind: str = "thing.seen") -> dict:
        return {"extra": n, "type": kind, "ts": f"2026-01-01T08:00:{n:02d}Z"}

    def test_append_puts_ts_and_type_first_and_in_order(self) -> None:
        log = EventLog(self.path)
        self.assertEqual(log.append([self._line(2), self._line(1)]), 2)
        lines = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([list(l)[:2] for l in lines], [["ts", "type"], ["ts", "type"]])
        self.assertEqual([l["extra"] for l in lines], [1, 2])

    def test_tail_is_newest_first_across_rotations_with_filters(self) -> None:
        log = EventLog(self.path, rotate_bytes=1, keep=5)
        log.append([self._line(1)])
        log.append([self._line(2, "other")])   # rotates the first line away
        log.append([self._line(3)])
        self.assertEqual(len(log.rotations()), 2)
        self.assertEqual([l["extra"] for l in log.tail(limit=10)], [3, 2, 1])
        self.assertEqual([l["extra"] for l in log.tail(limit=10, types=["thing.seen"])], [3, 1])
        self.assertEqual([l["extra"] for l in log.tail(limit=10, before="2026-01-01T08:00:03Z")], [2, 1])
        self.assertEqual([l["extra"] for l in log.tail(limit=1)], [3])

    def test_rotation_keeps_only_the_newest_segments(self) -> None:
        log = EventLog(self.path, rotate_bytes=1, keep=2)
        for n in range(1, 6):
            log.append([self._line(n)])
        self.assertLessEqual(len(log.rotations()), 2)
        self.assertEqual(log.count(), 1)

    def test_follow_yields_new_lines_and_survives_a_rotation(self) -> None:
        log = EventLog(self.path, rotate_bytes=200, keep=3)
        log.append([self._line(1)])

        async def scenario() -> list[int]:
            seen: list[int] = []
            gen = log.follow(poll_s=0.01)

            async def consume() -> None:
                async for line in gen:
                    seen.append(line["extra"])
                    if len(seen) >= 3:
                        break

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.05)
            log.append([self._line(2)])
            await asyncio.sleep(0.05)
            log.append([self._line(3), self._line(4)])
            await asyncio.wait_for(task, timeout=2)
            await gen.aclose()
            return seen

        self.assertEqual(asyncio.run(scenario()), [2, 3, 4])

    def test_follow_since_replays_the_backlog_first(self) -> None:
        log = EventLog(self.path)
        log.append([self._line(1), self._line(2), self._line(3)])

        async def scenario() -> list[int]:
            gen = log.follow(since="2026-01-01T08:00:01Z", poll_s=0.01)
            first = await gen.__anext__()
            second = await gen.__anext__()
            await gen.aclose()
            return [first["extra"], second["extra"]]

        self.assertEqual(asyncio.run(scenario()), [2, 3])

    def test_follow_many_tags_each_line_with_its_source(self) -> None:
        a = EventLog(self.sandbox.state / "a" / "events.jsonl")
        b = EventLog(self.sandbox.state / "b" / "events.jsonl")
        a.append([self._line(1)])
        b.append([self._line(2)])

        async def scenario() -> list[tuple[str, int]]:
            gen = follow_many({"a": a, "b": b}, since="2026-01-01T08:00:00Z", tag="toolkit", poll_s=0.01)
            out = []
            for _ in range(2):
                line = await gen.__anext__()
                out.append((line["toolkit"], line["extra"]))
            await gen.aclose()
            return out

        self.assertEqual(asyncio.run(scenario()), [("a", 1), ("b", 2)])
