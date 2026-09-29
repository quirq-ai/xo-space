"""The state root's folders, and a store that adopts its own old file."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.storage import layout


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)


class FolderTests(_Sandbox):
    def test_every_folder_is_under_the_state_root(self) -> None:
        folders = {
            layout.inbox_dir(): "inbox", layout.sharing_dir(): "sharing",
            layout.usage_dir(): "usage", layout.settings_dir(): "settings",
            layout.secrets_dir(): "secrets", layout.cache_dir(): "cache",
            layout.logs_dir(): "logs", layout.locks_dir(): ".locks",
        }
        for path, name in folders.items():
            self.assertEqual(path, self.root / name)

    def test_logs_are_defined_once_below_the_services_layer(self) -> None:
        from utils import runtime_env
        from utils.commands import scheduler

        self.assertIs(layout.logs_dir, runtime_env.logs_dir)
        self.assertIs(layout.inbox_activity_dir, runtime_env.inbox_activity_dir)
        self.assertEqual(layout.inbox_activity_dir(), layout.inbox_dir() / "activity")
        self.assertEqual(scheduler.log_file("job1"), self.root / "logs" / "scheduler" / "job1.log")


class UsageWatermarkTests(unittest.TestCase):
    """The watermark used to live in the checkout; its store adopts it once."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.new = base / "usage" / "stub.json"
        self.old = base / "data" / "stub" / "usage_sync_state.json"
        self.old.parent.mkdir(parents=True)
        self.old.write_text('{"last_synced_date": "2026-09-01"}', encoding="utf-8")

    def test_the_checkout_watermark_is_adopted_on_first_read(self) -> None:
        from services import usage_sync

        with patch.multiple(usage_sync, SYNC_STATE_FILE=str(self.new),
                            _DEFAULT_WATERMARK_PATH=str(self.new),
                            _LEGACY_WATERMARK_PATH=str(self.old)):
            self.assertEqual(usage_sync._load_sync_state()["last_synced_date"], "2026-09-01")
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.is_file())

    def test_an_explicit_override_leaves_the_old_file_alone(self) -> None:
        from services import usage_sync

        with patch.multiple(usage_sync, SYNC_STATE_FILE=str(self.new.with_name("override.json")),
                            _DEFAULT_WATERMARK_PATH=str(self.new),
                            _LEGACY_WATERMARK_PATH=str(self.old)):
            self.assertEqual(usage_sync._load_sync_state(), {})
        self.assertTrue(self.old.exists())

    def test_the_default_path_is_under_the_state_root_per_agent(self) -> None:
        from services import usage_sync

        self.assertEqual(Path(usage_sync._DEFAULT_WATERMARK_PATH).parent.name, "usage")


if __name__ == "__main__":
    unittest.main()
