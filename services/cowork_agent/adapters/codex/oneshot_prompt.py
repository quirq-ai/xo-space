"""Capability ``oneshot_prompt``: one tool-free codex completion (storm
ch 01 §4.7, ch 03 §10, ch 10 §8).

For small jobs XO does on its own behalf (titles, a proposal's one-line why).
The run is ``codex exec --json`` with codex's tools switched off (its shell,
unified exec, code mode, apps, plugins, the browser and computer use, image
tools, web search: ``--disable``), a ``read-only`` sandbox and no approvals
to ask as a second line, without the person's ``config.toml``
(``--ignore-user-config``: none of their MCP servers or rules) and without a
transcript (``--ephemeral``), in an empty temp folder (no project is loaded),
with the prompt on stdin, never argv.
The login still comes from ``$CODEX_HOME``. It starts through the launcher
core passes, with the same env whitelist as a session.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.codex.runtime.env import session_env
from services.cowork_agent.adapters.codex.runtime.core import CLI_PATH_ENV, DEFAULT_BINARY
from services.cowork_agent.adapters.contract import (
    Launcher,
    OneshotRequest,
    OneshotResult,
    RuntimeFailure,
    SupportItem,
    SupportReport,
    TokenUsage,
)

KILL_GRACE_S = 2.0
SCHEMA_FILE = "schema.json"
#: codex's features that give a model a tool (``codex features list``): off,
#: so a oneshot has none, as claude's ``--tools ""``.
TOOL_FEATURES = ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "browser_use",
                 "browser_use_external", "in_app_browser", "computer_use", "view_image", "image_generation",
                 "skill_search", "sleep_tool", "goals")


def build_argv(binary: str, req: OneshotRequest, folder: str) -> list[str]:
    argv = [binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "-c", 'approval_policy="never"', "-c", 'web_search="disabled"']
    for feature in TOOL_FEATURES:
        argv += ["--disable", feature]
    if req.model:
        argv += ["--model", req.model]
    if req.effort:
        argv += ["-c", f'model_reasoning_effort="{req.effort}"']
    if req.output_schema is not None:
        argv += ["--output-schema", str(Path(folder) / SCHEMA_FILE)]
    return [*argv, "-"]


class CodexOneshot:
    def check(self) -> SupportReport:
        return SupportReport(items=(
            SupportItem(feature="isolation", support="native", enforced_by="cli",
                        mechanism="codex exec --disable <every tool feature> --ignore-user-config --ephemeral "
                                  "--sandbox read-only, in an empty folder",
                        note="no tools, no project, none of the person's MCP servers or rules, no transcript"),
            SupportItem(feature="output_schema", support="native", enforced_by="cli", mechanism="--output-schema"),
        ))

    async def complete(self, req: OneshotRequest, launcher: Launcher) -> OneshotResult:
        binary = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
        env = await asyncio.to_thread(session_env, spec_env={}, label="codex oneshot")
        with tempfile.TemporaryDirectory(prefix="xo-oneshot-") as cwd:
            if req.output_schema is not None:
                (Path(cwd) / SCHEMA_FILE).write_text(json.dumps(req.output_schema), encoding="utf-8")
            try:
                handle = await launcher.spawn(build_argv(binary, req, cwd), cwd=cwd, env=env, label="codex oneshot")
            except OSError as exc:
                raise RuntimeFailure("cli_missing", f"the codex CLI can't be started ({binary}: "
                                                    f"{exc.strerror or exc})") from exc
            try:
                code, lines = await asyncio.wait_for(_run(handle, req.prompt), req.timeout_s)
            except asyncio.TimeoutError as exc:
                raise RuntimeFailure("timeout", f"codex didn't answer within {req.timeout_s:g} s",
                                     retryable=True) from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "codex exited before reading the prompt") from exc
            finally:
                await handle.terminate(KILL_GRACE_S)
        return result(code, lines, structured=req.output_schema is not None)


async def _run(handle: Any, prompt: str) -> tuple[int, list[bytes]]:
    await handle.write(prompt.encode("utf-8"))
    await handle.close_stdin()
    lines = [line async for line in handle.lines()]
    return await handle.wait(), lines


def result(code: int, lines: list[bytes], *, structured: bool) -> OneshotResult:
    """``codex exec --json``'s events: the last agent message is the answer;
    ``turn.completed`` has the usage; ``turn.failed`` / ``error`` a failure."""
    text: str | None = None
    usage: TokenUsage | None = None
    failure: str | None = None
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            counts = event["usage"]
            cached = _int(counts.get("cached_input_tokens"))
            usage = TokenUsage(input=max(_int(counts.get("input_tokens")) - cached, 0), cache_read=cached,
                               output=_int(counts.get("output_tokens")),
                               reasoning=_int(counts.get("reasoning_output_tokens")))
        elif kind in ("turn.failed", "error"):
            error = event.get("error") if isinstance(event.get("error"), dict) else event
            failure = str(error.get("message") or kind)
    if failure is not None or code != 0:
        raise RuntimeFailure("protocol", f"codex reported an error (exit {code}): {(failure or 'no answer')[:300]}")
    if text is None:
        raise RuntimeFailure("protocol", f"codex printed no answer (exit {code})")
    parsed = None
    if structured:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    return OneshotResult(text=text, structured=parsed, usage=usage)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def oneshot() -> CodexOneshot:
    return CodexOneshot()
