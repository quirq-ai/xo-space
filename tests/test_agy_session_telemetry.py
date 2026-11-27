"""agy's Space session telemetry: its conversations from agy's own index, their
tokens from agy's own per-call usage records (sub-agents' in their root's row,
each call on its own day), no cost, and no prompt text."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.antigravity import session_telemetry as t
from services.cowork_agent.adapters.antigravity import tokens as agy_tokens
from tests.test_agy_usage import AgyStore, build_fixture, call_row


class AgyTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = AgyStore(Path(tmp.name))
        build_fixture(self.store)
        con = sqlite3.connect(self.store.summaries)
        con.execute("INSERT INTO conversation_summaries VALUES ('c3', 'no folder', '2026-11-27 09:36:00+00:00', "
                    "'[]', 0, NULL)")
        con.commit()
        con.close()
        self.store.patch(self, (t, "_SUMMARIES_DB", self.store.summaries),
                         (t, "CONVERSATIONS_DIR", self.store.conversations),
                         (t, "transcript_path", self.store.transcript))

    def test_a_root_conversation_carries_its_own_and_its_sub_agents_usage(self) -> None:
        payload = t.collect_session_telemetry()
        [row] = payload["sessions"]
        self.assertEqual((row["id"], row["project"], row["project_path"]), ("c1", "app", "/work/app"))
        self.assertEqual((row["started_at"], row["ended_at"]), ("2026-11-20T10:00:00Z", "2026-11-21T09:05:00Z"))
        self.assertEqual((row["fresh"], row["output"], row["cache_read"], row["cache_write"]), (180, 33, 3000, 7))
        self.assertEqual(row["tokens"], 180 + 33 + 3000 + 7)
        self.assertEqual((row["turns"], row["unreported_calls"], row["unclassified"]), (2, 1, 0))
        self.assertEqual((row["cost"], row["cost_known"], row["tokens_known"]), (None, False, True))
        self.assertEqual(payload["totals"]["tokens"], row["tokens"])
        self.assertIsNone(payload["totals"]["cost_usd"])
        self.assertEqual(payload["totals"]["unreported_calls"], 1)
        self.assertNotIn("secret", json.dumps(payload))

    def test_daily_rows_follow_each_calls_own_day(self) -> None:
        payload = t.collect_session_telemetry()
        self.assertEqual({(r["day"], r["tokens"]) for r in payload["daily_sessions"]},
                         {("2026-11-20", 1127), ("2026-11-21", 2093)})
        self.assertTrue(all(r["cost"] is None and not r["cost_known"] for r in payload["daily_models"]))
        self.assertEqual(sum(r["tokens"] for r in payload["daily_models"]), payload["totals"]["tokens"])

    def test_a_conversation_is_read_again_only_once_agy_changes_it(self) -> None:
        reads = []
        real = agy_tokens.read_conversation

        def counted(path):
            reads.append(Path(path).stem)
            return real(path)
        with mock.patch.object(agy_tokens, "read_conversation", counted):
            t.collect_session_telemetry()
            t.collect_session_telemetry()
            self.assertEqual(sorted(reads), ["c1", "c2"])
            con = sqlite3.connect(self.store.conversations / "c2.db")
            con.execute("INSERT INTO gen_metadata VALUES (?, ?)", call_row(1, fresh=1, output=1, steps=(0,)))
            con.commit()
            con.close()
            [row] = t.collect_session_telemetry()["sessions"]
        self.assertEqual(sorted(reads), ["c1", "c2", "c2"])
        self.assertEqual(row["fresh"], 181)

    def test_no_index_means_no_source(self) -> None:
        with mock.patch.object(t, "_SUMMARIES_DB", self.store.root / "missing.db"), self.assertRaises(ValueError):
            t.collect_session_telemetry()


if __name__ == "__main__":
    unittest.main()
