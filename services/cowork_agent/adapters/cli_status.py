"""
Shared CLI invocation for the per-agent status adapters.

The openclaw / claude_code / hermes status adapters all shell out to a CLI and
then interpret its output. The *invocation* half is identical across them —
resolve the binary, guard a missing absolute path, spawn, enforce a timeout
(killing the process on expiry), and decode stdout/stderr. The *interpretation*
half differs per agent (strict JSON vs. text parse vs. gateway-down fallback,
and which ``invalid_*`` code to raise), so it stays in each adapter.

This module owns only the invocation half:

    resolve_binary(env_var, default_bin) -> str
    run_cli(binary, args, *, timeout, label) -> CliResult

``run_cli`` raises :class:`CliStatusError` for ``binary_not_found`` / ``timeout``
(the cases that are identical everywhere) and otherwise returns a
:class:`CliResult`. It deliberately does **not** judge the return code or parse
output — each adapter keeps its own short tail for that, preserving its exact
error code and detail string.

``label`` is the binary's display noun (the agent's CLI command name) and is
used only to keep error *messages* identical to the pre-extraction text.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Optional, Sequence


class CliStatusError(Exception):
    """CLI invocation/interpretation failure.

    ``code`` is mapped to an HTTP status by the /models/status and
    /channels/status routers. The vocabulary is the union raised across all
    adapters: ``binary_not_found`` | ``timeout`` | ``execution_failed`` |
    ``invalid_json`` | ``invalid_output``. Each adapter re-exports this class
    under its historical name (e.g. ``OpenclawStatusError``) so callers and the
    routers are unchanged.
    """

    def __init__(self, message: str, *, code: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _found(candidates: Sequence[str]) -> Optional[str]:
    """The first of ``candidates`` that runs (a name on PATH or a ``~`` path)."""
    return next((found for c in candidates if (found := shutil.which(os.path.expanduser(c)))), None)


def resolve_binary(env_var: str, default_bin: str) -> str:
    """Env override → PATH lookup → bare command name."""
    configured = (os.getenv(env_var, "") or "").strip()
    return configured or _found((default_bin,)) or default_bin


def locate(path_env: str, binaries: Sequence[str]) -> Optional[str]:
    """The CLI ``path_env`` names, else the first of ``binaries`` found; None when none runs."""
    configured = (os.getenv(path_env, "") or "").strip()
    return _found((configured,) if configured else binaries)


@dataclass
class CliResult:
    """Outcome of a completed CLI run. stdout/stderr are utf-8 decoded
    (errors replaced) and stripped."""

    returncode: int
    stdout: str
    stderr: str


async def run_cli(
    binary: str,
    args: Sequence[str],
    *,
    timeout: float,
    label: str,
) -> CliResult:
    """Spawn ``binary args`` with a hard timeout and return the decoded result.

    Raises :class:`CliStatusError` with code ``binary_not_found`` if the binary
    is a missing absolute path or cannot be executed, or ``timeout`` if it does
    not finish within ``timeout`` seconds (the process is killed in that case).
    The return code is returned, not judged — callers decide what counts as a
    failure.
    """
    if os.path.isabs(binary) and not os.path.isfile(binary):
        raise CliStatusError(
            f"{label} binary not found at {binary}", code="binary_not_found", detail=binary
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise CliStatusError(
            f"{label} binary unavailable: {binary}", code="binary_not_found", detail=str(e)
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise CliStatusError(f"{label} timed out after {timeout}s", code="timeout")

    return CliResult(
        returncode=proc.returncode,
        stdout=(stdout or b"").decode("utf-8", errors="replace").strip(),
        stderr=(stderr or b"").decode("utf-8", errors="replace").strip(),
    )
