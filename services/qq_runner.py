"""Run qq commands from the server: the bridge for operations that moved to qq.

Design: infra/commands/DESIGN.md. A migrated operation runs ``qq <command> --json``
from this checkout first. QQNotRun (the operation never started) lets the caller fall back to its
in-process code; QQNoAnswer (started, no answer) must not run it again unless it is safe to repeat.

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
QQ = "qq"   # the launcher; tests/__init__.py points it at nothing, so tests run in-process

# Exit codes that mean the operation never started: bad usage (2), qq's own error (125), the
# command or its Python not runnable (126, 127).
NOT_RUN_CODES = frozenset({2, 125, 126, 127})


class QQUnavailable(Exception):
    """qq gave no answer: one of the two cases below."""


class QQNotRun(QQUnavailable):
    """The operation never started (qq missing, bad usage, no Python): the in-process code may run."""


class QQNoAnswer(QQUnavailable):
    """The operation started but gave no answer (timed out, crashed): it may be half done."""


@dataclass(frozen=True)
class QQResult:
    returncode: int
    data: dict
    stderr: str


async def run_qq(args: Sequence[str], timeout: float) -> QQResult:
    """Run ``qq <args> --json`` in this checkout and return its exit code and object.

    Raises QQNotRun when the operation never started and QQNoAnswer when it started but
    printed no JSON object (both are QQUnavailable): the caller decides how that maps to HTTP. A command that ran and refused
    (exit 1 with an object) is a normal result, not an exception."""
    result = await run([QQ, *args, "--json"], cwd=REPO_ROOT, timeout=timeout,
                       separate_stderr=True, log_label="qq " + " ".join(args))
    if result.binary_missing:
        raise QQNotRun("qq is not on PATH; install it (github.com/quirq-ai/qq)")
    if result.timed_out:
        raise QQNoAnswer(f"qq {' '.join(args)} timed out after {timeout:g}s")
    data: Optional[dict] = None
    try:
        parsed = json.loads(result.output.strip() or "null")
        data = parsed if isinstance(parsed, dict) else None
    except ValueError:
        data = None
    if data is None:
        detail = (result.stderr or result.output or result.exception or "").strip()[-300:]
        error = QQNotRun if result.returncode in NOT_RUN_CODES else QQNoAnswer
        raise error(f"qq {' '.join(args)} exited {result.returncode} without a JSON result"
                    + (f": {detail}" if detail else ""))
    return QQResult(result.returncode, data, result.stderr.strip())
