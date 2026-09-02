"""Regression coverage for expired Claude native-session recovery (issue #56)."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.adapters.claude_code import auth_state, remote_control
from services.cowork_agent.adapters.claude_code import providers_status


class AuthStateTests(unittest.TestCase):
    def test_records_only_classified_failures_without_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            auth_state, "AUTH_FAILURE_FILE", Path(tmp) / "auth.json"
        ):
            detail = "Failed to authenticate: OAuth session expired and could not be refreshed"
            self.assertEqual(auth_state.record_auth_failure(detail), "session_expired")
            self.assertEqual(auth_state.last_auth_failure_reason(), "session_expired")
            self.assertNotIn(detail, auth_state.AUTH_FAILURE_FILE.read_text())

            auth_state.clear_auth_failure()
            self.assertIsNone(auth_state.last_auth_failure_reason())
            self.assertIsNone(auth_state.record_auth_failure("network timeout"))


class ProviderStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_session_expiry_overrides_logged_in_presence(self) -> None:
        async def build_status(*_args, **kwargs):
            return {
                "agent": "claude_code",
                "oauth": {"claude_code": {"connected": kwargs["claude_oauth_present"]()}},
                "api_keys": {},
            }

        with patch.object(
            providers_status, "claude_auth_status", new=AsyncMock(return_value={"loggedIn": True})
        ), patch.object(
            providers_status, "last_auth_failure_reason", return_value="session_expired"
        ), patch.object(
            providers_status, "build_providers_status", side_effect=build_status
        ):
            result = await providers_status.get_providers_status()

        self.assertEqual(
            result["oauth"]["claude_code"],
            {"connected": False, "reason": "session_expired"},
        )


class RemoteControlTests(unittest.TestCase):
    def _state_paths(self, root: Path):
        return patch.multiple(
            remote_control,
            PID_FILE=root / "pid",
            URL_FILE=root / "url",
            ERR_FILE=root / "err",
            NAME_FILE=root / "name",
            _STATE_FILES=(root / "pid", root / "url", root / "err", root / "name"),
        )

    def test_status_exposes_dead_launcher_error_and_records_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self._state_paths(Path(tmp)), patch.object(
            auth_state, "AUTH_FAILURE_FILE", Path(tmp) / "auth.json"
        ), patch.object(remote_control, "native_login_present", return_value=True), patch.object(
            remote_control, "_running_pid", return_value=None
        ):
            remote_control.ERR_FILE.write_text(
                "Failed to authenticate: OAuth session expired and could not be refreshed"
            )
            result = remote_control.status()
            recorded_reason = auth_state.last_auth_failure_reason()

        self.assertFalse(result["running"])
        self.assertIn("OAuth session expired", result["last_error"])
        self.assertEqual(recorded_reason, "session_expired")

    def test_failed_start_returns_early_exit_error_and_keeps_it_for_status(self) -> None:
        class Proc:
            pid = 123

        def fail_to_start(*_args, **kwargs):
            Path(kwargs["env"]["RC_ERR"]).write_text(
                "OAuth session expired and could not be refreshed"
            )
            return Proc()

        with tempfile.TemporaryDirectory() as tmp, self._state_paths(Path(tmp)), patch.object(
            auth_state, "AUTH_FAILURE_FILE", Path(tmp) / "auth.json"
        ), patch.object(remote_control, "_lock", return_value=nullcontext()
        ), patch.object(remote_control, "native_login_present", return_value=True), patch.object(
            remote_control, "ensure_gates_seeded"
        ), patch.object(remote_control, "resolve_binary", return_value="claude"), patch.object(
            remote_control, "_launch_dir", return_value=Path(tmp)
        ), patch.object(remote_control.subprocess, "Popen", side_effect=fail_to_start), patch.object(
            remote_control, "_running_pid", return_value=None
        ), patch.object(remote_control, "_wait_for_start", return_value=False):
            result = remote_control.start()

            self.assertEqual(result["error"], "start_failed")
            self.assertIn("OAuth session expired", result["detail"])
            self.assertTrue(remote_control.ERR_FILE.exists())
            self.assertEqual(auth_state.last_auth_failure_reason(), "session_expired")


if __name__ == "__main__":
    unittest.main()
