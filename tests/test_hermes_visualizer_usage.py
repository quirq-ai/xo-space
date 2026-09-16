"""The hermes watcher source reports token usage from the session totals.

Hermes keeps usage per session, not per message, so the source turns growth in
the ``sessions`` row's totals into ``UsageObserved`` events and remembers the
totals it recorded in ``hermes-offsets.json``. These tests drive the real
source over a small state.db: first sight, no change, growth, a restart, a
reset session, and a hermes build without usage columns.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.hermes import visualizer_source as source_mod
from services.cowork_agent.visualizer.ingest.events import UsageObserved
from services.cowork_agent.visualizer.sinks import stats

SID = "83152f65-0f2a-473f-845a-951b611d7f19"
PROJECT = "demo"


class HermesWatcherUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.db = self.base / "state.db"
        self.offsets = self.base / "hermes-offsets.json"
        for patch in (
            mock.patch.object(source_mod, "_OFFSETS_FILE", self.offsets),
            mock.patch.object(source_mod, "_LEGACY_OFFSETS_FILE", self.base / "legacy.json"),
            mock.patch.object(source_mod, "_profile_state_dbs", return_value=[("proto", self.db)]),
            mock.patch.object(source_mod, "_build_session_to_project_map", return_value={SID: PROJECT}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def make_db(self, *, usage_columns: bool = True) -> None:
        usage = (
            " input_tokens integer, output_tokens integer,"
            " cache_read_tokens integer, cache_write_tokens integer,"
            if usage_columns else ""
        )
        connection = sqlite3.connect(self.db)
        connection.executescript(
            f"create table sessions (id text, model text,{usage} last_activity_at real);"
            "create table messages (id integer primary key, session_id text, role text,"
            " content text, tool_calls text, timestamp real);"
        )
        columns = "id, model, last_activity_at" + (
            ", input_tokens, output_tokens, cache_read_tokens, cache_write_tokens" if usage_columns else ""
        )
        values = (SID, "gpt-x", 1789508625.0) + ((0, 0, 0, 0) if usage_columns else ())
        connection.execute(
            f"insert into sessions ({columns}) values ({','.join('?' * len(values))})", values,
        )
        connection.executemany(
            "insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
            [(SID, "user", "hi", 1789507583.4), (SID, "assistant", "hello", 1789507600.1)],
        )
        connection.commit()
        connection.close()

    def set_totals(self, inp: int, out: int, cache_read: int, cache_write: int = 0) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute(
            "update sessions set input_tokens=?, output_tokens=?, cache_read_tokens=?,"
            " cache_write_tokens=? where id=?",
            (inp, out, cache_read, cache_write, SID),
        )
        connection.commit()
        connection.close()

    @staticmethod
    def usage(events) -> list[UsageObserved]:
        return [event for event in events if isinstance(event, UsageObserved)]

    def test_growth_in_session_totals_becomes_usage_once(self) -> None:
        self.make_db()
        self.set_totals(204065, 20880, 3227264)
        source = source_mod.Source()

        [first] = self.usage(list(source.poll_events()))
        self.assertEqual(
            (first.input_tokens, first.output_tokens, first.cache_read_input_tokens,
             first.cache_creation_input_tokens, first.model, first.project_id, first.runtime),
            (204065, 20880, 3227264, 0, "gpt-x", PROJECT, "hermes"),
        )
        self.assertTrue(first.ts.startswith("2026-09-15T21:43:45"))
        self.assertEqual(self.usage(list(source.poll_events())), [], "unchanged totals")

        self.set_totals(210000, 21000, 3300000, 10)
        [delta] = self.usage(list(source.poll_events()))
        self.assertEqual(
            (delta.input_tokens, delta.output_tokens, delta.cache_read_input_tokens,
             delta.cache_creation_input_tokens),
            (5935, 120, 72736, 10),
        )

        # A restart reads the recorded totals back and counts nothing twice.
        self.assertEqual(self.usage(list(source_mod.Source().poll_events())), [])
        saved = json.loads(self.offsets.read_text(encoding="utf-8"))
        self.assertEqual(saved[f"proto:{SID}#input_tokens"], 210000)
        self.assertEqual(saved[f"proto:{SID}"], 2)

    def test_a_lower_total_is_a_new_baseline_not_negative_usage(self) -> None:
        self.make_db()
        self.set_totals(1000, 100, 0)
        source = source_mod.Source()
        list(source.poll_events())
        self.set_totals(300, 30, 0)
        self.assertEqual(self.usage(list(source.poll_events())), [])
        self.set_totals(400, 50, 0)
        [delta] = self.usage(list(source.poll_events()))
        self.assertEqual((delta.input_tokens, delta.output_tokens), (100, 20))

    def test_the_stats_sink_records_the_tokens(self) -> None:
        self.make_db()
        self.set_totals(204065, 20880, 3227264)
        runtime = self.base / "runtime"
        runtime.mkdir()
        stats.apply(runtime, list(source_mod.Source().poll_events()))
        document = json.loads((runtime / "stats.json").read_text(encoding="utf-8"))
        self.assertEqual(document["_session_totals"][SID]["tokens"], {"input": 204065, "output": 20880})
        self.assertEqual(document["by_runtime"]["hermes"]["tokens"], {"input": 204065, "output": 20880})

    def test_a_hermes_build_without_usage_columns_still_reports_messages(self) -> None:
        self.make_db(usage_columns=False)
        events = list(source_mod.Source().poll_events())
        self.assertEqual(self.usage(events), [])
        self.assertEqual(sum(1 for event in events if type(event).__name__ == "MessageObserved"), 2)


if __name__ == "__main__":
    unittest.main()
