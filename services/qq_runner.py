"""Run qq commands from the server: the bridge for operations that moved to qq.

Design: infra/commands/DESIGN.md. An operation listed in ``$QUIRQ_QQ_OPERATIONS``
(comma-separated, e.g. ``update``) is run as ``qq <command> --json`` from this
checkout instead of in-process; anything not listed keeps its in-process path.

A qq command prints one JSON object on stdout with ``--json`` and exits 0 (ok),
1 (failed or refused), 2 (bad usage) or 3 (pending); qq's own errors are 125.
Runs go through ``utils.commands`` like every other external command, so they
land in the activity log with credentials redacted.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from utils.commands import run

ENV_OPERATIONS = "QUIRQ_QQ_OPERATIONS"
REPO_ROOT = Path(__file__).resolve().parents[1]


def enabled(operation: str) -> bool:
    """True when the server should run ``operation`` through qq."""
    raw = os.getenv(ENV_OPERATIONS, "") or ""
    return operation in {part.strip() for part in raw.split(",") if part.strip()}


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
