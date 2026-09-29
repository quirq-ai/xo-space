"""History files: the Space timeline and each project's timeline (#188 RC5).

v1 never opened them (class UNPARSED), so an emptied Space timeline went
unreported. Two facts make the check exact: nothing rotates or truncates the
Space timeline, and nothing ever creates it empty, so "exists and 0 bytes"
only happens when its history was removed; "absent" is legitimate (older
installs, migrated timelines). Lines are counted, never quoted.
"""

from __future__ import annotations

import json
import os
import stat

from services.doctor.context import Context
from services.doctor.model import WARN, Finding, ev, moment, size
from services.doctor.reading import read_tail

#: How much of each timeline (from the end) is checked.
TAIL_BYTES = 1024 * 1024
#: The most read across all timelines in one run.
RUN_BUDGET_BYTES = 16 * 1024 * 1024

SPACE_TIMELINE = "projects/timeline.jsonl"


def _count_invalid(data: bytes, seeked: bool) -> tuple[int, int, int]:
    """(bad lines, first bad line number, lines checked). The line cut by a
    seek, and a last line with no newline yet (being written), are skipped."""
    lines = data.split(b"\n")
    if seeked and lines:
        lines = lines[1:]
    if lines and not data.endswith(b"\n"):
        lines = lines[:-1]
    bad = first = checked = 0
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        checked += 1
        try:
            good = isinstance(json.loads(raw.decode("utf-8")), dict)
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            good = False
        if not good:
            bad += 1
            first = first or number
    return bad, first, checked


def check(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    space = ctx.state_root / "projects" / "timeline.jsonl"
    try:
        info = os.lstat(space)
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and info.st_size == 0:
        out.append(Finding(
            "history.empty", WARN, SPACE_TIMELINE, ctx.display(space),
            "The Space timeline exists but is empty.", "",
            title="The Space's activity history is empty",
            evidence=[ev("Last modified", moment(info.st_mtime, ctx.now))],
            consequence=("The Space activity feed shows nothing before now, and the Inbox gets no items from "
                         "activity until new events arrive. Nothing in xo-space writes an empty timeline, so its "
                         "history was removed."),
            self_repair="New activity is added as it happens; the lost history isn't rebuilt.",
            next_step="If you have an earlier copy of this file, put it back while the server is stopped.",
            problem_key=f"file:{SPACE_TIMELINE}"))
    budget = RUN_BUDGET_BYTES
    try:
        project_timelines = sorted((ctx.state_root / "projects").glob("*/timeline.jsonl"))
    except OSError:
        project_timelines = []
    for path in [space, *project_timelines]:
        if budget <= 0:
            break
        tail = read_tail(path, min(TAIL_BYTES, budget))
        if tail is None:
            continue
        data, seeked = tail
        budget -= len(data)
        bad, first, checked = _count_invalid(data, seeked)
        if not bad:
            continue
        rel = path.relative_to(ctx.state_root).as_posix()
        is_space = rel == SPACE_TIMELINE
        name = "The Space timeline" if is_space else f"Project {ctx.project_label(path.parent.name)}'s timeline"
        where = f" of the last {size(len(data))}" if seeked else ""
        out.append(Finding(
            "history.invalid_lines", WARN, rel, ctx.display(path),
            f"{bad} of {checked} lines{where} aren't valid events; the first is line {first}{where}.", "",
            title=f"{name} has damaged lines",
            evidence=[ev("Damaged lines", bad), ev("Lines checked", checked), ev("First damaged line", first)],
            consequence=("Damaged lines are skipped, so those events are missing from the activity feed"
                         + (" and the Inbox." if is_space else ".")),
            self_repair="Nothing: new events are added after them.",
            next_step=("Nothing is needed unless you want those events back; the lines can be removed by hand while "
                       "the server is stopped."),
            problem_key=f"file:{rel}:lines"))
    return out
