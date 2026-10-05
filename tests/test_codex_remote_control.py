"""Codex Remote Control service and routes, against fake codex-cli 0.152.x output."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent.adapters.codex import remote_control as rc
from services.cowork_agent.adapters.codex import routes as codex_routes
from services.cowork_agent.adapters.loader import try_load_capability
from utils.commands import CommandResult

BIN = "/opt/codex/bin/codex"

START_PAYLOAD = {
    "mode": "daemon",
    "status": "connected",
    "serverName": "devbox",
    "environmentId": "env_123",
    "timedOut": False,
    "daemon": {
        "status": "bootstrapped",
        "backend": "pid",
        "remoteControlEnabled": True,
        "managedCodexPath": "/home/u/.codex/packages/standalone/current/codex",
        "managedCodexVersion": "0.152.0",
        "socketPath": "/home/u/.codex/app-server-daemon/app-server-control.sock",
        "cliVersion": "0.152.0",
        "appServerVersion": "0.152.0",
    },
}
PAIR_PAYLOAD = {
    "pairingCode": "RAW-CODE-1234",
    "manualPairingCode": "ABCD-EFGH",
    "environmentId": "env_123",
    "expiresAt": 1_800_000_600,
}
RUNNING_PROBE = {"status": "running", "appServerVersion": "0.152.0", "cliVersion": "0.152.0"}
DAEMON_DOWN = (
    "Error: failed to connect to /home/u/.codex/app-server-daemon/app-server-control.sock\n"
    "\n"
    "Caused by:\n"
    "    Connection refused (os error 111)\n"
)


def _result(output: str = "", returncode: int = 0, **extra) -> CommandResult:
    return CommandResult(argv=[BIN], returncode=returncode, output=output, duration_seconds=0.0, **extra)


def _json(payload: dict) -> CommandResult:
    return _result(json.dumps(payload) + "\n")


class FakeCli:
    """Stands in for ``rc._run``, keyed by the args (``remote-control start --json``
    → ``remote_control_start``)."""

    def __init__(self, **by_args: CommandResult) -> None:
        self.by_args = by_args
        self.calls: list[tuple[list[str], float]] = []
        self.unlogged: list[str] = []  # commands run with log_output=False

    async def __call__(self, argv: list[str], *, timeout: float, log_output: bool = True) -> CommandResult:
        self.calls.append((argv, timeout))
        if not log_output:
            self.unlogged.append(" ".join(argv[1:]))
        words = " ".join(arg for arg in argv[1:] if arg != "--json")
        return self.by_args["_".join(words.replace("-", " ").split())]

    @property
    def commands(self) -> list[str]:
        return [" ".join(argv[1:]) for argv, _ in self.calls]


class _IsolatedState:
    """Each test starts with no remembered enrollment and a private CODEX_HOME."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for patcher in (
            mock.patch.object(rc, "codex_home", return_value=self.home),
            mock.patch.dict(os.environ, {"CODEX_CLI_PATH": "", "CODEX_REMOTE_CONTROL_TIMEOUT": ""}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        rc._last_enrollment = None
        self.addCleanup(setattr, rc, "_last_enrollment", None)


# ── CLI output helpers ───────────────────────────────────────────────────


class ParseJsonObjectTests(unittest.TestCase):
    def test_picks_the_last_json_object_among_warnings(self) -> None:
        output = 'WARN something\n{"status": "old"}\nnot json {\n{"status": "connected"}\n'
        self.assertEqual(rc.parse_json_object(output), {"status": "connected"})

    def test_ignores_non_objects_and_broken_lines(self) -> None:
        self.assertIsNone(rc.parse_json_object('[1, 2]\n{"broken": \nplain text\n'))
        self.assertIsNone(rc.parse_json_object(""))


class CollapseCliErrorTests(unittest.TestCase):
    def test_folds_caused_by_and_drops_error_prefix(self) -> None:
        self.assertEqual(
            rc.collapse_cli_error(DAEMON_DOWN, fallback="x"),
            "failed to connect to /home/u/.codex/app-server-daemon/app-server-control.sock "
            "(caused by: Connection refused (os error 111))",
        )

    def test_drops_json_lines_and_uses_fallback_when_empty(self) -> None:
        self.assertEqual(rc.collapse_cli_error('{"pairingCode": "X"}\n\n', fallback="none"), "none")

    def test_caps_length(self) -> None:
        text = rc.collapse_cli_error("e" * 1000, fallback="")
        self.assertEqual(len(text), rc._MAX_DETAIL_CHARS)
        self.assertTrue(text.endswith("…"))


class SmallHelperTests(unittest.TestCase):
    def test_cli_version_keeps_the_number(self) -> None:
        self.assertEqual(rc._cli_version(_result("codex-cli 0.152.0\n")), "0.152.0")
        self.assertIsNone(rc._cli_version(_result("", returncode=1)))
        self.assertIsNone(rc._cli_version(_result("   \n")))

    def test_expiry_in_seconds_or_milliseconds(self) -> None:
        with mock.patch.object(rc.time, "time", return_value=1_800_000_000):
            for value in (1_800_000_600, 1_800_000_600_000):
                with self.subTest(value=value):
                    self.assertEqual(rc._expiry(value), ("2027-01-15T08:10:00Z", 600))
            self.assertEqual(rc._expiry(1_799_999_000)[1], 0)  # already expired → floored
        for bad in (None, "soon", True):
            with self.subTest(bad=bad):
                self.assertEqual(rc._expiry(bad), (None, None))

    def test_action_timeout_env_override(self) -> None:
        cases = {"": 90.0, "30": 30.0, "abc": 90.0, "-5": 90.0, "0": 90.0}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"CODEX_REMOTE_CONTROL_TIMEOUT": raw}):
                self.assertEqual(rc._action_timeout(), expected)

    def test_start_message_mirrors_cli_wording(self) -> None:
        self.assertEqual(
            rc._start_message("connected", "devbox"),
            "This machine is available for remote control as devbox.",
        )
        self.assertIn("this machine", rc._start_message("connecting", None))
        self.assertEqual(rc._start_message("weird", None), "Remote control daemon started (weird).")


# ── Binary lookup ────────────────────────────────────────────────────────


class ResolveBinaryTests(_IsolatedState, unittest.TestCase):
    def _executable(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_absolute_env_path_is_authoritative(self) -> None:
        binary = self._executable(self.home / "custom" / "codex")
        with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(binary)}), \
                mock.patch.object(rc.shutil, "which", return_value="/usr/bin/codex"):
            self.assertEqual(rc.resolve_binary(), str(binary))

    def test_missing_absolute_env_path_does_not_fall_back(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.home / "nope")}), \
                mock.patch.object(rc.shutil, "which", return_value="/usr/bin/codex"):
            self.assertIsNone(rc.resolve_binary())

    def test_path_lookup(self) -> None:
        with mock.patch.object(rc.shutil, "which", return_value="/usr/bin/codex"):
            self.assertEqual(rc.resolve_binary(), "/usr/bin/codex")

    def test_standalone_install_when_path_cannot_see_it(self) -> None:
        binary = self._executable(self.home / "packages" / "standalone" / "current" / "codex")
        with mock.patch.object(rc.shutil, "which", return_value=None), \
                mock.patch("pathlib.Path.home", return_value=self.home / "empty-home"):
            self.assertEqual(rc.resolve_binary(), str(binary))

    def test_none_when_nothing_found(self) -> None:
        with mock.patch.object(rc.shutil, "which", return_value=None), \
                mock.patch("pathlib.Path.home", return_value=self.home / "empty-home"):
            self.assertIsNone(rc.resolve_binary())


# ── Failure classification + on-disk daemon facts ────────────────────────


class FailureClassificationTests(unittest.TestCase):
    def _code(self, result: CommandResult) -> rc.RemoteControlError:
        with self.assertRaises(rc.RemoteControlError) as ctx:
            rc._raise_for_failure(result, "remote-control start", 90.0)
        return ctx.exception

    def test_success_does_not_raise(self) -> None:
        rc._raise_for_failure(_result("{}"), "remote-control start", 90.0)

    def test_each_failure_maps_to_its_code(self) -> None:
        cases = {
            "cli_missing": _result(returncode=-1, binary_missing=True),
            "timeout": _result(returncode=-9, timed_out=True),
            "cli_error": _result(returncode=-1, exception="boom"),
            "daemon_not_running": _result(DAEMON_DOWN, returncode=1),
            "unsupported_platform": _result(
                "Error: the app-server daemon is only supported on Unix platforms", returncode=1
            ),
        }
        for code, result in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self._code(result).code, code)

    def test_other_exit_codes_carry_the_cli_text(self) -> None:
        exc = self._code(_result("Error: not logged in", returncode=2))
        self.assertEqual((exc.code, exc.message), ("cli_error", "not logged in"))

    def test_as_response_shape(self) -> None:
        self.assertEqual(
            rc.RemoteControlError("daemon_not_running", "Start it.", cli_output="sock").as_response(),
            {"ok": False, "error": "daemon_not_running", "detail": "Start it.",
             "cli_output": "sock", "running": False},
        )
        self.assertNotIn("running", rc.RemoteControlError("timeout", "slow").as_response())


class LivePidTests(_IsolatedState, unittest.TestCase):
    def _write_pid(self, pid) -> None:
        daemon_dir = self.home / "app-server-daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        (daemon_dir / "app-server.pid").write_text(json.dumps({"pid": pid}), encoding="utf-8")

    def test_live_process(self) -> None:
        self._write_pid(os.getpid())
        self.assertEqual(rc._live_pid(), os.getpid())

    def test_stale_file_after_crash(self) -> None:
        self._write_pid(4242)
        with mock.patch.object(rc.os, "kill", side_effect=ProcessLookupError):
            self.assertIsNone(rc._live_pid())

    def test_process_owned_by_someone_else_is_alive(self) -> None:
        self._write_pid(4242)
        with mock.patch.object(rc.os, "kill", side_effect=PermissionError):
            self.assertEqual(rc._live_pid(), 4242)

    def test_missing_or_invalid_records(self) -> None:
        self.assertIsNone(rc._live_pid())
        for bad in (True, -3, "123", None):
            with self.subTest(bad=bad):
                self._write_pid(bad)
                self.assertIsNone(rc._live_pid())


# ── Public API ───────────────────────────────────────────────────────────


class _ApiCase(_IsolatedState, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch.object(rc, "resolve_binary", return_value=BIN),
            mock.patch.object(rc, "codex_oauth_connected", return_value=True),
            mock.patch.object(rc, "_live_pid", return_value=4242),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_cli(self, **by_args: CommandResult) -> FakeCli:
        fake = FakeCli(**by_args)
        patcher = mock.patch.object(rc, "_run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class GetStatusTests(_ApiCase):
    async def test_running_daemon(self) -> None:
        cli = self.use_cli(version=_result("codex-cli 0.152.0\n"),
                           app_server_daemon_version=_json(RUNNING_PROBE))
        status = await rc.get_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], 4242)
        self.assertEqual(status["cli"], {"available": True, "path": BIN, "version": "0.152.0"})
        self.assertEqual(status["daemon"]["status"], "running")
        self.assertEqual(status["daemon"]["app_server_version"], "0.152.0")
        self.assertIsNone(status["session_url"])
        self.assertTrue(status["login_present"])
        self.assertEqual(status["agent"], "codex")
        self.assertTrue(status["pairing"]["supported"])
        # Read-only: only the two probes ran, with the short timeout.
        self.assertEqual(sorted(cli.commands), ["--version", "app-server daemon version"])
        self.assertTrue(all(timeout == rc.PROBE_TIMEOUT_SECONDS for _, timeout in cli.calls))

    async def test_refused_socket_is_stopped_and_forgets_enrollment(self) -> None:
        rc._last_enrollment = {"server_name": "devbox"}
        self.use_cli(version=_result("codex-cli 0.152.0\n"),
                     app_server_daemon_version=_result(DAEMON_DOWN, returncode=1))
        status = await rc.get_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["daemon"]["status"], "stopped")
        self.assertIsNone(status["daemon"]["error"])
        self.assertIsNone(status["enrollment"])
        self.assertIsNone(rc._last_enrollment)

    async def test_probe_timeout_is_unknown(self) -> None:
        self.use_cli(version=_result("codex-cli 0.152.0\n"),
                     app_server_daemon_version=_result(returncode=-9, timed_out=True))
        status = await rc.get_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["daemon"]["status"], "unknown")
        self.assertIn("timed out", status["daemon"]["error"])

    async def test_missing_cli(self) -> None:
        rc._last_enrollment = {"server_name": "devbox"}
        cli = self.use_cli()
        with mock.patch.object(rc, "resolve_binary", return_value=None):
            status = await rc.get_status()
        self.assertEqual(status["cli"], {"available": False, "path": None, "version": None})
        self.assertEqual(status["daemon"]["status"], "unknown")
        self.assertFalse(status["running"])
        self.assertIsNone(rc._last_enrollment)
        self.assertEqual(cli.calls, [])

    async def test_status_keeps_the_name_from_the_last_start(self) -> None:
        self.use_cli(remote_control_start=_json(START_PAYLOAD),
                     version=_result("codex-cli 0.152.0\n"),
                     app_server_daemon_version=_json(RUNNING_PROBE))
        await rc.start()
        status = await rc.get_status()
        self.assertEqual(status["name"], "devbox")
        self.assertEqual(status["enrollment"]["environment_id"], "env_123")


class StartTests(_ApiCase):
    async def test_connected(self) -> None:
        cli = self.use_cli(remote_control_start=_json(START_PAYLOAD))
        response = await rc.start(name="ignored")
        self.assertTrue(response["ok"])
        self.assertFalse(response["already_running"])
        self.assertTrue(response["running"])
        self.assertEqual(response["name"], "devbox")
        self.assertEqual(response["message"], "This machine is available for remote control as devbox.")
        self.assertEqual(response["enrollment"]["connection_status"], "connected")
        self.assertTrue(response["daemon"]["remote_control_enabled"])
        self.assertEqual(response["cli"]["version"], "0.152.0")
        # `name` is accepted for parity but never reaches the CLI.
        self.assertEqual(cli.calls, [([BIN, "remote-control", "start", "--json"], 90.0)])

    async def test_already_running(self) -> None:
        payload = {**START_PAYLOAD, "daemon": {**START_PAYLOAD["daemon"], "status": "alreadyRunning"}}
        self.use_cli(remote_control_start=_json(payload))
        self.assertTrue((await rc.start())["already_running"])

    async def test_warning_before_json_is_tolerated(self) -> None:
        self.use_cli(remote_control_start=_result("WARN slow disk\n" + json.dumps(START_PAYLOAD)))
        self.assertTrue((await rc.start())["ok"])

    async def test_missing_cli(self) -> None:
        cli = self.use_cli()
        with mock.patch.object(rc, "resolve_binary", return_value=None):
            response = await rc.start()
        self.assertEqual((response["ok"], response["error"], response["running"]), (False, "cli_missing", False))
        self.assertEqual(cli.calls, [])

    async def test_no_json_is_bad_output(self) -> None:
        self.use_cli(remote_control_start=_result("started, maybe\n"))
        response = await rc.start()
        self.assertEqual((response["ok"], response["error"]), (False, "bad_output"))
        self.assertIsNone(rc._last_enrollment)

    async def test_timeout(self) -> None:
        self.use_cli(remote_control_start=_result(returncode=-9, timed_out=True))
        with mock.patch.dict(os.environ, {"CODEX_REMOTE_CONTROL_TIMEOUT": "5"}):
            response = await rc.start()
        self.assertEqual(response["error"], "timeout")
        self.assertIn("5s", response["detail"])


class PairTests(_ApiCase):
    async def test_manual_code_preferred(self) -> None:
        self.use_cli(remote_control_pair=_json(PAIR_PAYLOAD))
        with mock.patch.object(rc.time, "time", return_value=1_800_000_000):
            response = await rc.pair()
        self.assertEqual(response, {
            "ok": True,
            "manual_pairing_code": "ABCD-EFGH",
            "pairing_code": "RAW-CODE-1234",
            "environment_id": "env_123",
            "expires_at": "2027-01-15T08:10:00Z",
            "expires_in_seconds": 600,
            "instructions": rc.PAIRING_INSTRUCTIONS,
        })

    async def test_raw_code_only(self) -> None:
        self.use_cli(remote_control_pair=_json({"pairingCode": "RAW-ONLY"}))
        response = await rc.pair()
        self.assertEqual((response["manual_pairing_code"], response["pairing_code"]), ("RAW-ONLY", "RAW-ONLY"))
        self.assertIsNone(response["expires_at"])

    async def test_daemon_down(self) -> None:
        self.use_cli(remote_control_pair=_result(DAEMON_DOWN, returncode=1))
        response = await rc.pair()
        self.assertEqual((response["ok"], response["error"], response["running"]),
                         (False, "daemon_not_running", False))

    async def test_no_code_is_bad_output_without_cli_text(self) -> None:
        self.use_cli(remote_control_pair=_json({"environmentId": "env_123"}))
        response = await rc.pair()
        self.assertEqual((response["ok"], response["error"]), (False, "bad_output"))
        self.assertNotIn("cli_output", response)

    async def test_only_pair_keeps_its_output_out_of_the_log(self) -> None:
        cli = self.use_cli(remote_control_pair=_json(PAIR_PAYLOAD),
                           remote_control_start=_json(START_PAYLOAD),
                           remote_control_stop=_json({"status": "stopped"}),
                           version=_result("codex-cli 0.152.0\n"),
                           app_server_daemon_version=_json(RUNNING_PROBE))
        await rc.pair()
        await rc.start()
        await rc.stop()
        await rc.get_status()
        self.assertEqual(cli.unlogged, ["remote-control pair --json"])

    async def test_code_never_reaches_the_command_log(self) -> None:
        """Through the real runner: commands.log records pair, never the code."""
        script = self.home / "bin" / "codex"
        script.parent.mkdir(parents=True)
        script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(PAIR_PAYLOAD)}\nEOF\n", encoding="utf-8")
        script.chmod(0o755)
        state_root = self.home / ".quirq"
        env = {"QUIRQ_STATE_ROOT": str(state_root), "QUIRQ_COMMAND_LOG": "", "QUIRQ_COMMAND_LOG_PATH": ""}
        with mock.patch.object(rc, "resolve_binary", return_value=str(script)), \
                mock.patch.dict(os.environ, env):
            response = await rc.pair()
        self.assertEqual(response["manual_pairing_code"], "ABCD-EFGH")
        logged = (state_root / "inbox" / "activity" / "commands.log").read_text(encoding="utf-8")
        self.assertIn("remote-control pair --json", logged)
        for code in ("ABCD-EFGH", "RAW-CODE-1234"):
            self.assertNotIn(code, logged)

    async def test_failure_never_echoes_a_code(self) -> None:
        output = json.dumps(PAIR_PAYLOAD) + "\nError: enrollment rejected\n"
        self.use_cli(remote_control_pair=_result(output, returncode=1))
        response = await rc.pair()
        self.assertFalse(response["ok"])
        body = json.dumps(response)
        for code in ("ABCD-EFGH", "RAW-CODE-1234"):
            self.assertNotIn(code, body)


class StopTests(_ApiCase):
    async def test_stopped(self) -> None:
        rc._last_enrollment = {"server_name": "devbox"}
        self.use_cli(remote_control_stop=_json({"status": "stopped"}))
        response = await rc.stop()
        self.assertEqual(
            {k: response[k] for k in ("ok", "running", "was_running", "raw_status", "message")},
            {"ok": True, "running": False, "was_running": True, "raw_status": "stopped",
             "message": "Remote control stopped."},
        )
        self.assertEqual(response["daemon"]["status"], "stopped")
        self.assertIsNone(rc._last_enrollment)

    async def test_not_running_is_still_ok(self) -> None:
        self.use_cli(remote_control_stop=_json({"status": "notRunning"}))
        response = await rc.stop()
        self.assertTrue(response["ok"])
        self.assertFalse(response["was_running"])
        self.assertEqual(response["message"], "Remote control was not running.")

    async def test_unsupported_platform(self) -> None:
        self.use_cli(remote_control_stop=_result(
            "Error: the app-server daemon is only supported on Unix platforms", returncode=1))
        response = await rc.stop()
        self.assertEqual((response["ok"], response["error"]), (False, "unsupported_platform"))
        self.assertIn("Unix", response["cli_output"])


# ── Routes ───────────────────────────────────────────────────────────────


class RoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(codex_routes.router)
        self.client = TestClient(app)

    def test_loader_mounts_the_routes_package(self) -> None:
        module = try_load_capability("routes", agent="codex")
        self.assertIs(module, codex_routes)
        mounted = {(path, method) for route in module.router.routes
                   for path in [route.path] for method in route.methods}
        self.assertEqual(mounted, {
            ("/api/remote-control/status", "GET"),
            ("/api/remote-control/start", "POST"),
            ("/api/remote-control/pair", "POST"),
            ("/api/remote-control/stop", "POST"),
        })

    def test_status(self) -> None:
        with mock.patch.object(rc, "get_status", mock.AsyncMock(return_value={"running": True})) as status:
            response = self.client.get("/api/remote-control/status")
        self.assertEqual((response.status_code, response.json()), (200, {"running": True}))
        status.assert_awaited_once_with()

    def test_start_with_and_without_body(self) -> None:
        with mock.patch.object(rc, "start", mock.AsyncMock(return_value={"ok": True})) as start:
            self.assertEqual(self.client.post("/api/remote-control/start", json={"name": "mac"}).status_code, 200)
            self.assertEqual(self.client.post("/api/remote-control/start").status_code, 200)
        self.assertEqual(start.await_args_list, [mock.call(name="mac"), mock.call(name=None)])

    def test_pair_and_stop(self) -> None:
        with mock.patch.object(rc, "pair", mock.AsyncMock(return_value={"ok": True, "manual_pairing_code": "C"})), \
                mock.patch.object(rc, "stop", mock.AsyncMock(return_value={"ok": True, "running": False})):
            self.assertEqual(self.client.post("/api/remote-control/pair").json()["manual_pairing_code"], "C")
            self.assertFalse(self.client.post("/api/remote-control/stop").json()["running"])

    def test_expected_failures_are_http_200(self) -> None:
        failure = {"ok": False, "error": "daemon_not_running", "detail": "Start it first.", "running": False}
        with mock.patch.object(rc, "pair", mock.AsyncMock(return_value=failure)):
            response = self.client.post("/api/remote-control/pair")
        self.assertEqual((response.status_code, response.json()), (200, failure))


if __name__ == "__main__":
    unittest.main()
