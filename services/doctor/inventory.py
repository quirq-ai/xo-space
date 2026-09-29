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

# Legacy classes, still reported as details.class (design §4).
KEEP = "keep"
REBUILDABLE = "rebuildable"
UNPARSED = "unparsed"

# How a file behaves when it is damaged (#188 design §4; investigation
# Appendix A has the evidence for each assignment below).
#: The only copy of intent or history; the store refuses or loses it.
IRREPLACEABLE = "irreplaceable"
#: The only copy, and its writer replaces it with fresh data on the next event.
OVERWRITTEN_NEXT = "overwritten_next"
#: Disposable, but a feature is blocked while it is damaged.
LIVE_STATE = "live_state"
#: A reading position: rewritten from memory while the watcher runs; a
#: restart on a damaged one re-reads everything and double-counts.
READ_POSITION = "read_position"
#: The store replaces the damage with defaults on its next write.
SELF_OVERWRITING = "self_overwriting"
#: Rebuilt from other files, but only when its content next changes or it is deleted.
CHANGE_CACHE = "change_cache"
#: Rebuilt on a timer or at restart, even when corrupted in place.
REBUILT_VIEW = "rebuilt_view"
#: Existence is the data; the content is never read.
MARKER = "marker"
#: Append-only history (timelines); checked by services/doctor/history.py.
HISTORY = "history"
#: Credentials and machine settings; never opened.
PRIVATE = "private"
#: Logs, locks, moved-aside data, run and event logs.
OPAQUE = "opaque"

PARSED = frozenset({IRREPLACEABLE, OVERWRITTEN_NEXT, LIVE_STATE, READ_POSITION,
                    SELF_OVERWRITING, CHANGE_CACHE, REBUILT_VIEW})
_FAIL_WHEN_UNUSABLE = frozenset({IRREPLACEABLE, OVERWRITTEN_NEXT, LIVE_STATE, READ_POSITION})

# What the owning store does with a schema number it doesn't expect.
REFUSED = "refused"    # it refuses the file
IGNORED = "ignored"    # it reads the file anyway
REPLACED = "replaced"  # it replaces the file with a fresh one

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "cowork_agent" / "visualizer" / "schema"

V1 = frozenset({1})


@dataclass(frozen=True)
class Spec:
    base: str
    pattern: str
    behaviour: str
    schema_file: Optional[str] = None
    versions: Optional[frozenset[int]] = None
    # Neither schema_file nor versions on a parsed file: exempt from stamping.
    schema_newer: str = REFUSED
    schema_older: str = REFUSED  # also applies to a missing stamp
    #: The owning store reads an unstamped file as the lowest accepted version.
    stamp_optional: bool = False

    @property
    def parsed(self) -> bool:
        return self.behaviour in PARSED

    @property
    def klass(self) -> str:
        """The v1 class, derived; kept for details.class and folder checks."""
        if not self.parsed:
            return UNPARSED
        return KEEP if self.behaviour in _FAIL_WHEN_UNUSABLE else REBUILDABLE


#: A store that never checks the number: newer is read anyway, older and
#: missing are fine.
_IGN = {"newer": IGNORED, "older": IGNORED, "optional": True}


def _s(pattern: str, behaviour: str, *, schema_file: str | None = None,
       versions: frozenset[int] | None = None, newer: str = REFUSED, older: str = REFUSED,
       optional: bool = False) -> Spec:
    return Spec(STATE, pattern, behaviour, schema_file, versions, newer, older, optional)


#: First match wins, so specific patterns come before general ones.
SPECS: tuple[Spec, ...] = (
    _s("inbox/inbox.json", IRREPLACEABLE, schema_file="inbox.schema.json", **_IGN),
    _s("inbox/activity/**", OPAQUE),  # the command log and its archive
    _s("scheduler/jobs.json", IRREPLACEABLE, versions=V1, **_IGN),
    _s("scheduler/state.json", LIVE_STATE, versions=V1, **_IGN),
    _s("scheduler/runs/*.jsonl", OPAQUE),
    _s("connections/accounts.json", CHANGE_CACHE, versions=V1, **_IGN),
    _s("connections/*/config.json", IRREPLACEABLE, versions=V1, **_IGN),
    _s("connections/*/state.json", SELF_OVERWRITING, versions=V1, **_IGN),
    _s("connections/*/events*.jsonl", OPAQUE),
    # Existence is the signal (project_sharing/state.py is_removed); a person
    # who "repairs" a marker by deleting it gets the project cloned back.
    _s("sharing/removed/*.json", MARKER),
    _s("sharing/*.json", SELF_OVERWRITING, versions=V1, **_IGN),
    _s("settings/onboarding.json", SELF_OVERWRITING, versions=V1, **_IGN),
    _s("settings/theme.json", IRREPLACEABLE, versions=V1),
    _s("settings/branding.json", IRREPLACEABLE, versions=V1),
    _s("settings/*.env", PRIVATE),
    _s("secrets/**", PRIVATE),
    _s("usage/*.json", SELF_OVERWRITING, versions=V1, **_IGN),
    _s("projects/offsets.json", READ_POSITION, versions=V1, **_IGN),
    _s("projects/*-offsets.json", READ_POSITION),  # an adapter's own shape; exempt
    _s("projects/timeline*.jsonl", HISTORY),
    _s("projects/*/stats.json", OVERWRITTEN_NEXT, schema_file="stats.schema.json", **_IGN),
    _s("projects/*/workitems/claims.json", LIVE_STATE, versions=V1, optional=True),
    _s("projects/*/sessions/sessions-augment.json", OVERWRITTEN_NEXT,
       schema_file="sessions-augment.schema.json", **_IGN),
    _s("projects/*/sessions/sessionslist.d/*.json", IRREPLACEABLE),  # keyed by session; exempt
    # The Space's own index of sessions started with no project (#146): the same
    # shard shape as a project's, one level up, outside projects/<key>/.
    _s("sessions/sessionslist.d/*.json", IRREPLACEABLE),  # keyed by session; exempt
    _s("projects/*/github/issues.json", REBUILT_VIEW, schema_file="github-issues.schema.json",
       newer=REPLACED, older=REPLACED),
    _s("projects/*/timeline*.jsonl", HISTORY),
    _s("cache/heartbeat.json", REBUILT_VIEW, versions=V1, **_IGN),
    _s("cache/stats.json", CHANGE_CACHE, schema_file="stats.schema.json", **_IGN),
    _s("cache/graph.json", REBUILT_VIEW),
    _s("cache/dashboard.json", REBUILT_VIEW),
    _s("cache/sessions.json", REBUILT_VIEW),
    _s("cache/sessions/sessionslist.json", CHANGE_CACHE),  # keyed by name; exempt
    _s("cache/sessions/sessions-augment.json", CHANGE_CACHE,
       schema_file="sessions-augment.schema.json", **_IGN),
    _s("cache/activity/workspace.json", CHANGE_CACHE, schema_file="activity.schema.json", **_IGN),
    _s("cache/activity/projects/*.json", CHANGE_CACHE, schema_file="activity.schema.json", **_IGN),
    _s("logs/**", OPAQUE),
    _s(".locks/*", OPAQUE),
    _s("quarantine/**", OPAQUE),
    Spec(WORKSPACE, "space.json", SELF_OVERWRITING, "space.schema.json",
         schema_newer=REPLACED, schema_older=REPLACED),
    Spec(WORKSPACE, "projects.json", REBUILT_VIEW, "projects.schema.json",
         schema_newer=REPLACED, schema_older=REPLACED),
    Spec(WORKSPACE, "xo.json", REBUILT_VIEW),  # services/xo_manifest.py writes no stamp
    Spec(PROJECT, "project.json", IRREPLACEABLE, "project.schema.json",
         schema_newer=IGNORED, schema_older=IGNORED, stamp_optional=True),
    Spec(PROJECT, "todos.json", IRREPLACEABLE, "todos.schema.json",
         schema_newer=REFUSED, schema_older=IGNORED, stamp_optional=True),
    Spec(PROJECT, "workitems.json", IRREPLACEABLE, "workitems.schema.json", stamp_optional=True),
    Spec(PROJECT, "peers.json", IRREPLACEABLE, "peers.schema.json", stamp_optional=True),
    Spec(PROJECT, "agent.json", IRREPLACEABLE, "agent.schema.json",
         schema_newer=IGNORED, schema_older=IGNORED, stamp_optional=True),
)


def _match(parts: list[str], pattern: list[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_match(parts[i:], pattern[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:])


#: Each pattern split once at import, instead of on every spec_for() call (F7).
_SPEC_PARTS: tuple[tuple[Spec, list[str]], ...] = tuple((spec, spec.pattern.split("/")) for spec in SPECS)


def spec_for(base: str, rel: str) -> Optional[Spec]:
    parts = rel.split("/")
    for spec, pattern in _SPEC_PARTS:
        if spec.base == base and _match(parts, pattern):
            return spec
    return None


@lru_cache(maxsize=None)
def _schema_document(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def schema_file_versions(name: str) -> frozenset[int]:
    """The versions a shipped schema file accepts: ``properties.schema.const`` or ``enum``."""
    rule = _schema_document(name)["properties"]["schema"]
    return frozenset({rule["const"]}) if "const" in rule else frozenset(rule["enum"])


def stamp_required(spec: Spec) -> bool:
    """False when the document's own schema leaves ``schema`` out of
    ``required``. Such a record is legitimate unstamped and an absent version
    means the lowest accepted one — ``agent.schema.json`` says so in as many
    words, and every adapter reads an unstamped record without complaint. The
    inventory's own version table (§7.3) always requires a stamp.
    ``stamp_optional`` specs never require one (the store reads an unstamped
    file; design §4)."""
    if spec.stamp_optional:
        return False
    if spec.schema_file:
        return "schema" in (_schema_document(spec.schema_file).get("required") or [])
    return True


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
