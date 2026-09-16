"""The state files the doctor knows: where they live, how much they matter,
and which schema versions this xo-space reads (architecture §7.2-7.3).

Versions come from two shipped sources: the JSON Schema files under
``services/cowork_agent/visualizer/schema/`` where one exists, and the
``versions`` sets below for the rest. ``tests/test_doctor_inventory.py`` holds
both to the golden fixtures, which do not ship in the Docker image.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Optional

from services.doctor.reading import MAX_WALK_ENTRIES

STATE, WORKSPACE, PROJECT = "state", "workspace", "project"
#: FAIL when unusable: the only copy of intent, identity or accumulated counts.
KEEP = "keep"
#: WARN when unusable: recomputed from other files.
REBUILDABLE = "rebuildable"
#: Never parsed: history (size only) and private files (never opened).
UNPARSED = "unparsed"

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "cowork_agent" / "visualizer" / "schema"

V1 = frozenset({1})


@dataclass(frozen=True)
class Spec:
    base: str
    pattern: str
    klass: str
    schema_file: Optional[str] = None
    versions: Optional[frozenset[int]] = None
    # Neither schema_file nor versions on a parsed file: exempt from stamping.


def _s(pattern: str, klass: str, *, schema_file: str | None = None, versions: frozenset[int] | None = None) -> Spec:
    return Spec(STATE, pattern, klass, schema_file, versions)


#: First match wins, so specific patterns come before general ones.
SPECS: tuple[Spec, ...] = (
    _s("inbox/inbox.json", KEEP, schema_file="inbox.schema.json"),
    _s("scheduler/jobs.json", KEEP, versions=V1),
    _s("scheduler/state.json", KEEP, versions=V1),
    _s("scheduler/runs/*.jsonl", UNPARSED),
    _s("connections/accounts.json", KEEP, versions=V1),
    _s("connections/*/config.json", KEEP, versions=V1),
    _s("connections/*/state.json", KEEP, versions=V1),
    _s("connections/*/events*.jsonl", UNPARSED),
    _s("sharing/removed/*.json", KEEP, versions=V1),
    _s("sharing/*.json", KEEP, versions=V1),
    _s("settings/onboarding.json", KEEP, versions=V1),
    _s("settings/*.env", UNPARSED),
    _s("secrets/**", UNPARSED),
    _s("usage/*.json", KEEP, versions=V1),
    _s("projects/offsets.json", KEEP, versions=V1),
    _s("projects/*-offsets.json", KEEP),  # an adapter's own shape; exempt
    _s("projects/timeline*.jsonl", UNPARSED),
    _s("projects/*/stats.json", KEEP, schema_file="stats.schema.json"),
    _s("projects/*/workitems/claims.json", KEEP, versions=V1),
    _s("projects/*/sessions/sessions-augment.json", KEEP, schema_file="sessions-augment.schema.json"),
    _s("projects/*/sessions/sessionslist.d/*.json", KEEP),  # keyed by session; exempt
    _s("projects/*/github/issues.json", REBUILDABLE, schema_file="github-issues.schema.json"),
    _s("projects/*/timeline*.jsonl", UNPARSED),
    _s("cache/heartbeat.json", REBUILDABLE, versions=V1),
    _s("cache/stats.json", REBUILDABLE, schema_file="stats.schema.json"),
    _s("cache/graph.json", REBUILDABLE),
    _s("cache/dashboard.json", REBUILDABLE),
    _s("cache/sessions.json", REBUILDABLE),
    _s("cache/sessions/sessionslist.json", REBUILDABLE),  # keyed by name; exempt
    _s("cache/sessions/sessions-augment.json", REBUILDABLE, schema_file="sessions-augment.schema.json"),
    _s("cache/activity/workspace.json", REBUILDABLE, schema_file="activity.schema.json"),
    _s("cache/activity/projects/*.json", REBUILDABLE, schema_file="activity.schema.json"),
    _s("logs/**", UNPARSED),
    _s(".locks/*", UNPARSED),
    _s("quarantine/**", UNPARSED),
    Spec(WORKSPACE, "space.json", KEEP, schema_file="space.schema.json"),
    Spec(WORKSPACE, "projects.json", REBUILDABLE, schema_file="projects.schema.json"),
    Spec(WORKSPACE, "xo.json", REBUILDABLE),  # services/xo_manifest.py writes no stamp
    Spec(PROJECT, "project.json", KEEP, schema_file="project.schema.json"),
    Spec(PROJECT, "todos.json", KEEP, schema_file="todos.schema.json"),
    Spec(PROJECT, "workitems.json", KEEP, schema_file="workitems.schema.json"),
    Spec(PROJECT, "peers.json", KEEP, schema_file="peers.schema.json"),
    Spec(PROJECT, "agent.json", KEEP, schema_file="agent.schema.json"),
)


def _match(parts: list[str], pattern: list[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_match(parts[i:], pattern[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:])


def spec_for(base: str, rel: str) -> Optional[Spec]:
    parts = rel.split("/")
    for spec in SPECS:
        if spec.base == base and _match(parts, spec.pattern.split("/")):
            return spec
    return None


@lru_cache(maxsize=None)
def schema_file_versions(name: str) -> frozenset[int]:
    """The versions a shipped schema file accepts: ``properties.schema.const`` or ``enum``."""
    document = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    rule = document["properties"]["schema"]
    return frozenset({rule["const"]}) if "const" in rule else frozenset(rule["enum"])


def accepted(spec: Spec) -> Optional[frozenset[int]]:
    if spec.schema_file:
        return schema_file_versions(spec.schema_file)
    return spec.versions


def names(base: str) -> tuple[str, ...]:
    return tuple(spec.pattern for spec in SPECS if spec.base == base)


def walk_files(root: Path, limit: int = MAX_WALK_ENTRIES) -> tuple[list[Path], bool, list[Path]]:
    """Regular files under ``root`` (no symlinks followed or returned),
    whether the walk stopped at ``limit`` entries, and any subfolder ``root``
    couldn't list (permissions, I/O). Every directory and file counts toward
    ``limit``."""
    found: list[Path] = []
    unreadable: list[Path] = []
    seen = 0

    def _onerror(exc: OSError) -> None:
        if exc.filename:
            unreadable.append(Path(exc.filename))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_onerror):
        seen += len(dirnames)
        if seen > limit:
            return found, True, unreadable
        for name in filenames:
            seen += 1
            if seen > limit:
                return found, True, unreadable
            path = Path(dirpath, name)
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    found.append(path)
            except OSError:
                continue
    return found, False, unreadable
