"""Model-free questions to the claude CLI (storm ch 02 §7): its version, its
login, an ``initialize`` round trip (models, account, capabilities) and the
account's usage (``get_usage``, read by claude with its own login). Each
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


async def spawn_cli(launcher: Launcher, argv: Sequence[str], *, cwd: str, env: Mapping[str, str],
                    label: str) -> Any:
    """Start the claude CLI. Any failure to start it (missing, not executable,
    any other OS error) is ``cli_missing``, saying what to do."""
    try:
        return await launcher.spawn(argv, cwd=cwd, env=env, label=label)
    except OSError as exc:
        raise RuntimeFailure("cli_missing", f"the claude CLI can't be started ({argv[0]}: "
                                            f"{exc.strerror or exc}); install it, or point CLAUDE_CLI_PATH at "
                                            "an executable claude") from exc


async def run(launcher: Launcher, argv: Sequence[str], *, env: Mapping[str, str], cwd: str,
              timeout: float = PROBE_TIMEOUT_S) -> tuple[int, list[bytes]]:
    """Run ``argv`` to its end: (exit code, stdout lines)."""
    handle = await spawn_cli(launcher, argv, cwd=cwd, env=env, label="claude probe")

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
    return (await _control(launcher, binary, env=env, requests=()))[0]


async def usage(launcher: Launcher, binary: str, *, env: Mapping[str, str]) -> dict[str, Any]:
    """claude's own usage answer (``get_usage``, the request the Agent SDK
    sends for its usage call), on the same prompt-less process as
    ``initialize``: claude reads its account's windows with its own login,
    and XO never handles the token. ``skip_behaviors`` skips claude's scan of
    the local transcripts, which only the behaviour report needs."""
    request = {"subtype": "get_usage", "skip_behaviors": True}
    return (await _control(launcher, binary, env=env, requests=(request,)))[1]


async def _control(launcher: Launcher, binary: str, *, env: Mapping[str, str],
                   requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``initialize``, then each of ``requests`` in turn, on one prompt-less
    process: the replies, in order. A request claude refuses raises
    ``protocol``, or ``unsupported`` when claude says it doesn't know it."""
    argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--session-id", str(uuid.uuid4()), "--tools", "", "--strict-mcp-config"]
    try:
        folder = tempfile.TemporaryDirectory(prefix="xo-claude-probe-")
    except OSError as exc:
        raise RuntimeFailure("cli_missing", f"no temp folder for the claude probe: {exc.strerror or exc}") from exc
    with folder as cwd:
        handle = await spawn_cli(launcher, argv, cwd=cwd, env=env, label="claude initialize probe")
        lines = handle.lines().__aiter__()
        replies: list[dict[str, Any]] = []
        subtype = "initialize"
        try:
            for index, request in enumerate(({"subtype": "initialize"}, *requests)):
                subtype = str(request.get("subtype"))
                request_id = f"probe-{index}" if index else "probe"     # initialize keeps its old id
                message = {"type": "control_request", "request_id": request_id, "request": dict(request)}
                await handle.write((json.dumps(message) + "\n").encode())
                replies.append(await asyncio.wait_for(_reply(lines, request_id, subtype),
                                                      INITIALIZE_PROBE_TIMEOUT_S))
            return replies
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"claude didn't answer {subtype}", retryable=True) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeFailure("process_died", f"claude exited before answering {subtype}") from exc
        finally:
            await handle.terminate(KILL_GRACE_S)


async def _reply(lines: Any, request_id: str, subtype: str) -> dict[str, Any]:
    """The answer to ``request_id``; other lines (claude's own messages, an
    answer to an earlier request) are skipped."""
    async for raw in lines:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "control_response":
            continue
        response = msg.get("response") if isinstance(msg.get("response"), dict) else {}
        if response.get("request_id") not in (None, request_id):
            continue
        if response.get("subtype") == "error":
            error = str(response.get("error") or "")
            lowered = error.lower()
            code = "unsupported" if any(word in lowered for word in ("unsupported", "not supported", "unknown")) \
                else "protocol"
            raise RuntimeFailure(code, f"claude refused {subtype}: {error}")
        body = response.get("response")
        return body if isinstance(body, dict) else {}
    raise RuntimeFailure("process_died", f"claude exited before answering {subtype}")
