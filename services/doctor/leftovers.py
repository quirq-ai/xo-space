"""Leftover runtime data: ``<state root>/projects/<key>/`` no project uses.

Detection only reads. ``move_aside`` (below) is the doctor's one action; it
re-checks every rule and renames the folder into ``quarantine/``. It never
copies and never deletes (architecture §9).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from datetime import datetime, timezone

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import _is_safe_runtime_key
from services.doctor.context import Context
from services.doctor.model import FAIL, WARN, Finding, ago, size
from services.doctor.reading import Tree, measure_tree, readable_dir
from services.errors import ServiceError
from services.storage import layout
from services.timestamps import iso

logger = logging.getLogger(__name__)

LEFTOVER_MIN_AGE_S = 600
#: A pre-pid runtime folder can briefly coexist with a project's pid folder in
#: the seconds between minting the pid and the server merging the old folder
#: in; that's not a split worth reporting yet.
SPLIT_MIN_AGE_S = 60
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


def _split_findings(ctx: Context) -> list[Finding]:
    """F2: a project resolved by pid, but its pre-pid folder-key runtime
    folder is still sitting beside the pid folder, unmerged (a current server
    only merges it in when it resolves the project again)."""
    runtime = ctx.state_root / "projects"
    out: list[Finding] = []
    for project in ctx.projects():
        if project.read.outcome != "ok" or not project.pid:
            continue
        folder_key = normalize_agent_id(project.name)
        if folder_key == project.pid:
            continue
        folder_dir = runtime / folder_key
        if folder_dir.is_symlink() or not folder_dir.is_dir():
            continue
        tree = measure_tree(folder_dir)
        if tree.newest is not None and ctx.now - tree.newest < SPLIT_MIN_AGE_S:
            continue
        pid_dir = runtime / project.pid
        if pid_dir.is_dir():
            observed = (f"Project {project.name} has runtime data under its folder name "
                       f"(projects/{folder_key}) as well as its pid (projects/{project.pid}).")
        else:
            observed = (f"Project {project.name} has its runtime data under its folder name "
                       f"(projects/{folder_key}) instead of its pid (projects/{project.pid}).")
        out.append(Finding(
            "runtime.split", WARN, project.name, ctx.display(folder_dir), observed,
            "Data in the folder-name copy isn't shown for this project. Restart the server once; "
            "it merges that folder into the pid folder.",
        ))
    return out


def check(ctx: Context) -> list[Finding]:
    result = survey(ctx)
    out = [result.blocked] if result.blocked is not None else []
    out += [_finding(ctx, leftover, result.blocked is None) for leftover in result.leftovers]
    out += _split_findings(ctx)
    return out


class DoctorError(ServiceError):
    """A refused or failed doctor action. Nothing was moved."""


_BLOCKED = {
    "runtime.projects_root_suspect": ("doctor_projects_root_suspect",
                                      "The projects folder is missing or empty, so leftovers can't be told apart from live data."),
    "runtime.keys_unknown": ("doctor_keys_unknown",
                             "A project's project.json can't be read, so leftovers can't be told apart from live data."),
    "runtime.too_many_leftovers": ("doctor_too_many_leftovers",
                                   "More folders look abandoned than there are projects. Nothing was moved."),
}


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def move_aside(key: str, *, now: Optional[float] = None) -> dict:
    """Rename ``projects/<key>`` to ``quarantine/runtime-leftovers/<key>-<time>``.

    Never trusts an earlier report: every §9.1 rule is evaluated again here.
    """
    invalid = DoctorError("doctor_invalid_key", "That is not a runtime data folder.", 400)
    if not isinstance(key, str) or not _is_safe_runtime_key(key):
        raise invalid
    ctx = Context.from_environment(now)
    runtime = ctx.state_root / "projects"
    source = runtime / key
    shown = ctx.display(source)
    # Everything below touches the filesystem again (a symlink/permission
    # check, the survey's own walk, the mkdir and the rename); any OSError
    # not already turned into a DoctorError above becomes doctor_move_failed
    # instead of escaping raw (§9.3). DoctorError itself isn't an OSError, so
    # the specific refusals raised inside this block pass through untouched.
    gone = DoctorError("doctor_gone", "This folder was already moved or removed. Run checks again.", 409)
    try:
        if source.is_symlink():
            raise invalid
        if not source.exists():
            raise gone
        if not source.is_dir() or source.resolve().parent != runtime.resolve():
            raise invalid
        result = survey(ctx)
        if result.blocked is not None:
            code, message = _BLOCKED[result.blocked.id]
            raise DoctorError(code, message, 409)
        leftover = next((item for item in result.leftovers if item.key == key), None)
        if leftover is None:
            raise DoctorError("doctor_not_leftover", "A project uses this data now. Nothing was moved.", 409)
        if not source.exists():
            raise gone
        if too_recent(ctx, leftover):
            raise DoctorError("doctor_too_recent", "This folder was written to in the last 10 minutes. Try again later.", 409)
        target = ctx.state_root / layout.quarantine_dir().name / "runtime-leftovers" / f"{key}-{_stamp(ctx.now)}"
        if os.path.lexists(target):
            raise DoctorError("doctor_move_failed", f"Could not move {shown}: {ctx.display(target)} already exists. Nothing was moved.", 500)
        target.parent.mkdir(parents=True, exist_ok=True)
        # One rename: it happens or it doesn't. EXDEV (another filesystem) is refused, never copied.
        os.rename(source, target)
    except FileNotFoundError as exc:
        if not source.exists():
            raise gone from exc
        raise DoctorError("doctor_move_failed", f"Could not move {shown}: {exc.strerror or exc}. Nothing was moved.", 500) from exc
    except OSError as exc:
        raise DoctorError("doctor_move_failed", f"Could not move {shown}: {exc.strerror or exc}. Nothing was moved.", 500) from exc
    logger.info("doctor: moved runtime leftover %s aside to %s", key, target)
    return {"moved": True, "key": key, "to": ctx.display(target),
            "bytes": leftover.tree.bytes, "files": leftover.tree.files}
