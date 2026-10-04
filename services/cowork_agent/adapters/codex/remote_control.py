"""Codex's managed daemon lifecycle and live, read-only Remote Control status.

CLI / protocol contract verified against openai/codex rust-v0.153.4. The CLI
owns the daemon, authentication, trust and process lifecycle. We never launch
an agent with approval/sandbox bypasses or store pairing credentials.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import os
import re
import shutil
import time

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from utils.commands import run
from .auth import chatgpt_connected
from .paths import codex_home


class RemoteControlError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _env() -> dict[str, str]:
    env = dict(os.environ, CODEX_HOME=str(codex_home()))
    # Remote Control uses the workspace's native ChatGPT login.
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    return env


async def _command(action: str) -> dict:
    result = await run(
        ["codex", "remote-control", action, "--json"],
        env=_env(), timeout=90, separate_stderr=True, sensitive_output=True,
    )
    if result.binary_missing:
        raise RemoteControlError("Install the Codex CLI in this space, then retry.", 409)
    if result.timed_out:
        raise RemoteControlError("Codex timed out. Refresh status before retrying.", 504)
    if not result.ok:
        # CLI diagnostics can include auth/pairing material. Never return or log them.
        raise RemoteControlError(
            f"Codex could not {action} remote control. Check your ChatGPT login, "
            "network connection and Codex CLI version in the space terminal."
        )
    try:
        data = json.loads(result.output)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except ValueError as exc:
        raise RemoteControlError("Unexpected Codex response. Update the CLI and retry.") from exc


@asynccontextmanager
async def _operation():
    """Fail closed on concurrent requests, including across API workers."""
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / ".xo-remote-control.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RemoteControlError("A remote-control action is in progress. Refresh and retry.", 409) from exc
        yield
    finally:
        os.close(fd)


async def _read_status() -> dict:
    # This private Unix socket is the same one used by `app-server daemon`.
    # Initializing with the experimental capability is required for status/read.
    path = codex_home() / "app-server-control" / "app-server-control.sock"
    async with asyncio.timeout(5):
        # Codex's tungstenite server rejects extension negotiation, including
        # websockets' default permessage-deflate offer.
        async with unix_connect(str(path), uri="ws://localhost/", compression=None,
                                max_size=1_048_576) as ws:
            async def request(request_id: int, method: str, params=None):
                await ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
                while True:
                    message = json.loads(await ws.recv())
                    if not isinstance(message, dict):
                        raise ValueError("Invalid response")
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise RemoteControlError("Codex status is unavailable. Update the CLI and retry.")
                        return message["result"]

            await request(1, "initialize", {
                "clientInfo": {"name": "xo_space", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            await ws.send(json.dumps({"method": "initialized"}))
            return await request(2, "remoteControl/status/read")


def _status(data: dict) -> dict:
    if not isinstance(data, dict):
        raise RemoteControlError("Unexpected remote-control status. Update the Codex CLI.")
    state = data.get("status")
    if state not in {"disabled", "connecting", "connected", "errored"}:
        raise RemoteControlError("Unexpected remote-control status. Update the Codex CLI.")
    return {
        "agent": "codex", "supported": True,
        "running": state != "disabled", "state": state,
        "login_present": chatgpt_connected(),
        "name": data.get("serverName") if isinstance(data.get("serverName"), str) else None,
    }


async def status() -> dict:
    if not shutil.which("codex"):
        return {**_status({"status": "disabled"}), "supported": False,
                "message": "Install the Codex CLI in this space to use remote control."}
    try:
        return _status(await _read_status())
    except (FileNotFoundError, ConnectionRefusedError):
        # A missing or stale socket means no daemon, not a live connection.
        help_result = await run(["codex", "remote-control", "--help"], env=_env(), timeout=10)
        if not help_result.ok:
            return {**_status({"status": "disabled"}), "supported": False,
                    "message": "Update the Codex CLI in this space to use remote control."}
        return _status({"status": "disabled"})
    except (OSError, TimeoutError, WebSocketException, ValueError, KeyError, TypeError) as exc:
        raise RemoteControlError("Could not read Codex remote-control status. Refresh and retry.") from exc


async def start() -> dict:
    async with _operation():
        if not chatgpt_connected():
            raise RemoteControlError("Connect your ChatGPT account before starting remote control.", 409)
        data = _status(await _command("start"))
        if data["state"] in {"disabled", "errored"}:
            raise RemoteControlError("Codex could not connect. Check the space terminal and retry.")
        return data


async def stop() -> dict:
    async with _operation():
        data = await _command("stop")
        if data.get("status") not in {"stopped", "notRunning"}:
            raise RemoteControlError("Codex did not confirm stopping. Refresh status and retry.")
        return _status({"status": "disabled"})


async def pair() -> dict:
    async with _operation():
        current = await status()
        if current["state"] != "connected":
            raise RemoteControlError("Wait for remote control to connect before pairing a device.", 409)
        data = await _command("pair")
        code, expires = data.get("manualPairingCode"), data.get("expiresAt")
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{4,8}-[A-Z0-9]{4,8}", code)
                or type(expires) is not int or expires <= time.time()):
            raise RemoteControlError("Codex did not return a valid pairing code. Try again.")
        return {"code": code, "expires_at": expires}
