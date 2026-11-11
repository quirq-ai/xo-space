"""``scripts/storm_audit.py``, the storm rebuild's baseline: it reads the
measures off a store and never writes to it (Step 0)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import storm_audit
from services.cowork_agent.storm.store import SCHEMA_VERSION, Store
from tests.required_tools import clean_env
from tests.test_storm_store import digest

SCRIPT = Path(storm_audit.__file__)
ROWS = """
INSERT INTO chats (id, kind, project_pid, project_name, created_at) VALUES ('chat_p', 'project', 'p', 'demo', 't');
INSERT INTO sessions (id, seq, chat_id, thread_ts, role, preset, agent, spec, status, paused, retired_at, created_at,
                      updated_at) VALUES
  ('cse_old', 1, 'chat_p', NULL, 'project-coordinator', 'project_coordinator', 'agent_a', '{}', 'cold', NULL,
   '2026-11-11T08:00:00Z', '2026-11-10T08:00:00Z', 't'),
  ('cse_new', 2, 'chat_p', NULL, 'project-coordinator', 'project_coordinator', 'agent_a', '{}', 'warm', NULL, NULL,
   '2026-11-11T08:00:00Z', 't'),
  ('cse_t1', 3, 'chat_p', '1.0', 'thread', 'thread', 'agent_b', '{}', 'running', NULL, NULL, '2026-11-11T09:00:00Z', 't'),
  ('cse_t2', 4, 'chat_p', '2.0', 'thread', 'thread', 'agent_b', '{}', 'cold', 'stopped by the owner', NULL,
   '2026-11-11T09:00:00Z', 't');
INSERT INTO threads (chat_id, ts, state, session_id, created_at, updated_at) VALUES
  ('chat_p', '1.0', 'working', 'cse_t1', 't', 't'), ('chat_p', '2.0', 'working', 'cse_t2', 't', 't');
INSERT INTO turns (id, session_id, epoch, origin, status, started_at, usage) VALUES
  ('turn_1', 'cse_old', 1, 'wake', 'finished', '2026-11-10T09:00:00Z', '{"context_tokens": null}'),
  ('turn_2', 'cse_new', 1, 'injected', 'finished', '2026-11-11T09:00:00Z', '{"context_tokens": 5000}'),
  ('turn_3', 'cse_t1', 1, 'person', 'running', '2026-11-11T11:00:00Z', NULL),
  ('turn_4', 'cse_t2', 1, 'injected', 'finished', '2026-11-11T10:00:00Z', '{"context_tokens": null}');
INSERT INTO wakes (id, session_id, priority, reason, state, created_at) VALUES
  ('review:1', 'cse_old', 'now', 'review-answered', 'queued', '2026-11-11T07:00:00Z'),
  ('msg:1', 'cse_new', 'now', 'message', 'consumed', '2026-11-11T09:00:00Z');
INSERT INTO pools (id, state, resets_at, updated_at) VALUES ('agent_a:acct-1', 'warn', '2026-11-12T00:00:00Z', 't');
INSERT INTO events (ts, type, session_id, data) VALUES
  ('2026-09-01T00:00:00Z', 'step.tool', 'cse_old', '{}'),
  ('2026-11-11T08:00:00Z', 'session', 'cse_old', '{"reason": "retired", "why": "turns"}'),
  ('2026-11-11T09:00:00Z', 'run.started', 'cse_t1', '{}'),
  ('2026-11-11T11:00:00Z', 'turn.started', 'cse_t1', '{}');
"""


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "xo.db"

        async def make() -> None:
            await (await Store.open(self.store)).close()
        asyncio.run(make())
        conn = sqlite3.connect(self.store)
        conn.executescript(ROWS)
        conn.close()

    def run_audit(self, *args: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(storm_audit.main([str(self.store), *args]), 0)
        return json.loads(out.getvalue())

    def test_the_measures_read_off_a_store(self) -> None:
        report = self.run_audit("--since", "2026-11-11T00:00:00Z")
        self.assertEqual(report["store"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["turns_by_origin"]["all"]["by_origin"], {"injected": 2, "person": 1, "wake": 1})
        self.assertEqual(report["turns_by_origin"]["since"]["total"], 3)
        self.assertEqual(report["injected_turns"]["since"], {"coordinator": 1, "thread": 1})
        self.assertEqual(report["wakes_by_reason"], {"message": 1, "review-answered": 1})
        self.assertEqual(report["answers_stuck_on_retired_sessions"]["count"], 1)
        self.assertEqual(report["threads_working"]["count"], 2)
        self.assertEqual(report["threads_working"]["with_no_turn_running"], 1)
        self.assertEqual(report["sessions"], {"by_status": {"cold": 1, "running": 1, "warm": 1}, "paused": 1,
                                              "retired": 1})
        self.assertEqual(report["coordinator_incarnations"]["total"], 1)
        self.assertEqual(report["replacements"]["since"], {"turns": 1})
        self.assertEqual([p["state"] for p in report["pools"]], ["warn"])
        retention = report["retention"]
        self.assertEqual((retention["events"], retention["in_pruned_families"], retention["prunable_now"]), (4, 2, 1))
        missing = {(row["kind"], row["agent"]): row["context_tokens_null"]
                   for row in report["context_tokens_missing"]["all"]}
        self.assertEqual(missing, {("coordinator", "agent_a"): 1, ("thread", "agent_b"): 2})

    def test_it_never_writes_to_the_store(self) -> None:
        before = digest(self.store)
        self.run_audit()
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.store), "--table"], env=clean_env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("turns_by_origin:", result.stdout)
        self.assertEqual(digest(self.store), before)
        conn = storm_audit.open_read_only(self.store)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM events")

    def test_the_live_store_is_refused(self) -> None:
        with patch.object(storm_audit, "store_path", return_value=self.store), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            storm_audit.main([str(self.store)])
        self.assertIn("is the live store; audit a copy", err.getvalue())


if __name__ == "__main__":
    unittest.main()
