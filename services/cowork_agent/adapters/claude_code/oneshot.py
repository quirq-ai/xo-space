"""One-off, tool-free completions with the claude CLI (capability ``oneshot``).

For small jobs XO does on its own behalf, such as drafting a project's
category list. The run has no tools (``--tools ""``), writes no transcript
(``--no-session-persistence``, so it never shows up as a chat), starts in an
empty temp folder (no project memory is loaded), and gets the prompt on stdin
(a large file list would exceed the kernel's per-argument limit).
"""

from __future__ import annotations

import json
import logging
import tempfile

from utils.commands import run

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
EFFORT = "medium"
TIMEOUT_S = 300


async def complete(prompt: str, *, timeout: float = TIMEOUT_S) -> str | None:
    """The model's reply to ``prompt``, or ``None`` on any failure (logged)."""
    from services.cowork_agent.adapters.claude_code.adapter import Adapter
    from services.cowork_agent.registry.settings import load_agent_config

    adapter = Adapter(load_agent_config("claude_code"))
    argv = [
        adapter.config.get("cli_path") or "claude",
        "--print", "--output-format", "json",
        "--model", MODEL, "--effort", EFFORT,
        "--tools", "", "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory(prefix="xo-oneshot-") as cwd:
        result = await run(argv, cwd=cwd, timeout=timeout, env=adapter.cli_env(),
                           input=prompt.encode("utf-8"), separate_stderr=True,
                           log_label="intelligence: oneshot")
    if not result.ok:
        log.warning("oneshot: claude failed (code %s%s): %s", result.returncode,
                    ", timed out" if result.timed_out else "", (result.stderr or result.output)[:300])
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        log.warning("oneshot: claude returned non-JSON output")
        return None
    if not isinstance(data, dict) or data.get("is_error"):
        log.warning("oneshot: claude reported an error: %s", str(data.get("result") if isinstance(data, dict) else data)[:300])
        return None
    reply = data.get("result")
    return reply if isinstance(reply, str) else None
