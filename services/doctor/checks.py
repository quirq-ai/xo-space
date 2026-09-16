"""The doctor's checks. Each takes a Context, returns findings, and writes nothing."""

from __future__ import annotations

import os
from pathlib import Path

from services.doctor import inventory
from services.doctor.context import Context
from services.doctor.model import FAIL, OK, WARN, Finding
from services.doctor.reading import MAX_WALK_ENTRIES, ReadResult, readable_dir
from services.timestamps import parse_ts

MAX_UNKNOWN_LISTED = 50


def roots(ctx: Context) -> list[Finding]:
    if readable_dir(ctx.projects_root):
        return []
    return [Finding(
        "roots.projects_unavailable", FAIL, "projects root", ctx.display(ctx.projects_root),
        "The projects folder is missing or can't be read.",
        "No project's .xo/ can be checked. Check that the folder is mounted and that XO_PROJECTS_ROOT is right.",
    )]


def _read_finding(ctx: Context, path: Path, subject: str, spec: inventory.Spec, result: ReadResult) -> Finding:
    keep = spec.klass == inventory.KEEP
    level = FAIL if keep else WARN
    if result.outcome == "schema_unsupported":
        finding_id = "schema.unsupported"
        observed, why = {
            "newer": (f"Schema {result.schema} was written by a newer xo-space than this one.",
                      "This xo-space refuses or ignores the file. Update xo-space on this machine."),
            "older": (f"Schema {result.schema} is older than this xo-space reads.",
                      "The store that owns this file refuses it, so what it holds is not in use."),
        }.get(result.detail, ("The file has no schema number.",
                              "The store that owns this file can't tell which version it is, so it refuses it."))
    else:
        finding_id = "read." + result.outcome
        observed = {
            "unreadable": f"The file can't be read ({result.detail}).",
            "empty": "The file is empty.",
            "invalid_json": f"The file is not valid JSON ({result.detail}).",
            "wrong_type": f"The file holds a JSON {result.detail}, not an object.",
        }[result.outcome]
        if result.outcome == "unreadable":
            why = "This is not corruption. Check the file's permissions and the disk; until then nothing can use it."
        elif keep:
            why = "The store that owns it can't use it, and what it records exists nowhere else."
        else:
            why = "It is rebuilt from other files. If this is still here after a minute, the writer that rebuilds it is failing."
    return Finding(finding_id, level, subject, ctx.display(path), observed, why,
                   details={"class": spec.klass, "outcome": result.outcome})


def _judge(ctx: Context, out: list[Finding], path: Path, subject: str, spec: inventory.Spec) -> None:
    if spec.klass == inventory.UNPARSED:
        return
    result = ctx.read(path, spec)
    if result.outcome not in ("ok", "absent", "recent"):
        out.append(_read_finding(ctx, path, subject, spec, result))


def reads(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    unknown: list[tuple[str, Path]] = []
    files, truncated = ctx.state_files()
    for path in files:
        rel = path.relative_to(ctx.state_root).as_posix()
        spec = inventory.spec_for(inventory.STATE, rel)
        if spec is None:
            unknown.append((rel, path))
            continue
        _judge(ctx, out, path, rel, spec)
    for name in inventory.names(inventory.WORKSPACE):
        _judge(ctx, out, ctx.projects_root / ".xo" / name, f"<projects root>/.xo/{name}",
               inventory.spec_for(inventory.WORKSPACE, name))
    for project in ctx.projects():
        for name in inventory.names(inventory.PROJECT):
            _judge(ctx, out, project.xo / name, f"{project.name}/.xo/{name}",
                   inventory.spec_for(inventory.PROJECT, name))
    for rel, path in unknown[:MAX_UNKNOWN_LISTED]:
        out.append(Finding("inventory.unknown_file", OK, rel, ctx.display(path),
                           "A file this version of the doctor doesn't know.", "Listed for information only."))
    if truncated:
        out.append(Finding("read.too_large", WARN, "state root", ctx.display(ctx.state_root),
                           f"The state folder has more than {MAX_WALK_ENTRIES:,} entries; the rest weren't checked.",
                           "Some state files were not checked for corruption."))
    return out


#: space.json refreshes at most this often by default (space_json.py:197).
SPACE_REFRESH_S = 60


def space_identity(ctx: Context) -> list[Finding]:
    """F1: space.json copies XO_SPACE_ID with no carry-forward (space_json.py:197)."""
    path = ctx.projects_root / ".xo" / "space.json"
    result = ctx.read(path, inventory.spec_for(inventory.WORKSPACE, "space.json"))
    if result.outcome != "ok":
        return []  # absent, or reported by reads()
    document = result.value
    shown = ctx.display(path)
    expected = (os.getenv("XO_SPACE_ID", "") or "").strip() or None
    stored = document.get("xo_space_id")
    out: list[Finding] = []
    if expected and stored is None:
        out.append(Finding("space.identity", FAIL, "space.json", shown,
                           "space.json has no xo_space_id, but XO_SPACE_ID is set.",
                           "Project sharing, usage reporting and Composio identify this Space by that id; the record no longer says which Space this is."))
    elif expected and stored != expected:
        out.append(Finding("space.identity", FAIL, "space.json", shown,
                           f"space.json names Space {stored}, but XO_SPACE_ID is {expected}.",
                           "The record describes a different Space than the one this server runs as."))
    elif not expected and stored:
        out.append(Finding("space.identity", WARN, "space.json", shown,
                           "XO_SPACE_ID is not set, but space.json has an xo_space_id.",
                           "The next write of space.json will erase the id. Set XO_SPACE_ID to keep it."))
    updated = parse_ts(document.get("updated_at"))
    stored_roots = document.get("roots") if isinstance(document.get("roots"), dict) else {}
    if updated is not None and ctx.now - updated.timestamp() >= SPACE_REFRESH_S:
        pairs = (("projects_root", ctx.projects_root), ("state_root", ctx.state_root))
        stale = [name for name, current in pairs
                 if isinstance(stored_roots.get(name), str)
                 and Path(stored_roots[name]).expanduser().resolve() != current]
        if stale:
            out.append(Finding("space.identity", WARN, "space.json roots", shown,
                               f"space.json records different {' and '.join(stale)} than this server uses.",
                               "Anything that reads the roots from space.json looks in the wrong folder until the watcher rewrites it."))
    return out


def duplicate_ids(ctx: Context) -> list[Finding]:
    """F2: two folders with one pid share one runtime folder."""
    by_pid: dict[str, list[str]] = {}
    for project in ctx.projects():
        if project.pid:
            by_pid.setdefault(project.pid, []).append(project.name)
    return [
        Finding("projects.duplicate_id", WARN, pid, ctx.display(ctx.projects_root),
                f"Folders {', '.join(names)} have the same pid {pid}.",
                "They write to one runtime folder, so their stats, sessions and timelines merge. Give one of them a new pid.")
        for pid, names in sorted(by_pid.items()) if len(names) > 1
    ]
