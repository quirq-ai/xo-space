"""Native protocol, lifecycle boundaries and credential handling regressions."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.asyncio.server import unix_serve

from services.cowork_agent.adapters.codex import remote_control as rc
from services.cowork_agent.adapters.codex.auth import chatgpt_connected
from services.cowork_agent.adapters.codex.routes import router
from utils.commands import CommandResult


def result(data=None, **kwargs):
    return CommandResult(argv=["codex"], returncode=kwargs.pop("returncode", 0),
                         output=json.dumps(data or {}), duration_seconds=0, **kwargs)


class RemoteControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "OPENAI_API_KEY": "not-for-remote"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def login(self):
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fixture-token"}}))

    def test_native_login_ignores_api_keys_and_bad_files(self):
        for payload in ["broken", "null", "[]", '{"OPENAI_API_KEY":"key"}', '{"tokens":{"access_token":""}}']:
            (self.home / "auth.json").write_text(payload)
            self.assertFalse(chatgpt_connected())
        self.login()
        self.assertTrue(chatgpt_connected())

    async def test_start_requires_login_without_spawning(self):
        with patch.object(rc, "run", AsyncMock()) as command:
            with self.assertRaises(rc.RemoteControlError) as error:
                await rc.start()
            self.assertEqual(error.exception.status_code, 409)
            command.assert_not_called()

    async def test_start_uses_native_daemon_and_strips_api_key(self):
        self.login()
        command = AsyncMock(return_value=result({"status": "connected", "serverName": "space"}))
        with patch.object(rc, "run", command):
            data = await rc.start()
        self.assertTrue(data["running"])
        self.assertEqual(data["name"], "space")
        args, options = command.call_args
        self.assertEqual(args[0], ["codex", "remote-control", "start", "--json"])
        self.assertTrue(options["sensitive_output"])
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.home))

    async def test_failed_and_timed_out_commands_are_errors(self):
        self.login()
        for response in [result(returncode=1), result(returncode=-9, timed_out=True), result({"status": "errored"}), result({"status": "surprise"})]:
            with patch.object(rc, "run", AsyncMock(return_value=response)):
                with self.assertRaises(rc.RemoteControlError):
                    await rc.start()

    async def test_stop_is_idempotent_without_login(self):
        for state in ["stopped", "notRunning"]:
            with patch.object(rc, "run", AsyncMock(return_value=result({"status": state}))):
                data = await rc.stop()
            self.assertFalse(data["running"])

    async def test_pairing_only_returns_manual_code_and_expiry(self):
        code = {"pairingCode": "secret-machine-code", "manualPairingCode": "ABCD-EFGH", "expiresAt": int(time.time()) + 300, "environmentId": "private-env"}
        with patch.object(rc, "status", AsyncMock(return_value={"state": "connected"})), patch.object(rc, "run", AsyncMock(return_value=result(code))) as command:
            self.assertEqual(await rc.pair(), {"code": "ABCD-EFGH", "expires_at": code["expiresAt"]})
            self.assertTrue(command.call_args.kwargs["sensitive_output"])

    async def test_pairing_rejects_expired_or_malformed_codes(self):
        for code in [{"manualPairingCode": "ABCD-EFGH", "expiresAt": 1}, {"manualPairingCode": "<bad>", "expiresAt": int(time.time())+60}, {"manualPairingCode": "ABCD-EFGH", "expiresAt": "123"}]:
            with patch.object(rc, "status", AsyncMock(return_value={"state": "connected"})), patch.object(rc, "run", AsyncMock(return_value=result(code))):
                with self.assertRaises(rc.RemoteControlError):
                    await rc.pair()

    async def test_pairing_requires_live_connection(self):
        with patch.object(rc, "status", AsyncMock(return_value={"state": "connecting"})), patch.object(rc, "run", AsyncMock()) as command:
            with self.assertRaises(rc.RemoteControlError):
                await rc.pair()
            command.assert_not_called()

    async def test_concurrent_actions_fail_closed(self):
        async with rc._operation():
            with self.assertRaises(rc.RemoteControlError) as error:
                await rc.stop()
            self.assertEqual(error.exception.status_code, 409)

    async def test_missing_daemon_is_stopped_and_old_cli_is_unsupported(self):
        for returncode in [0, 2]:
            with patch.object(rc.shutil, "which", return_value="/bin/codex"), patch.object(rc, "run", AsyncMock(return_value=result(returncode=returncode))):
                data = await rc.status()
            self.assertFalse(data["running"])
            self.assertEqual(data["supported"], returncode == 0)

    async def test_real_unix_websocket_handshake_and_read_only_status(self):
        path = self.home / "app-server-control" / "app-server-control.sock"
        path.parent.mkdir()
        methods = []

        async def handler(ws):
            async for raw in ws:
                request = json.loads(raw)
                methods.append(request["method"])
                if request["method"] == "initialize":
                    self.assertTrue(request["params"]["capabilities"]["experimentalApi"])
                    await ws.send(json.dumps({"id": 1, "result": {"userAgent": "codex/0.153.4"}}))
                elif request["method"] == "remoteControl/status/read":
                    # Notifications may arrive before a response; they aren't that response.
                    await ws.send(json.dumps({"method": "remoteControl/status/changed", "params": {"status": "connecting"}}))
                    await ws.send(json.dumps({"id": 2, "result": {"status": "connected", "serverName": "test-space"}}))

        async with unix_serve(handler, str(path)):
            with patch.object(rc.shutil, "which", return_value="/bin/codex"), patch.object(rc, "run", AsyncMock()) as command:
                data = await rc.status()
        self.assertEqual(data["state"], "connected")
        self.assertEqual(methods, ["initialize", "initialized", "remoteControl/status/read"])
        command.assert_not_called()

    async def test_protocol_failure_is_not_reported_as_stopped(self):
        with patch.object(rc.shutil, "which", return_value="/bin/codex"), patch.object(rc, "_read_status", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(rc.RemoteControlError):
                await rc.status()


class RouteTests(unittest.TestCase):
    def test_routes_return_error_status_and_never_cache_pairing(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app, client=("127.0.0.1", 12345)) as client:
            with patch.object(rc, "start", AsyncMock(side_effect=rc.RemoteControlError("Connect ChatGPT", 409))):
                response = client.post("/api/codex/remote-control/start")
                self.assertEqual(response.status_code, 409)
            with patch.object(rc, "pair", AsyncMock(return_value={"code": "ABCD-EFGH", "expires_at": 123})):
                response = client.post("/api/codex/remote-control/pair")
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/codex/remote-control/start").status_code, 405)
        with TestClient(app, client=("198.51.100.2", 12345)) as client:
            for action in ("start", "stop", "pair"):
                self.assertEqual(client.post(f"/api/codex/remote-control/{action}").status_code, 403)
