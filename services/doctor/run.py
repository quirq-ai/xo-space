"""Run every check with error isolation and assemble the report (architecture §5)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from services.doctor import checks, leftovers
from services.doctor.context import Context
from services.doctor.model import ERROR, FAIL, LEVELS, CheckResult, Finding, rank, worst
from services.doctor.reading import readable_dir
from services.timestamps import iso

logger = logging.getLogger(__name__)

Check = Callable[[Context], list[Finding]]

#: Listed explicitly, in report order. No auto-discovery.
CHECKS: tuple[tuple[str, Check], ...] = (
    ("roots", checks.roots),
    ("read", checks.reads),
    ("space", checks.space_identity),
    ("projects", checks.duplicate_ids),
    ("runtime", leftovers.check),
    ("tmp", checks.stale_temps),
    ("layout", checks.layout_moves),
    ("legacy", checks.legacy_pending),
    ("watcher", checks.heartbeat),
    ("growth", checks.growth),
)


#: A badly broken state root can produce one finding per file: 3,000 corrupt
#: session shards made a 1.9 MB report, and the Space UI renders every row.
#: Keep the worst ones and say how many were left out.
MAX_FINDINGS_PER_CHECK = 100


def _cap(family: str, findings: list[Finding]) -> list[Finding]:
    if len(findings) <= MAX_FINDINGS_PER_CHECK:
        return findings
    # A stable sort by rank keeps each level's original order, so the report
    # still reads in inventory order within the worst level.
    ordered = sorted(findings, key=lambda finding: -rank(finding.level))
    kept, dropped = ordered[:MAX_FINDINGS_PER_CHECK], ordered[MAX_FINDINGS_PER_CHECK:]
    return kept + [Finding(
        # The rollup's level is the worst of what it hides, not of the whole
        # list: informational OK rows (inventory.unknown_file) sort last and
        # are dropped first, and must not be announced as failures.
        f"{family}.truncated", worst(finding.level for finding in dropped), family, "",
        f"{len(dropped):,} more finding(s) of this kind are not listed.",
        "The list is capped so the report stays readable. Fix the ones above and run the checks again.",
        details={"dropped": len(dropped)},
    )]


def _run_one(family: str, check: Check, ctx: Context) -> CheckResult:
    try:
        findings = _cap(family, check(ctx))
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
        logger.exception("doctor: check %s raised", family)
        return CheckResult(family, ERROR, [], error=f"{type(exc).__name__}: {exc}")
    return CheckResult(family, worst(f.level for f in findings), findings)


def run_checks(*, now: float | None = None) -> dict:
    started = time.monotonic()
    ctx = Context.from_environment(now)
    if not readable_dir(ctx.state_root):
        results = [CheckResult("roots", FAIL, [Finding(
            "roots.state_unavailable", FAIL, "state root", ctx.display(ctx.state_root),
            "The Quirq state folder is missing or can't be read.",
            "Nothing Quirq keeps on this machine can be checked. Check QUIRQ_STATE_ROOT and the folder's permissions.",
        )])]
    else:
        results = [_run_one(family, check, ctx) for family, check in CHECKS]
    levels = [result.level for result in results]
    return {
        "schema": 1,
        "checked_at": iso(datetime.fromtimestamp(ctx.now, timezone.utc)),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "level": worst(levels),
        "summary": {level: levels.count(level) for level in LEVELS},
        "roots": {"state": ctx.display(ctx.state_root), "projects": ctx.display(ctx.projects_root)},
        "checks": [result.to_dict() for result in results],
    }
