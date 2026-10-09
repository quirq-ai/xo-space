"""Run qq commands from the server: the bridge for operations that moved to qq.

Design: infra/commands/DESIGN.md. A migrated operation runs ``qq <command> --json``
from this checkout first; only when qq cannot run (missing, timed out, no JSON
answer: QQUnavailable) does the caller fall back to its in-process code.

A qq command prints one JSON object on stdout with ``--json`` and exits 0 (ok),
1 (failed or refused), 2 (bad usage) or 3 (pending); qq's own errors are 125.
Runs go through ``utils.commands`` like every other external command, so they
land in the activity log with credentials redacted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from utils.commands import run

REPO_ROOT = Path(__file__).resolve().parents[1]


class QQUnavailable(Exception):
    """qq could not be run, or it did not answer with a JSON object."""


@dataclass(frozen=True)
class QQResult:
    returncode: int
    data: dict
    stderr: str


async def run_qq(args: Sequence[str], timeout: float) -> QQResult:
    """Run ``qq <args> --json`` in this checkout and return its exit code and object.

    Raises QQUnavailable when qq is missing, times out, or prints no JSON object:
    the caller decides how that maps to HTTP. A command that ran and refused
    (exit 1 with an object) is a normal result, not an exception."""
    result = await run(["qq", *args, "--json"], cwd=REPO_ROOT, timeout=timeout,
                       separate_stderr=True, log_label="qq " + " ".join(args))
    if result.binary_missing:
        raise QQUnavailable("qq is not on PATH; install depot (github.com/quirq-ai/depot)")
    if result.timed_out:
        raise QQUnavailable(f"qq {' '.join(args)} timed out after {timeout:g}s")
    data: Optional[dict] = None
    try:
        parsed = json.loads(result.output.strip() or "null")
        data = parsed if isinstance(parsed, dict) else None
    except ValueError:
        data = None
    if data is None:
        detail = (result.stderr or result.output or result.exception or "").strip()[-300:]
        raise QQUnavailable(f"qq {' '.join(args)} exited {result.returncode} without a JSON result"
                            + (f": {detail}" if detail else ""))
    return QQResult(result.returncode, data, result.stderr.strip())
