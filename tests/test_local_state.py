from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import local_state, xo_cowork_state
from services.cowork_agent.visualizer import flock, state
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore


class LocalStateTests(unittest.TestCase):
    def test_canonical_roots_distinguish_quirq_from_legacy(self) -> None:
        self.assertEqual(local_state.quirq_state_dir().name, ".quirq")
        self.assertEqual(local_state.legacy_state_dir().name, ".xo-cowork")
        self.assertEqual(state.watcher_state_dir().parts[-2:], (".quirq", "watcher"))

    def test_quirq_root_can_be_explicitly_mounted(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": "/mounted/quirq"}):
            self.assertEqual(
                local_state.quirq_state_dir(),
                Path("/mounted/quirq"),
            )

    def test_onboarding_state_migrates_to_quirq_without_rewriting_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quirq_dir = root / ".quirq"
            quirq_file = quirq_dir / "state.json"
            legacy_file = root / ".xo-cowork" / "state.json"
            legacy_file.parent.mkdir(parents=True)
            original = {
                "onboarding_completed": True,
                "onboarding_completed_at": "2026-07-25T00:00:00Z",
            }
            legacy_file.write_text(json.dumps(original), encoding="utf-8")

            with (
                patch.object(xo_cowork_state, "STATE_DIR", quirq_dir),
                patch.object(xo_cowork_state, "STATE_FILE", quirq_file),
                patch.object(xo_cowork_state, "LEGACY_STATE_FILE", legacy_file),
            ):
                loaded = xo_cowork_state.get_state()

            self.assertEqual(loaded, original)
            self.assertEqual(json.loads(quirq_file.read_text(encoding="utf-8")), original)
            self.assertEqual(json.loads(legacy_file.read_text(encoding="utf-8")), original)

    def test_jsonl_offsets_migrate_to_quirq_on_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_file = root / ".quirq" / "watcher" / "offsets.json"
            legacy_file = root / ".xo-cowork" / "watcher" / "offsets.json"
            native_log = Path("/runtime/sessions/session.jsonl")
            legacy_file.parent.mkdir(parents=True)
            legacy_file.write_text(
                json.dumps({
                    "version": 1,
                    "offsets": {
                        str(native_log): {"offset": 42, "inode": 7},
                    },
                }),
                encoding="utf-8",
            )

            store = OffsetStore(new_file, legacy_store_path=legacy_file)
            self.assertEqual(store.get(native_log), (42, 7))
            store.flush()

            migrated = json.loads(new_file.read_text(encoding="utf-8"))
            self.assertEqual(migrated["offsets"][str(native_log)], {
                "offset": 42,
                "inode": 7,
            })

    def test_shared_file_locks_live_under_quirq(self) -> None:
        """Lock sentinels never land in ``.xo/``.

        Both locked files are checked: ``sessions-augment.json``, which
        the watcher tick and the todos API both write, and
        ``todos.json``, which the API owns but several concurrent
        requests can reach at once.
        """
        watcher_root = Path("/machine/.quirq/watcher")
        for data_path in (
            Path("/projects/demo/.xo/sessions/sessions-augment.json"),
            Path("/projects/demo/.xo/todos.json"),
        ):
            with patch.object(flock, "watcher_state_dir", return_value=watcher_root):
                lock_path = flock._lock_path_for(data_path)
            self.assertEqual(lock_path.parent, watcher_root / "locks")
            self.assertTrue(lock_path.name.startswith(data_path.name + "."))


if __name__ == "__main__":
    unittest.main()
