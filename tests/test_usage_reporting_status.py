from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import services.usage_sync as usage_sync


class UsageReportingStatusTests(unittest.TestCase):
    """usage_reporting_status() composes 'is anything reported' for Setup.

    Hermetic: the sync state file is pointed at a temp path, and the auth
    token comes from a patched routers.auth.auth.get_auth_token — no real
    ~/.quirq, .env, or network.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = str(Path(self._tmp.name) / "usage_sync_state.json")
        patcher = mock.patch.object(usage_sync, "SYNC_STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _status(self, token: str | None) -> dict:
        with mock.patch("routers.auth.auth.get_auth_token", return_value=token):
            return usage_sync.usage_reporting_status()

    def test_off_without_a_key(self) -> None:
        status = self._status(None)
        self.assertEqual(status["status"], "off")

    def test_pending_with_a_key_but_no_probe(self) -> None:
        status = self._status("some-key")
        self.assertEqual(status["status"], "pending")
        self.assertIsNone(status["probe_outcome"])

    def test_on_when_the_last_probe_accepted(self) -> None:
        state = {"last_synced_date": "2026-08-27"}
        usage_sync._record_key_probe(state, "accepted", 200)

        status = self._status("some-key")
        self.assertEqual(status["status"], "on")
        self.assertEqual(status["probe_outcome"], "accepted")
        self.assertEqual(status["last_synced_date"], "2026-08-27")
        self.assertIsNotNone(status["probe_at"])

    def test_blocked_when_the_last_probe_was_rejected(self) -> None:
        usage_sync._record_key_probe({}, "rejected", 401)

        status = self._status("some-key")
        self.assertEqual(status["status"], "blocked")

    def test_pending_when_the_probe_was_inconclusive(self) -> None:
        usage_sync._record_key_probe({}, "unverified", None)

        status = self._status("some-key")
        self.assertEqual(status["status"], "pending")

    def test_off_wins_even_with_a_stale_probe(self) -> None:
        # Key removed after a probe: nothing can be sent any more, so the
        # stale probe record must not resurrect "on".
        usage_sync._record_key_probe({}, "accepted", 200)
        self.assertEqual(self._status(None)["status"], "off")

    def test_probe_record_persists_alongside_the_watermark(self) -> None:
        state = {"last_synced_date": "2026-08-20"}
        usage_sync._record_key_probe(state, "accepted", 200)

        on_disk = json.loads(Path(self.state_file).read_text())
        self.assertEqual(on_disk["last_synced_date"], "2026-08-20")
        self.assertEqual(on_disk["key_probe"]["outcome"], "accepted")
        self.assertEqual(on_disk["key_probe"]["status"], 200)


if __name__ == "__main__":
    unittest.main()
