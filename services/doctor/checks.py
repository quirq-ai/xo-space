"""The doctor's checks. Each takes a Context, returns findings, and writes nothing."""

from __future__ import annotations

from pathlib import Path

from services.doctor import inventory
from services.doctor.context import Context
from services.doctor.model import FAIL, OK, WARN, Finding
from services.doctor.reading import MAX_WALK_ENTRIES, ReadResult, readable_dir

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
