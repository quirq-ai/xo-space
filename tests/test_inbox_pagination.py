"""Cursor pagination and server-side source/query filtering in
``service.list_items``. The store read is stubbed so these exercise the
paging and filter logic over a fixed document, independent of the feeders."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.inbox import service, store

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def iso(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(i: int, *, source: str = "api", status: str = "new",
         ts: str | None = None, title: str | None = None) -> dict:
    return {"id": f"{i:08x}", "ts": ts or iso(i), "source": source, "kind": "note",
            "title": title or f"item {i}", "body": "", "project_id": None,
            "link": None, "url": None, "status": status, "key": None}


def _doc(items: list[dict]) -> dict:
    return {"schema": 1, "updated_at": iso(0), "sources": {}, "cursors": {}, "items": items}


def _list(items: list[dict], **kwargs) -> dict:
    with patch.object(service, "refresh", return_value=False), \
         patch.object(store, "load_document", return_value=(_doc(items), True)):
        return service.list_items(**kwargs)


class CursorPaginationTests(unittest.TestCase):
    def test_pages_walk_the_whole_list_once_with_next_cursor(self) -> None:
        items = [item(i) for i in range(5)]   # id 0 newest (ts = i seconds ago)
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            res = _list(items, limit=2, cursor=cursor)
            seen += [it["id"] for it in res["items"]]
            pages += 1
            cursor = res["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10)   # guard against a non-terminating cursor
        self.assertEqual(pages, 3)                       # 2 + 2 + 1
        self.assertEqual(seen, [f"{i:08x}" for i in range(5)])   # newest first, no repeats
        self.assertEqual(len(seen), len(set(seen)))

    def test_last_page_has_no_next_cursor(self) -> None:
        res = _list([item(i) for i in range(2)], limit=5)
        self.assertEqual(len(res["items"]), 2)
        self.assertIsNone(res["next_cursor"])

    def test_cursor_advances_even_when_its_item_was_deleted(self) -> None:
        items = [item(i) for i in range(4)]
        first = _list(items, limit=2)
        cursor = first["next_cursor"]
        # the cursor's item (id 00000001, the 2nd row) is gone on the next read
        remaining = [it for it in items if it["id"] != f"{1:08x}"]
        res = _list(remaining, limit=2, cursor=cursor)
        self.assertEqual([it["id"] for it in res["items"]], [f"{2:08x}", f"{3:08x}"])

    def test_a_bad_cursor_is_a_400(self) -> None:
        with self.assertRaises(store.InboxError) as ctx:
            _list([item(0)], cursor="not-base64!!")
        self.assertEqual(ctx.exception.code, "invalid_cursor")


class ServerSideFilterTests(unittest.TestCase):
    MIX = None

    def setUp(self) -> None:
        self.MIX = [
            item(0, source="issues", title="login redirect bug"),
            item(1, source="timeline", title="session started"),
            item(2, source="todos", title="blocked on api key"),
            item(3, source="connections", title="new emails"),
            item(4, source="sharing", title="repo shared"),
            item(5, source="swarm", title="agent finished"),
        ]

    def test_source_pill_maps_to_its_feeder_sources(self) -> None:
        self.assertEqual([it["source"] for it in _list(self.MIX, source="issues")["items"]], ["issues"])
        self.assertEqual([it["source"] for it in _list(self.MIX, source="connections")["items"]], ["connections"])
        self.assertEqual(sorted(it["source"] for it in _list(self.MIX, source="workspace")["items"]),
                         ["timeline", "todos"])
        # agents is the catch-all: anything not written by a named feeder
        self.assertEqual([it["source"] for it in _list(self.MIX, source="agents")["items"]], ["swarm"])
        # all (and no source) returns everything
        self.assertEqual(len(_list(self.MIX, source="all")["items"]), 6)
        self.assertEqual(len(_list(self.MIX)["items"]), 6)

    def test_query_matches_across_fields_and_all_terms(self) -> None:
        res = _list(self.MIX, query="login bug")
        self.assertEqual([it["source"] for it in res["items"]], ["issues"])
        self.assertEqual(_list(self.MIX, query="nomatch")["items"], [])

    def test_source_and_query_compose_with_pagination(self) -> None:
        items = [item(i, source="connections", title=f"email {i}") for i in range(5)]
        items += [item(100 + i, source="issues", title=f"issue {i}") for i in range(3)]
        res = _list(items, source="connections", query="email", limit=2)
        self.assertEqual(len(res["items"]), 2)
        self.assertTrue(all(it["source"] == "connections" for it in res["items"]))
        self.assertIsNotNone(res["next_cursor"])

    def test_an_unknown_source_is_a_400(self) -> None:
        with self.assertRaises(store.InboxError) as ctx:
            _list(self.MIX, source="nope")
        self.assertEqual(ctx.exception.code, "invalid_value")

    def test_counts_cover_the_whole_file_not_the_filtered_page(self) -> None:
        items = [item(0, source="issues", status="new"),
                 item(1, source="timeline", status="seen"),
                 item(2, source="connections", status="done")]
        res = _list(items, status="open", source="issues")
        self.assertEqual(res["counts"], {"new": 1, "seen": 1, "done": 1})   # whole file
        self.assertEqual([it["id"] for it in res["items"]], [f"{0:08x}"])   # filtered page


if __name__ == "__main__":
    unittest.main()
