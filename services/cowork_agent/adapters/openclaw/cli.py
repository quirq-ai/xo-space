"""
The ``openclaw`` CLI: the one way this adapter writes ``openclaw.json``.

Writes go through ``openclaw agents add`` and ``openclaw config patch``
(argv from the manifest's command templates) instead of the adapter
serialising the file itself. The CLI validates the result against the
installed release's schema, takes OpenClaw's config lock, keeps the roster in
the form that release requires (a second agent stamps
``agents.ownership: "explicit"``), and the running gateway hot-applies
``agents.entries`` changes. Reading the file directly is still fine
(``store.load_openclaw_config``).

The gateway applies a write after a 300 ms debounce plus the reload itself;
:func:`seconds_until_applied` tells a caller how long to let the last write
settle before relying on it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.registry.agent_registry import get_agent
from utils.commands import run_sync

_AGENT = "openclaw"
_DETAIL_CHARS = 500

#: Seconds after a write by which the gateway has applied it: the 300 ms
#: reload debounce plus the reload (up to ~0.7 s observed), with margin.
RELOAD_SETTLE_SECONDS = 1.5

_last_write: Optional[float] = None


class OpenclawCliError(RuntimeError):
    """An ``openclaw`` CLI call failed; the message carries the CLI's reason."""


def _run(argv: list[str], *, input: bytes | None = None) -> None:
    global _last_write
    manifest = get_agent(_AGENT)
    label = " ".join(argv[1:3])
    result = run_sync(
        argv,
        cwd=manifest.cwd,
        timeout=manifest.cli_timeout_seconds,
        separate_stderr=True,
        input=input,
    )
    if result.ok:
        _last_write = time.monotonic()
        return
    if result.binary_missing:
        raise OpenclawCliError(f"{argv[0]} not found on PATH")
    if result.timed_out:
        raise OpenclawCliError(f"openclaw {label} timed out after {manifest.cli_timeout_seconds}s")
    raise OpenclawCliError(f"openclaw {label} exited {result.returncode}: {failure_detail(result)}")


def failure_detail(result) -> str:
    """The reason a failed CLI call gives: the ``- …`` problem lines it lists
    (e.g. an invalid config's unrecognized key) and its last line, which is
    the reason itself or the remedy. Config warnings printed before are left
    out."""
    text = result.stderr or result.output or result.exception or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no output"
    problems = [line[2:] for line in lines[:-1] if line.startswith("- ")]
    return "; ".join(problems + [lines[-1]])[:_DETAIL_CHARS]


def seconds_until_applied() -> float:
    """How long until the gateway has applied this process's last config
    write; 0 once it has, or when nothing was written."""
    if _last_write is None:
        return 0.0
    return max(0.0, _last_write + RELOAD_SETTLE_SECONDS - time.monotonic())


def add_agent(agent_id: str, workspace: Path) -> None:
    """``openclaw agents add <id> --workspace <dir>``: adds the roster entry and
    scaffolds the agent's directory. Files already in the workspace are kept."""
    _run(get_agent(_AGENT).command("agents_add", agent_id=agent_id, workspace=str(workspace)))


def patch(changes: dict[str, Any]) -> None:
    """``openclaw config patch --stdin``: one validated write of a JSON merge
    patch. Objects merge recursively and a ``None`` value deletes its key, so
    sets and removals land together or not at all."""
    if not changes:
        return
    _run(get_agent(_AGENT).command("config_patch_stdin"), input=json.dumps(changes).encode("utf-8"))
