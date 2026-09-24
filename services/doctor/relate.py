"""One entry per underlying problem (#188 design §10).

Runs once on the assembled results, after every check. It moves a finding
under the finding that explains it: the moved finding's whole dict goes into
the parent's ``related`` and one line into its evidence, so nothing is
hidden, only grouped. It never invents or silently drops a problem. Each
check's level is recomputed from what it still holds.
"""

from __future__ import annotations

from typing import Iterable, Optional

from services.doctor.leftovers import describe_unknown
from services.doctor.model import CheckResult, Finding, ev, worst

_FILE_IDS = ("read.", "schema.")
#: read.* findings about the walk itself, not about one file.
_NOT_ONE_FILE = frozenset({"read.too_large", "read.truncated", "read.recent"})


def _attach(parent: Finding, child: Finding, label: str, value: str) -> None:
    parent.related.append(child.to_dict())
    parent.evidence.append(ev(label, value))


def _fold_leftover_files(by_family: dict[str, CheckResult]) -> None:
    runtime, read = by_family.get("runtime"), by_family.get("read")
    if runtime is None or read is None:
        return
    parents = {f.subject: f for f in runtime.findings if f.id == "runtime.leftover"}
    if not parents:
        return
    kept: list[Finding] = []
    for finding in read.findings:
        parts = finding.subject.split("/")
        parent = parents.get(parts[1]) if len(parts) >= 3 and parts[0] == "projects" else None
        if parent is not None and finding.id.startswith(_FILE_IDS) and finding.id not in _NOT_ONE_FILE:
            _attach(parent, finding, "Also damaged inside", f"{'/'.join(parts[2:])}: {finding.observed}")
        else:
            kept.append(finding)
    read.findings = kept


def _unblock_named_projects(by_family: dict[str, CheckResult]) -> None:
    runtime, read = by_family.get("runtime"), by_family.get("read")
    if runtime is None or read is None:
        return
    own = {f.subject.split("/", 1)[0]: f for f in read.findings
           if f.subject.endswith("/.xo/project.json") and f.id.startswith(_FILE_IDS)}
    if not own:
        return
    for blocked in [f for f in runtime.findings if f.id == "runtime.keys_unknown"]:
        entries = blocked.details.get("projects") or []
        rest = [entry for entry in entries if entry.get("name") not in own]
        for entry in entries:
            target = own.get(entry.get("name"))
            if target is not None:
                target.evidence.append(ev(
                    "Leftover checks", "Paused until this file can be read: this project's runtime data can't be "
                                       "told apart from leftovers, and nothing can be moved aside."))
        if len(rest) == len(entries):
            continue
        if not rest:
            runtime.findings.remove(blocked)
            continue
        blocked.subject, blocked.observed = describe_unknown(rest)
        blocked.details["projects"] = rest


def _fold_projects_root(by_family: dict[str, CheckResult]) -> None:
    roots, runtime = by_family.get("roots"), by_family.get("runtime")
    if roots is None or runtime is None:
        return
    parent = next((f for f in roots.findings if f.id == "roots.projects_unavailable"), None)
    child = next((f for f in runtime.findings if f.id == "runtime.projects_root_suspect"), None)
    if parent is not None and child is not None:
        runtime.findings.remove(child)
        _attach(parent, child, "Runtime data folders", child.observed)


def _fold_under(by_family: dict[str, CheckResult], child_family: str, child_id: str, parent_family: str,
                parent_ids: Iterable[str], parent_subject: Optional[str]) -> None:
    children, parents = by_family.get(child_family), by_family.get(parent_family)
    if children is None or parents is None:
        return
    ids = frozenset(parent_ids)
    parent = next((f for f in parents.findings
                   if f.id in ids and (parent_subject is None or f.subject == parent_subject)), None)
    if parent is None:
        return
    kept: list[Finding] = []
    for finding in children.findings:
        if finding.id == child_id:
            _attach(parent, finding, "Also affected", finding.title or finding.observed)
        else:
            kept.append(finding)
    children.findings = kept


def relate(results: list[CheckResult]) -> list[CheckResult]:
    by_family = {result.id: result for result in results}
    _fold_leftover_files(by_family)
    _unblock_named_projects(by_family)
    _fold_projects_root(by_family)
    _fold_under(by_family, "scheduler", "scheduler.overdue", "watcher",
                ("watcher.stopped", "watcher.heartbeat"), None)
    _fold_under(by_family, "connections", "connections.overdue", "components",
                ("component.crashed", "component.exited"), "connections poller")
    _fold_under(by_family, "github", "github.stale", "components",
                ("component.crashed", "component.exited"), "github poller")
    for result in results:
        if result.error is None:
            result.level = worst(finding.level for finding in result.findings)
    return results
