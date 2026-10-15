"""Capability ``oneshot_prompt``: one tool-free claude completion (storm
ch 01 §4.7, ch 10 §8).

For small jobs XO does on its own behalf (titles, a proposal's one-line why).
The run has no tools (``--tools ""``) and none of the person's MCP servers
(``--strict-mcp-config``), writes no transcript (``--no-session-persistence``),
starts in an empty temp folder (no project is loaded), and gets the prompt on
stdin, never argv. The person's settings still load: they can carry the login. It starts through the
launcher core passes, with the same env whitelist as a session.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from services.cowork_agent.adapters.claude_code.runtime import CLI_PATH_ENV, DEFAULT_BINARY
from services.cowork_agent.adapters.claude_code.runtime.env import session_env
from services.cowork_agent.adapters.claude_code.runtime.transcript import remove_empty_project_folder
from services.cowork_agent.adapters.claude_code.runtime.wire import token_usage
from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.contract import (
    Launcher,
    OneshotRequest,
    OneshotResult,
    RuntimeFailure,
    SupportItem,
    SupportReport,
)

KILL_GRACE_S = 2.0


def build_argv(binary: str, req: OneshotRequest) -> list[str]:
    argv = [binary, "--print", "--output-format", "json", "--tools", "", "--no-session-persistence",
            "--strict-mcp-config"]
    if req.model:
        argv += ["--model", req.model]
    if req.effort:
        argv += ["--effort", req.effort]
    if req.output_schema is not None:
        argv += ["--json-schema", json.dumps(req.output_schema)]
    return argv


class ClaudeOneshot:
    def check(self) -> SupportReport:
        return SupportReport(items=(
            SupportItem(feature="isolation", support="native", enforced_by="cli",
                        mechanism="--tools '' --strict-mcp-config --no-session-persistence, in an empty folder"),
            SupportItem(feature="output_schema", support="native", enforced_by="cli", mechanism="--json-schema"),
        ))

    async def complete(self, req: OneshotRequest, launcher: Launcher) -> OneshotResult:
        binary = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
        env = await asyncio.to_thread(session_env, switches={}, spec_env={}, label="claude oneshot")
        with tempfile.TemporaryDirectory(prefix="xo-oneshot-") as cwd:
            try:
                handle = await launcher.spawn(build_argv(binary, req), cwd=cwd, env=env, label="claude oneshot")
            except FileNotFoundError as exc:
                raise RuntimeFailure("cli_missing", f"the claude CLI was not found ({binary})") from exc
            try:
                code, out = await asyncio.wait_for(_run(handle, req.prompt), req.timeout_s)
            except asyncio.TimeoutError as exc:
                raise RuntimeFailure("timeout", f"claude didn't answer within {req.timeout_s:g} s",
                                     retryable=True) from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "claude exited before reading the prompt") from exc
            finally:
                await handle.terminate(KILL_GRACE_S)
                await asyncio.to_thread(remove_empty_project_folder, Path.home() / ".claude", cwd)
        return _result(code, out)


async def _run(handle, prompt: str) -> tuple[int, bytes]:
    await handle.write(prompt.encode("utf-8"))
    await handle.close_stdin()
    out = b"\n".join([line async for line in handle.lines()])
    return await handle.wait(), out


def _result(code: int, out: bytes) -> OneshotResult:
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise RuntimeFailure("protocol", f"claude printed no JSON result (exit {code})")
    if data.get("is_error") or code != 0:
        raise RuntimeFailure("protocol", f"claude reported an error (exit {code}): "
                                         f"{str(data.get('result') or data.get('subtype'))[:300]}")
    text = data.get("result")
    return OneshotResult(text=text if isinstance(text, str) else None, structured=data.get("structured_output"),
                         usage=token_usage(data))


def oneshot() -> ClaudeOneshot:
    return ClaudeOneshot()
