"""Model-free questions to the claude CLI (storm ch 02 §7): its version, its
login, and an ``initialize`` round trip (models, account, capabilities). Each
runs a short-lived process through the one executor, with the session env
whitelist, and is killed with its group on timeout or cancel.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters.contract import Launcher, RuntimeFailure

PROBE_TIMEOUT_S = 30.0
INITIALIZE_PROBE_TIMEOUT_S = 60.0
KILL_GRACE_S = 2.0


async def run(launcher: Launcher, argv: Sequence[str], *, env: Mapping[str, str], cwd: str,
              timeout: float = PROBE_TIMEOUT_S) -> tuple[int, list[bytes]]:
    """Run ``argv`` to its end: (exit code, stdout lines)."""
    try:
        handle = await launcher.spawn(argv, cwd=cwd, env=env, label="claude probe")
    except FileNotFoundError as exc:
        raise RuntimeFailure("cli_missing", f"the claude CLI was not found ({argv[0]}); install it or point "
                                            "CLAUDE_CLI_PATH at it") from exc

    async def collect() -> tuple[int, list[bytes]]:
        lines = [line async for line in handle.lines()]
        return await handle.wait(), lines

    try:
        return await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure("timeout", f"claude {' '.join(argv[1:3])} didn't finish within {timeout:g} s",
                             retryable=True) from exc
    finally:
        await handle.terminate(KILL_GRACE_S)


async def initialize(launcher: Launcher, binary: str, *, env: Mapping[str, str]) -> dict[str, Any]:
    """The ``initialize`` reply of a process that never gets a prompt (no
    model call, no transcript), started in an empty folder with none of the
    person's MCP servers (``--strict-mcp-config`` and no ``--mcp-config``).
    Their settings still load: they can carry the login."""
    argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--session-id", str(uuid.uuid4()), "--tools", "", "--strict-mcp-config"]
    with tempfile.TemporaryDirectory(prefix="xo-claude-probe-") as cwd:
        try:
            handle = await launcher.spawn(argv, cwd=cwd, env=env, label="claude initialize probe")
        except FileNotFoundError as exc:
            raise RuntimeFailure("cli_missing", f"the claude CLI was not found ({binary})") from exc
        try:
            request = {"type": "control_request", "request_id": "probe", "request": {"subtype": "initialize"}}
            await handle.write((json.dumps(request) + "\n").encode())
            return await asyncio.wait_for(_reply(handle), INITIALIZE_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", "claude didn't answer initialize", retryable=True) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeFailure("process_died", "claude exited before answering initialize") from exc
        finally:
            await handle.terminate(KILL_GRACE_S)


async def _reply(handle: Any) -> dict[str, Any]:
    async for raw in handle.lines():
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("type") == "control_response":
            response = msg.get("response") if isinstance(msg.get("response"), dict) else {}
            if response.get("subtype") == "error":
                raise RuntimeFailure("protocol", f"claude refused initialize: {response.get('error')}")
            body = response.get("response")
            return body if isinstance(body, dict) else {}
    raise RuntimeFailure("process_died", "claude exited before answering initialize")
