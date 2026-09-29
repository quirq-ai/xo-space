"""Leftover runtime data: ``<state root>/projects/<key>/`` no project uses.

Detection only reads. ``move_aside`` (below) is the doctor's one action; it
re-checks every rule and renames the folder into ``quarantine/``. It never
copies and never deletes (architecture §9).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from datetime import datetime, timezone

from services.cowork_agent.helpers import normalize_agent_id
from services.doctor import inventory
from services.doctor.context import Context
from services.doctor.model import FAIL, WARN, Finding, ago, ev, moment, printable, size
from services.doctor.projects import is_safe_runtime_key
from services.doctor.reading import Tree, measure_tree, readable_dir, read_tail
from services.errors import ServiceError
from services.storage import layout
from services.timestamps import iso

logger = logging.getLogger(__name__)

LEFTOVER_MIN_AGE_S = 600
#: A pre-pid runtime folder can briefly coexist with a project's pid folder in
#: the seconds between minting the pid and the server merging the old folder
#: in; that's not a split worth reporting yet.
SPLIT_MIN_AGE_S = 60
#: How much of the Space timeline (from the end) is worth scanning for a
#: leftover's last-known project name; older lines aren't worth the read.
NAME_SCAN_BYTES = 4 * 1024 * 1024
#: A project_id past this length is never a real one; don't record it.
NAME_MAX_LEN = 200
#: At most this many of a leftover's own session index entries are read for its name.
NAME_SESSION_FILES = 20
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
            if entry.is_dir() and not entry.is_symlink() and is_safe_runtime_key(entry.name)]


def describe_unknown(entries: list[dict]) -> tuple[str, str]:
    """(subject, observed) of runtime.keys_unknown for these projects."""
    names = ", ".join(entry["name"] for entry in entries)
    reasons = ", ".join(f"{entry['name']} ({entry['reason']})" for entry in entries)
    return names, f"project.json can't be read in: {reasons}."


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
            title="Every project's runtime data looks abandoned",
            consequence="The projects folder is missing, unreadable or empty, so leftovers can't be told apart from live data.",
            self_repair="Nothing.",
            next_step="Check that the projects folder is mounted and that XO_PROJECTS_ROOT is right.",
        ), [])
    unreadable = [project for project in live if project.read.outcome not in ("ok", "absent")]
    if unreadable:
        entries = [{"name": project.name,
                    "reason": project.read.detail or project.read.outcome.replace("_", " ")}
                   for project in unreadable]
        subject, observed = describe_unknown(entries)
        return Survey(Finding(
            "runtime.keys_unknown", WARN, subject, ctx.display(ctx.projects_root), observed,
            "Those projects' runtime data can't be told apart from leftovers, so leftovers aren't checked and nothing "
            "can be moved. Fix or reconnect the projects named here first.",
            details={"projects": entries},
            title="Leftover checks are paused",
            consequence="These projects' runtime data can't be told apart from leftovers, so leftovers aren't listed and nothing can be moved aside.",
            self_repair="Nothing.",
            next_step="Fix or reconnect the projects named here.",
        ), [])
    in_use = frozenset().union(*(project.keys_in_use for project in live))
    found = [Leftover(path.name, path, measure_tree(path)) for path in candidates if path.name not in in_use]
    if found and len(found) >= len(live):
        return Survey(Finding(
            "runtime.too_many_leftovers", WARN, "runtime data", ctx.display(ctx.state_root / "projects"),
            f"Runtime data folders no project uses: {len(found)}. Projects: {len(live)}.",
            "Either projects were deleted, or the projects folder itself changed (Setup, projects folder). If you "
            "changed it, switch back or move the projects over first. Nothing can be moved aside while more folders "
            "look abandoned than there are projects, in case they belong to projects that are only in another folder.",
            title="More folders look abandoned than there are projects",
            consequence="Nothing can be moved aside while this is true.",
            self_repair="Nothing.",
            next_step="If you changed the projects folder (Setup), switch back or move the projects over. If you "
                      "deleted those projects, their folders are the ones listed below.",
        ), found)
    return Survey(None, found)


def too_recent(ctx: Context, leftover: Leftover) -> bool:
    newest = leftover.tree.newest
    return newest is None or ctx.now - newest < LEFTOVER_MIN_AGE_S


def _valid_name(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= NAME_MAX_LEN


def _timeline_names(data: bytes, seeked: bool) -> dict[str, str]:
    """The last ``project_id`` per ``pid`` in a tail of the Space timeline.
    Reads nothing else from a line."""
    lines = data.split(b"\n")
    if seeked and lines:
        lines = lines[1:]  # the partial line the seek landed inside
    names: dict[str, str] = {}
    for raw in lines:
        if not raw.strip():
            continue
        try:
            document = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            # A pathologically deep line can exhaust the parser's recursion limit.
            continue
        if not isinstance(document, dict):
            continue
        pid, project_id = document.get("pid"), document.get("project_id")
        if isinstance(pid, str) and pid and _valid_name(project_id):
            names[pid] = project_id
    return names


def _name_from_sessions(ctx: Context, key: str) -> Optional[str]:
    """The project folder name recorded in the leftover's own session index
    (each row's ``directory``). The composite key is adapter-shaped and is
    never parsed."""
    spec = inventory.spec_for(inventory.STATE, "projects/x/sessions/sessionslist.d/y.json")
    try:
        shards = sorted((ctx.state_root / "projects" / key / "sessions" / "sessionslist.d").glob("*.json"))
    except OSError:
        return None
    for shard in shards[:NAME_SESSION_FILES]:
        result = ctx.read(shard, spec)
        if result.outcome != "ok":
            continue
        for row in result.value.values():
            directory = row.get("directory") if isinstance(row, dict) else None
            if isinstance(directory, str):
                name = os.path.basename(directory.rstrip("/"))
                if _valid_name(name):
                    return name
    return None


def _repository_hint(ctx: Context, key: str) -> Optional[str]:
    """The GitHub repository the leftover mirrored, as a hint (not a folder name)."""
    result = ctx.read(ctx.state_root / "projects" / key / "github" / "issues.json",
                      inventory.spec_for(inventory.STATE, "projects/x/github/issues.json"))
    repo = result.value.get("repo") if result.outcome == "ok" else None
    return repo if _valid_name(repo) else None


def _last_known_names(ctx: Context, keys: list[str]) -> dict[str, tuple[str, str]]:
    """key → (project name, where it was found), from the Space timeline,
    then the Inbox, then the leftover's own session list. Only names and
    pids leave these files."""
    wanted = set(keys)
    found: dict[str, tuple[str, str]] = {}
    tail = read_tail(ctx.state_root / "projects" / "timeline.jsonl", NAME_SCAN_BYTES)
    if tail is not None:
        for pid, name in _timeline_names(*tail).items():
            if pid in wanted:
                found[pid] = (name, "the Space timeline")
    if wanted - found.keys():
        inbox = ctx.read(ctx.state_root / "inbox" / "inbox.json",
                         inventory.spec_for(inventory.STATE, "inbox/inbox.json"))
        items = inbox.value.get("items") if inbox.outcome == "ok" else None
        for item in items if isinstance(items, list) else []:
            pid = item.get("pid") if isinstance(item, dict) else None
            if pid in wanted and pid not in found and _valid_name(item.get("project_id")):
                found[pid] = (item["project_id"], "the Inbox")
    for key in sorted(wanted - found.keys()):
        name = _name_from_sessions(ctx, key)
        if name:
            found[key] = (name, "its session list")
    return found


def _finding(ctx: Context, leftover: Leftover, actionable: bool, names: dict[str, tuple[str, str]]) -> Finding:
    tree = leftover.tree
    contains = [label for name, label in _CONTENTS if (leftover.path / name).exists()]
    written = (f"Last written {ago(ctx.now - tree.newest)} ago." if tree.newest is not None
               else "Can't be dated: too large or partly unreadable.")
    project_name, source = names.get(leftover.key, (None, None))
    repo = None if project_name else _repository_hint(ctx, leftover.key)
    root = ctx.display(ctx.projects_root)
    if project_name:
        observed = f"No project in {root} uses this data. It belonged to project {project_name}. {written}"
        title = f"Project {project_name}'s runtime data is no longer used"
    elif repo:
        observed = (f"No project in {root} uses this data. It belonged to the project for GitHub repository "
                    f"{repo}. {written}")
        title = f"Runtime data of the {repo} project is no longer used"
    else:
        observed = f"No project in {root} uses this data. {written}"
        title = "Runtime data that no project uses"
    taken = f"{'at least ' if tree.truncated else ''}{size(tree.bytes)}"
    details = {"bytes": tree.bytes, "files": tree.files, "truncated": tree.truncated, "contains": contains,
               "newest_mtime": None if tree.newest is None else iso(datetime.fromtimestamp(tree.newest, timezone.utc))}
    evidence = [ev("Size", taken), ev("Files", f"{tree.files:,}"),
                ev("Last written", moment(tree.newest, ctx.now) if tree.newest is not None
                   else "unknown (too large or partly unreadable)"),
                ev("Contains", ", ".join(contains) or "nothing it recognises")]
    if project_name:
        details["project_name"], details["name_source"] = project_name, source
        evidence.append(ev("Named from", source))
    elif repo:
        details["repository"] = repo
        evidence.append(ev("Named from", "its GitHub issue copy (a repository, not a folder name)"))
    offered = actionable and not too_recent(ctx, leftover)
    back = ("If you moved or renamed the project folder yourself, move it back instead; this data will be picked "
            "up again. ")
    if offered:
        next_step = back + "Otherwise use Move aside: it goes into quarantine/, and nothing is deleted."
    elif actionable:
        next_step = back + "It was written in the last 10 minutes; Move aside (into quarantine/) becomes available after that."
    else:
        next_step = back + "Moving it into quarantine/ is paused until the problem named above is fixed."
    return Finding(
        "runtime.leftover", WARN, leftover.key, ctx.display(leftover.path), observed, "",
        details=details, action=dict(ACTION) if offered else None, title=title, evidence=evidence,
        consequence=f"It takes {taken} and is never read unless its project folder comes back.",
        self_repair="Nothing: it stays until a person moves or deletes it.",
        next_step=next_step, problem_key=f"leftover:{leftover.key}")


def _split_findings(ctx: Context) -> list[Finding]:
    """F2: a project resolved by pid, but its pre-pid folder-key runtime
    folder is still sitting beside the pid folder, unmerged (a current server
    only merges it in when it resolves the project again)."""
    runtime = ctx.state_root / "projects"
    out: list[Finding] = []
    projects = ctx.projects()
    for project in projects:
        if project.read.outcome != "ok" or not project.pid:
            continue
        folder_key = normalize_agent_id(project.name)
        if folder_key == project.pid:
            continue
        # Another project may actively use this folder key, either as its own
        # (pid-less) folder name or as its pid. Reporting a split there would
        # tell someone to restart the server, which would merge that folder
        # into *this* project's pid folder and take the other project's data.
        if any(folder_key in other.keys_in_use for other in projects if other is not project):
            continue
        folder_dir = runtime / folder_key
        if folder_dir.is_symlink() or not folder_dir.is_dir():
            continue
        tree = measure_tree(folder_dir)
        # Forward only, as in reading.classify: a future-dated folder would
        # otherwise be skipped as "being written" on every run.
        if tree.newest is not None and 0 <= ctx.now - tree.newest < SPLIT_MIN_AGE_S:
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
            title=f"Project {project.name}'s runtime data is split in two",
            evidence=[ev("Folder-name copy", ctx.display(folder_dir))],
            consequence="Data in the folder-name copy isn't shown for this project.",
            self_repair="The server merges it into the pid folder the next time it starts.",
            next_step="Restart the server once.",
            problem_key=f"split:{project.name}",
        ))
    return out


def check(ctx: Context) -> list[Finding]:
    result = survey(ctx)
    out = [result.blocked] if result.blocked is not None else []
    names = _last_known_names(ctx, [leftover.key for leftover in result.leftovers]) if result.leftovers else {}
    out += [_finding(ctx, leftover, result.blocked is None, names) for leftover in result.leftovers]
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
    if not isinstance(key, str) or not is_safe_runtime_key(key):
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
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Path.resolve() on a symlink loop, which is not an OSError.
        detail = getattr(exc, "strerror", None) or exc
        raise DoctorError("doctor_move_failed", f"Could not move {shown}: {detail}. Nothing was moved.", 500) from exc
    logger.info("doctor: moved runtime leftover %s aside to %s", key, target)
    return printable({"moved": True, "key": key, "to": ctx.display(target),
                       "bytes": leftover.tree.bytes, "files": leftover.tree.files})
