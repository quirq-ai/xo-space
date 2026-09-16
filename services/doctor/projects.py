"""Project folders and the runtime keys each one uses.

Mirrors ``project_layout.runtime_key`` (pid from ``.xo/project.json`` when
present, not ``_template`` and path-safe, else the normalized folder name)
without calling it, because it reaches ``xo_projects_root()``, which creates
the root. Both keys count as in use: a pre-pid runtime folder named after the
project folder is merged into the pid folder only when a current server
resolves the project (``project_layout.py:268-291``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import _is_safe_runtime_key  # a safety rule is imported, never copied
from services.doctor import inventory
from services.doctor.reading import ReadResult

if TYPE_CHECKING:
    from services.doctor.context import Context


@dataclass(frozen=True)
class Project:
    name: str
    xo: Path
    read: ReadResult  # the outcome for .xo/project.json
    pid: Optional[str]
    keys_in_use: frozenset[str]


def _pid(value: Optional[dict]) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    raw = value.get("pid")
    if not raw or value.get("_template", False):
        return None
    key = str(raw)
    return key if _is_safe_runtime_key(key) else None


def scan(ctx: "Context") -> list[Project]:
    """Every non-hidden directory directly under the projects root, symlinks
    included (more keys in use only makes the leftover rule safer)."""
    try:
        entries = sorted(ctx.projects_root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    spec = inventory.spec_for(inventory.PROJECT, "project.json")
    found: list[Project] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        xo = entry / ".xo"
        result = ctx.read(xo / "project.json", spec)
        pid = _pid(result.value)
        keys = {normalize_agent_id(entry.name)} | ({pid} if pid else set())
        found.append(Project(entry.name, xo, result, pid, frozenset(keys)))
    return found
