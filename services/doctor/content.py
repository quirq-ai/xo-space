"""Documents that parse but that their owning store can't use.

``read`` says a file is valid JSON with an accepted schema number. This says
whether what is inside is usable, by asking the owning store's own pure
``shape_problem(document)``: one rule, with one definition, that the store
applies when it reads. A store is imported inside the check, so a renamed
function turns this one check into ERROR instead of stopping the server from
importing the doctor router.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Iterator

from services.doctor import catalog, inventory
from services.doctor.context import Context
from services.doctor.model import Finding, ev
from services.doctor.reading import ReadResult

#: (base, inventory pattern) → (module, function) of the store that owns it.
VALIDATORS: dict[tuple[str, str], tuple[str, str]] = {
    (inventory.STATE, "inbox/inbox.json"): ("services.inbox.store", "shape_problem"),
    (inventory.STATE, "scheduler/jobs.json"): ("utils.commands.scheduler", "shape_problem"),
    (inventory.STATE, "scheduler/state.json"): ("utils.commands.scheduler", "shape_problem"),
    (inventory.PROJECT, "todos.json"): ("services.cowork_agent.visualizer.todos_store", "shape_problem"),
    (inventory.PROJECT, "workitems.json"): ("services.cowork_agent.visualizer.workitems_store", "shape_problem"),
    (inventory.PROJECT, "peers.json"): ("services.cowork_agent.visualizer.peers_store", "shape_problem"),
}

#: A store's reason can quote a key from the file; keep it to a line.
MAX_REASON_CHARS = 200

#: Catalog situations to look up, most specific first: a store that drops
#: content it can't use behaves like one handed the wrong kind of data.
_SITUATIONS = ("wrong_shape", "wrong_type")


def _documents(ctx: Context, base: str, pattern: str) -> Iterator[tuple[Path, str]]:
    if base == inventory.STATE:
        yield ctx.state_root / pattern, pattern
    elif base == inventory.PROJECT:
        for project in ctx.projects():
            yield project.xo / pattern, f"{project.name}/.xo/{pattern}"


def _finding(ctx: Context, path: Path, subject: str, spec: inventory.Spec, result: ReadResult,
             reason: str) -> Finding:
    about = catalog.about(spec)
    values = catalog.labels(spec, subject, ctx.project_label)
    name = catalog.fill(about.name, values)
    reason = reason[:MAX_REASON_CHARS]

    def text(part: str) -> str:
        return catalog.fill(about.text(_SITUATIONS, part), values)

    evidence = catalog.read_evidence(about, result, inventory.accepted(spec), ctx.now, values)
    evidence.append(ev("Problem", reason))
    return Finding(
        "content.wrong_shape", catalog.level_for(spec, "wrong_shape", watcher_alive=True), subject,
        ctx.display(path), f"The file is valid JSON, but {reason}.", "",
        details={"class": spec.klass, "behaviour": spec.behaviour, "outcome": "wrong_shape"},
        title=f"{name} {catalog.PHRASE['wrong_shape']}", evidence=evidence,
        consequence=text("consequence"), self_repair=text("self_repair"), next_step=text("next_step"),
        problem_key=f"file:{subject}")


def check(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for (base, pattern), (module, function) in VALIDATORS.items():
        validate = getattr(import_module(module), function)
        spec = inventory.spec_for(base, pattern)
        for path, subject in _documents(ctx, base, pattern):
            result = ctx.read(path, spec)
            if result.outcome != "ok":
                continue  # absent, or already reported by the read check
            reason = validate(result.value)
            if reason:
                out.append(_finding(ctx, path, subject, spec, result, reason))
    return out
