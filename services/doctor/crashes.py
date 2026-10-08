"""Failures the health record kept (services/health), read back.

The other checks judge what the disk and the running tasks show now; this
one says what broke since, and how often: a crash the restart erased from
the task record, a run that ended without shutting down, a request that
failed with a server error, a store that refused a damaged file. Records not
seen for :data:`SHOWN_FOR_S` drop out of the report (the store keeps them
until its own retention removes them). Reads only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from services.doctor import inventory, liveness
from services.doctor.context import Context
from services.doctor.model import FAIL, WARN, Finding, ev, moment
from services.timestamps import parse_ts

#: A record last seen longer ago than this isn't reported.
SHOWN_FOR_S = 7 * 86400
#: A crash this recent is a FAIL; an older one a WARN.
RECENT_S = 86400
#: This many crashes of one kind within RECENT_S: repeating.
REPEATING_COUNT = 3
#: This many unclean exits within SHOWN_FOR_S: a FAIL.
UNCLEAN_FAIL_COUNT = 2
#: Code locations shown as evidence, innermost first.
FRAMES_SHOWN = 5

#: The store a refusal came from, as a person names it.
STORES = {"todos": "The todo list", "workitems": "The work-item list", "peers": "The collaborator list",
          "inbox": "The Inbox"}


def _events_dir(ctx: Context) -> Path:
    return ctx.state_root / "setup" / "health" / "events"


def _ts(value: object) -> Optional[float]:
    stamp = parse_ts(value) if isinstance(value, str) else None
    return None if stamp is None else stamp.timestamp()


def _label(component: str) -> tuple[str, str]:
    return liveness.COMPONENTS.get(component, (component.capitalize(), "Its work stops until it runs again."))


def _occurrences(event: dict[str, Any]) -> list[float]:
    found = event.get("occurrences") if isinstance(event.get("occurrences"), list) else []
    return [stamp for stamp in (_ts(o.get("at")) for o in found if isinstance(o, dict)) if stamp is not None]


def _version(ctx: Context, event: dict[str, Any]) -> Optional[str]:
    """The xo-space version of the run the last occurrence came from."""
    found = event.get("occurrences") if isinstance(event.get("occurrences"), list) else []
    boot_id = found[-1].get("boot_id") if found and isinstance(found[-1], dict) else None
    if not isinstance(boot_id, str) or not boot_id.isalnum():
        return None
    notes = ctx.read(ctx.state_root / "setup" / "health" / "boots" / f"{boot_id}.json",
                     inventory.spec_for(inventory.STATE, "setup/health/boots/x.json"))
    value = notes.value.get("version") if notes.outcome == "ok" else None
    return value if isinstance(value, str) else None


def _evidence(ctx: Context, event: dict[str, Any], first: Optional[float], last: float) -> list[dict[str, str]]:
    rows = [ev("Happened", f"{int(event.get('count') or 1):,} time(s)"),
            ev("Last", moment(last, ctx.now))]
    if first is not None and first < last:
        rows.append(ev("First", moment(first, ctx.now)))
    error = event.get("error_type") or "?"
    message = event.get("message") or ""
    rows.append(ev("Error", f"{error}: {message}" if message else error))
    frames = event.get("frames") if isinstance(event.get("frames"), list) else []
    for frame in list(reversed(frames))[:FRAMES_SHOWN]:
        if isinstance(frame, dict):
            rows.append(ev("Where", f"{frame.get('file')}:{frame.get('line')} in {frame.get('function')}"))
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if details.get("ended_around"):
        ended = _ts(details["ended_around"])
        rows.append(ev("Ended around", moment(ended, ctx.now) if ended else str(details["ended_around"])))
    version = _version(ctx, event)
    if version:
        rows.append(ev("Version", version))
    return rows


def _finding(ctx: Context, path: Path, event: dict[str, Any]) -> Optional[Finding]:
    last = _ts(event.get("last_seen"))
    if last is None or ctx.now - last > SHOWN_FOR_S:
        return None
    first = _ts(event.get("first_seen"))
    kind, component = event.get("kind"), str(event.get("component") or "?")
    subject = str(event.get("subject") or "")
    signature = str(event.get("signature") or path.stem)
    recent = [stamp for stamp in _occurrences(event) if 0 <= ctx.now - stamp <= RECENT_S]
    evidence = _evidence(ctx, event, first, last)
    shown = ctx.display(path)
    common = {"evidence": evidence, "problem_key": f"event:{signature}",
              "details": {"component": component, "kind": kind, "subject": subject or None,
                          "signature": signature, "count": event.get("count")}}
    when = f"last {moment(last, ctx.now)}"
    label, stops = _label(component)

    if kind in ("crash", "fatal"):
        repeating = len(recent) >= REPEATING_COUNT
        level = FAIL if recent else WARN
        if kind == "fatal":
            title, observed = "The server stopped with a fatal error", f"A fatal error stopped the server, {when}."
            consequence = "The server stopped at once; work in progress was cut off."
            next_step = ("If it happens again, report it with the evidence below; the full dump is in "
                         "setup/health/fatal.log.1.")
        else:
            title = f"{label} {'keeps crashing' if repeating else 'crashed'}"
            observed = f"{label} crashed, {when}."
            consequence = stops
            next_step = "If it keeps happening, the evidence names where; the server log has the full traceback."
        return Finding("crash.repeating" if repeating else "crash.recent", level, component, shown, observed, "",
                       title=title, consequence=consequence,
                       self_repair="Nothing restarts it by itself; restarting the server does.",
                       next_step=next_step, **common)
    if kind == "exit":
        return Finding("crash.exit", WARN, component, shown, f"{label} stopped running, {when}.", "",
                       title=f"{label} stopped running", consequence=stops,
                       self_repair="Nothing restarts it by itself; restarting the server does.",
                       next_step="Restart the server. If it stops again, the server log says why.", **common)
    if kind == "failing":
        where = f": {subject}" if subject else ""
        title = (f"Part of the watcher's work failed{where}" if component == "watcher"
                 else f"{label} kept failing")
        return Finding("crash.failing", WARN, component, shown, f"It failed repeatedly, {when}.", "",
                       title=title,
                       consequence=(stops if component != "watcher" else
                                    "Whatever that step feeds (usage totals, timelines, session lists, the Space-wide "
                                    "views) missed the work it failed on."),
                       self_repair="It retries; it recovers only when the cause goes away.",
                       next_step="The evidence names the error and where; fix the file or setting it points to.",
                       **common)
    if kind == "refusal":
        store = STORES.get(component, component.capitalize())
        return Finding("crash.refusal", WARN, component, shown,
                       f"{store} refused to use {subject or 'its file'}, {when}.", "",
                       title=f"{store} refused a damaged file",
                       consequence="Changes to it failed while the file was damaged.",
                       self_repair="Nothing was overwritten: the store refuses a file it can't use.",
                       next_step=("If the file is still damaged, its own finding says what to do; otherwise "
                                  "nothing is needed."), **common)
    if kind == "http_500":
        return Finding("crash.http_500", WARN, component, shown,
                       f"A request to {subject or 'the server'} failed with a server error, {when}.", "",
                       title=f"Requests failed with a server error: {subject or 'unknown route'}",
                       consequence="Those requests failed for whoever made them.",
                       self_repair="Nothing.",
                       next_step="The evidence names where in the code it failed; the server log has the traceback.",
                       **common)
    if kind == "unclean_exit":
        repeated = len([stamp for stamp in _occurrences(event) if ctx.now - stamp <= SHOWN_FOR_S])
        return Finding("crash.unclean_exit", FAIL if repeated >= UNCLEAN_FAIL_COUNT else WARN, "server", shown,
                       f"A run of the server ended without shutting down, {when}.", "",
                       title="The server stopped without shutting down",
                       consequence=("Work in progress was cut off; a scheduled command running then is recorded "
                                    "as lost."),
                       self_repair="The server started again; nothing else is repaired.",
                       next_step=("If it keeps happening, check the machine's memory (the out-of-memory killer) "
                                  "and the server log around the time it ended."), **common)
    return None


def check(ctx: Context) -> list[Finding]:
    folder = _events_dir(ctx)
    spec = inventory.spec_for(inventory.STATE, "setup/health/events/x.json")
    try:
        paths = sorted(p for p in folder.glob("*.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return []
    found: list[tuple[float, Finding]] = []
    for path in paths:
        result = ctx.read(path, spec)
        if result.outcome != "ok":
            continue  # damage is the read check's to report
        finding = _finding(ctx, path, result.value)
        if finding is not None:
            found.append((_ts(result.value.get("last_seen")) or 0.0, finding))
    return [finding for _, finding in sorted(found, key=lambda pair: -pair[0])]
