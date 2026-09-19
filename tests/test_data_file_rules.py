"""Rules 2 to 4 for the data files XO Space writes.

2. Times are ISO-8601 UTC ending in ``Z``.
3. Every event line starts with ``ts`` and ``type``.
4. Every data file carries a ``schema`` number.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.jobs import scheduler
from services import usage_sync
from services.cowork_agent.project_sharing import state as sharing_state
from services.cowork_agent.visualizer import watcher as watcher_mod
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore
from services.cowork_agent.visualizer.state import watcher_heartbeat_path
from services.cowork_agent.xo_projects_sync import manifest
from tests.support import SandboxTestCase


class _Sandbox(SandboxTestCase):
    """Empty roots: every test here writes its own files and reads them back."""

    copy_fixtures = False

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class EventLineTests(_Sandbox):
    def test_a_scheduler_run_line_starts_with_ts_and_type(self) -> None:
        record = {
            "started_at": "2026-09-14T10:00:00Z", "finished_at": "2026-09-14T10:00:05Z",
            "trigger": "manual", "status": "ok", "returncode": 0,
            "duration_seconds": 5.0, "output_tail": "done",
        }
        scheduler._append_run("job1", record)
        [line] = scheduler.runs_file("job1").read_text(encoding="utf-8").splitlines()
        written = json.loads(line)
        self.assertEqual(list(written)[:3], ["ts", "type", "job_id"])
        self.assertEqual(
            (written["ts"], written["type"], written["job_id"]),
            ("2026-09-14T10:00:05Z", scheduler.RUN_EVENT_TYPE, "job1"),
        )
        self.assertEqual({k: written[k] for k in record}, record)


class SchemaStampTests(_Sandbox):
    def test_watcher_offsets_carry_schema(self) -> None:
        path = self.sandbox.state / "watcher" / "offsets.json"
        store = OffsetStore(path, legacy_store_path=None)
        store.set(Path("/tmp/a.jsonl"), offset=10, inode=1)
        store.flush()
        document = self.load(path)
        self.assertEqual(document["schema"], 1)
        self.assertNotIn("version", document)
        self.assertEqual(OffsetStore(path, legacy_store_path=None).get(Path("/tmp/a.jsonl")), (10, 1))

    def test_offsets_written_with_version_still_load(self) -> None:
        path = self.sandbox.base / "offsets.json"
        path.write_text(json.dumps({"version": 1, "offsets": {"/tmp/a.jsonl": {"offset": 3, "inode": 2}}}))
        self.assertEqual(OffsetStore(path, legacy_store_path=None).get(Path("/tmp/a.jsonl")), (3, 2))

    def test_the_heartbeat_carries_schema(self) -> None:
        with patch.object(watcher_mod, "get_active_agent", return_value=SimpleNamespace(name="stub")), \
             patch.object(watcher_mod, "try_load_capability", return_value=None):
            watcher = watcher_mod.Watcher()
        watcher._write_heartbeat(time.monotonic())
        beat = self.load(watcher_heartbeat_path())
        self.assertEqual(list(beat)[:2], ["schema", "last_tick_at"])
        self.assertTrue(beat["last_tick_at"].endswith("Z"))

    def test_sharing_bookmarks_and_removal_markers_carry_schema(self) -> None:
        repo = "github.com/acme/sample"
        sharing_state.save_cursor(repo, 5)
        sharing_state.save_last_reported(repo, "abc123")
        bookmark = self.load(sharing_state.state_path(repo))
        self.assertEqual(list(bookmark)[0], "schema")
        self.assertEqual((bookmark["cursor"], bookmark["last_reported"]), (5, "abc123"))
        self.assertEqual(sharing_state.load_cursor(repo), 5)

        root = self.sandbox.projects
        sharing_state.mark_removed(repo, root)
        self.assertEqual(self.load(sharing_state.removed_path(repo, root))["schema"], 1)

    def test_the_usage_watermark_carries_schema_and_z_times(self) -> None:
        path = self.sandbox.base / "usage" / "state.json"
        with patch.object(usage_sync, "SYNC_STATE_FILE", str(path)):
            usage_sync._save_sync_state({"schema": 99, "last_synced_date": "2026-09-13"})
        document = self.load(path)
        self.assertEqual(document, {"schema": 1, "last_synced_date": "2026-09-13"})
        self.assertTrue(usage_sync._now_z().endswith("Z"))


class TimeFormatTests(unittest.TestCase):
    def test_backup_manifest_stamps_end_in_z(self) -> None:
        self.assertTrue(manifest.utc_iso_now().endswith("Z"))


if __name__ == "__main__":
    unittest.main()
