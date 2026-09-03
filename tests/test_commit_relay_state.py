from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.commit_relay import state

REPO = "github.com/acme/trip-planner"


class CommitRelayStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / ".quirq"
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_files_land_under_quirq_relay(self) -> None:
        state.save_cursor(REPO, 7)
        files = list((self.root / "relay").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), {"cursor": 7})

    def test_fields_do_not_clobber_each_other(self) -> None:
        state.save_cursor(REPO, 5)
        state.save_last_reported(REPO, "abc123")
        state.save_cursor(REPO, 9)
        self.assertEqual(state.load_cursor(REPO), 9)
        self.assertEqual(state.load_last_reported(REPO), "abc123")

    def test_missing_state_reads_as_empty(self) -> None:
        self.assertEqual(state.load_cursor(REPO), 0)
        self.assertIsNone(state.load_last_reported(REPO))

    def test_corrupt_file_reads_as_empty(self) -> None:
        path = state.state_path(REPO)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(state.load_cursor(REPO), 0)
        self.assertIsNone(state.load_last_reported(REPO))
        state.save_cursor(REPO, 3)  # recovers by overwriting
        self.assertEqual(state.load_cursor(REPO), 3)

    def test_keyed_on_identity_not_folder(self) -> None:
        # A folder rename changes nothing the relay reads: the key is the origin.
        state.save_cursor(REPO, 11)
        self.assertEqual(state.load_cursor(REPO), 11)
        self.assertEqual(state.load_cursor("github.com/acme/other"), 0)
