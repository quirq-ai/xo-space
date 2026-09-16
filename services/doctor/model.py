"""The vocabulary of a doctor report: levels, findings, check results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

OK, WARN, FAIL, ERROR = "OK", "WARN", "FAIL", "ERROR"
LEVELS: tuple[str, ...] = (OK, WARN, FAIL, ERROR)
# ERROR ranks highest: a check that could not run may be hiding a FAIL.
_RANK = {OK: 0, WARN: 1, FAIL: 2, ERROR: 3}


def worst(levels: Iterable[str]) -> str:
    return max(levels, key=_RANK.__getitem__, default=OK)


@dataclass
class Finding:
    id: str
    level: str
    subject: str
    path: str
    observed: str
    why_it_matters: str
    details: dict[str, Any] = field(default_factory=dict)
    action: Optional[dict[str, Any]] = None

    @property
    def key(self) -> str:
        """Stable across runs for the same problem, so a UI can keep a dialog open."""
        return f"{self.id}:{self.subject}"

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id, "key": self.key, "level": self.level, "subject": self.subject,
            "path": self.path, "observed": self.observed, "why_it_matters": self.why_it_matters,
            "details": self.details,
        }
        if self.action is not None:
            out["action"] = self.action
        return out


@dataclass
class CheckResult:
    id: str
    level: str
    findings: list[Finding]
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "level": self.level,
                               "findings": [f.to_dict() for f in self.findings]}
        if self.error is not None:
            out["error"] = self.error
        return out


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, span in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= span:
            n = seconds // span
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
