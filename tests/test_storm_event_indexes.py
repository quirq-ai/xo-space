"""The reads that ask for one type in one chat or one session, and the replay
page, go straight to their events through an index (store v9) instead of
walking every event of the chat, the session or the whole log."""

from __future__ import annotations

import asyncio
import sqlite3
import unittest

from services.cowork_agent.storm.store.db import SCHEMA_FILE
from services.cowork_agent.storm.stream.frames import Filter
from services.cowork_agent.storm.stream.replay import read_page


def plan(conn: sqlite3.Connection, sql: str, params=()) -> str:
    return " | ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))


class _Explained:
    """A connection that keeps the plan of each query it runs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn, self.plans = conn, []

    def execute(self, sql: str, params=()):
        self.plans.append(plan(self.conn, sql, params))
        return self.conn.execute(sql, params)


class _Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = _Explained(conn)

    async def read(self, fn):
        return fn(self.conn)


class EventIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA_FILE.read_text())
        self.conn.executemany(
            "INSERT INTO events (ts, type, chat_id, session_id) VALUES ('2026-11-27T00:00:00Z', ?, ?, ?)",
            [(t, c, s) for c, s in (("c1", "s1"), ("c1", "s2"), ("c2", "s3"))
             for t in ("turn.started", "keeper.wake", "comms.call", "thread.continued")] * 5)
        self.addCleanup(self.conn.close)

    def test_one_type_in_a_chat_or_a_session_reads_its_own_index(self) -> None:
        for sql, index in (
                ("SELECT data FROM events WHERE type = 'keeper.wake' AND chat_id = ? "
                 "AND json_extract(data, '$.goal_id') = ? ORDER BY seq DESC LIMIT 1", "events_by_chat_type"),
                ("SELECT data FROM events WHERE type = 'thread.continued' AND chat_id = ? ORDER BY seq",
                 "events_by_chat_type"),
                ("SELECT data FROM events WHERE session_id = ? AND type = 'comms.call' "
                 "AND json_extract(data, '$.result_id') = ? ORDER BY seq DESC LIMIT 1", "events_by_session_type")):
            with self.subTest(sql=sql):
                shown = plan(self.conn, sql, ("x",) * sql.count("?"))
                self.assertIn(f"USING INDEX {index}", shown)
                self.assertNotIn("TEMP B-TREE", shown)

    def test_a_replay_page_reads_only_what_its_filter_names(self) -> None:
        cases = (
            (Filter(chat="c1", session="s2"), "events_by_session", {("c1", "s2")}),
            (Filter(chat="c1"), "events_by_chat", {("c1", "s1"), ("c1", "s2")}),
            (Filter(session="s3"), "events_by_session", {("c2", "s3")}),
            (Filter(), None, {("c1", "s1"), ("c1", "s2"), ("c2", "s3")}))
        for flt, index, scopes in cases:
            for newest in (False, True):
                with self.subTest(filter=flt, newest=newest):
                    store = _Store(self.conn)
                    page = asyncio.run(read_page(store, flt, after=2, before=55, limit=100, newest=newest))
                    want = [seq for (seq,) in self.conn.execute(
                        "SELECT seq FROM events WHERE seq > 2 AND seq < 55 AND (? IS NULL OR chat_id = ?) "
                        "AND (? IS NULL OR session_id = ?) ORDER BY seq",
                        (flt.chat, flt.chat, flt.session, flt.session))]
                    self.assertEqual([e.seq for e in page], want)
                    self.assertTrue(want and {(e.chat_id, e.session_id) for e in page} == scopes)
                    (shown,) = store.conn.plans
                    self.assertNotIn("TEMP B-TREE", shown)
                    if index:
                        self.assertRegex(shown, f"USING INDEX {index} \\(")
                    else:
                        self.assertIn("USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)", shown)

    def test_a_newest_page_is_the_last_ones_in_seq_order(self) -> None:
        page = asyncio.run(read_page(_Store(self.conn), Filter(chat="c1"), limit=3, newest=True))
        last = [seq for (seq,) in self.conn.execute(
            "SELECT seq FROM events WHERE chat_id = 'c1' ORDER BY seq DESC LIMIT 3")][::-1]
        self.assertEqual([e.seq for e in page], last)


if __name__ == "__main__":
    unittest.main()
