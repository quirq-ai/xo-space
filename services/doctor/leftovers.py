"""Leftover runtime data: ``<state root>/projects/<key>/`` no project uses.

Detection only reads. ``move_aside`` (below) is the doctor's one action; it
re-checks every rule and renames the folder into ``quarantine/``. It never
copies and never deletes (architecture §9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from datetime import datetime, timezone

from services.cowork_agent.project_layout import _is_safe_runtime_key
from services.doctor.context import Context
from services.doctor.model import FAIL, WARN, Finding, ago, size
from services.doctor.reading import Tree, measure_tree, readable_dir
from services.timestamps import iso

logger = logging.getLogger(__name__)

LEFTOVER_MIN_AGE_S = 600
ACTION = {"kind": "move_runtime_leftover_aside"}
_CONTENTS = (("sessions", "sessions"), ("stats.json", "stats"), ("timeline.jsonl", "timeline"),
             ("github", "issues mirror"), ("workitems", "claims"))


@dataclass(frozen=True)
class Leftover:
    key: str
    path: Path
    tree: Tree


@dataclass(frozen=True)
class Survey:
    #: Set when a run-level rule fails (§9.1 rules 3, 4, 7): no action is offered.
    blocked: Optional[Finding]
    leftovers: list[Leftover]


def _candidates(ctx: Context) -> list[Path]:
    runtime = ctx.state_root / "projects"
    try:
        entries = sorted(runtime.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    return [entry for entry in entries
            if entry.is_dir() and not entry.is_symlink() and _is_safe_runtime_key(entry.name)]


def survey(ctx: Context) -> Survey:
    candidates = _candidates(ctx)
    if not candidates:
        return Survey(None, [])
    live = ctx.projects()
    if not readable_dir(ctx.projects_root) or not live:
        return Survey(Finding(
            "runtime.projects_root_suspect", FAIL, "projects root", ctx.display(ctx.projects_root),
            f"{len(candidates)} runtime data folder(s) exist, but the projects folder is missing, unreadable or empty.",
            "Every project's runtime data would look abandoned. Check that the projects folder is mounted and that XO_PROJECTS_ROOT is right.",
        ), [])
    unknown = [project.name for project in live if project.read.outcome not in ("ok", "absent")]
    if unknown:
        return Survey(Finding(
            "runtime.keys_unknown", WARN, ", ".join(unknown), ctx.display(ctx.projects_root),
            f"project.json can't be read in: {', '.join(unknown)}.",
            "Those projects' runtime data can't be told apart from leftovers, so leftovers aren't checked. Fix the project.json files reported above first.",
        ), [])
    in_use = frozenset().union(*(project.keys_in_use for project in live))
    found = [Leftover(path.name, path, measure_tree(path)) for path in candidates if path.name not in in_use]
    if found and len(found) >= len(live):
        return Survey(Finding(
            "runtime.too_many_leftovers", WARN, "runtime data", ctx.display(ctx.state_root / "projects"),
            f"{len(found)} runtime data folder(s) look abandoned, and there are {len(live)} project(s).",
            "If you changed the projects folder in Setup, switch back or move the projects over first. None of the folders listed can be moved from here while this is true.",
        ), found)
    return Survey(None, found)


def too_recent(ctx: Context, leftover: Leftover) -> bool:
    newest = leftover.tree.newest
    return newest is None or ctx.now - newest < LEFTOVER_MIN_AGE_S


def _finding(ctx: Context, leftover: Leftover, actionable: bool) -> Finding:
    tree = leftover.tree
    contains = [label for name, label in _CONTENTS if (leftover.path / name).exists()]
    written = (f"Last written {ago(ctx.now - tree.newest)} ago." if tree.newest is not None
               else "Can't be dated: too large or partly unreadable.")
    return Finding(
        "runtime.leftover", WARN, leftover.key, ctx.display(leftover.path),
        f"No project in {ctx.display(ctx.projects_root)} uses this data. {written}",
        f"It takes {'at least ' if tree.truncated else ''}{size(tree.bytes)} and is never read unless the project folder comes back. "
        "If you moved or renamed the project folder yourself, move it back instead; this data will be picked up again.",
        details={"bytes": tree.bytes, "files": tree.files, "truncated": tree.truncated, "contains": contains,
                 "newest_mtime": None if tree.newest is None else iso(datetime.fromtimestamp(tree.newest, timezone.utc))},
        action=dict(ACTION) if actionable and not too_recent(ctx, leftover) else None,
    )


def check(ctx: Context) -> list[Finding]:
    result = survey(ctx)
    out = [result.blocked] if result.blocked is not None else []
    out += [_finding(ctx, leftover, result.blocked is None) for leftover in result.leftovers]
    return out
