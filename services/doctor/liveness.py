"""Is each background part of the Space alive and succeeding? (#188 design §7)

Two layers. The in-process task record (``services/background.py``, read as
``ctx.components``) says whether a task crashed, when, and with what; it is
empty when the doctor runs outside the server. What each component leaves on
disk (heartbeat, poll records, mirrors, schedules) says whether a loop that
is still alive is stuck, and carries across restarts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from services.cowork_agent import runtime_config
from services.cowork_agent.visualizer.state import watcher_heartbeat_path
from services.doctor import inventory
from services.doctor.context import Context
from services.doctor.model import FAIL, WARN, Finding, ago, ev, moment
from services.timestamps import parse_ts
from utils import runtime_env

WATCHER = "watcher"


def watcher_enabled() -> bool:
    """``QUIRQ_WATCHER_ENABLED`` (default true), parsed the same way
    ``runtime_config.effective_settings`` does, but without resolving the
    active agent: that also happens inside ``effective_settings`` and has
    nothing to do with these two watcher env vars, so a broken agent setup
    must not turn the watcher or layout check into ERROR."""
    as_bool = getattr(runtime_config, "_as_bool", None)
    if as_bool is None:  # pragma: no cover - defensive fallback only
        def as_bool(value: str | None, *, default: bool) -> bool:
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return as_bool(os.getenv("QUIRQ_WATCHER_ENABLED"), default=True)


def stale_after() -> float:
    # Borrowed inside the function: a rename upstream must turn one check
    # into an ERROR result, not stop the server importing the doctor router.
    from services.cowork_agent.quirq_catalog import _stale_after_seconds  # the Quirq view's liveness rule, shared

    return _stale_after_seconds(runtime_env.watcher_tick_interval_seconds())


def heartbeat_age(ctx: Context, path: Optional[Path] = None,
                  spec: Optional[inventory.Spec] = None) -> Optional[float]:
    """Seconds since ``last_tick_at`` in the heartbeat at ``path`` (default:
    the current one), or None when there is no readable stamp."""
    if path is None:
        path = watcher_heartbeat_path()
        spec = inventory.spec_for(inventory.STATE, "cache/heartbeat.json")
    value = ctx.read(path, spec).value
    stamp = parse_ts(value.get("last_tick_at")) if isinstance(value, dict) else None
    return None if stamp is None else max(0.0, ctx.now - stamp.timestamp())


def watcher_dead(ctx: Context) -> bool:
    """The task record says the watcher task ended (crashed or returned)."""
    record = ctx.components.get(WATCHER)
    return record is not None and record.get("state") in ("crashed", "returned")


def watcher_alive(ctx: Context) -> bool:
    """Enabled, not known dead, and its heartbeat is fresh."""
    if not watcher_enabled() or watcher_dead(ctx):
        return False
    age = heartbeat_age(ctx)
    return age is not None and age <= stale_after()


def usage_state_path(ctx: Context) -> Optional[Path]:
    """The usage-report bookmark the server actually reads: USAGE_SYNC_STATE_FILE,
    else ``usage/<active agent>.json`` (services/usage_sync.py:40-44). None
    when the active agent can't be resolved."""
    override = (os.getenv("USAGE_SYNC_STATE_FILE", "") or "").strip()
    try:
        if override:
            return Path(override).expanduser().resolve()
        from services.cowork_agent.registry.agent_registry import get_active_agent

        return ctx.state_root / "usage" / f"{get_active_agent().name}.json"
    except Exception:  # noqa: BLE001 - an unresolvable agent judges every file instead
        return None


#: A stale heartbeat is a FAIL after this long: by then stats, timelines,
#: scheduled commands and the Inbox session feed are visibly frozen.
WATCHER_FAIL_AFTER_S = 300
#: Consecutive ticks with a failed step before a WARN, and before a FAIL.
WATCHER_FAILING_WARN = 5
WATCHER_FAILING_FAIL = 60
#: Consecutive failed passes of any other registered task before a WARN.
COMPONENT_FAILING_WARN = 3

RESTART = "Restart the server. If it stops again, the server log has the error."

#: What each registered task does for a person: (label, what stops when it stops).
COMPONENTS: dict[str, tuple[str, str]] = {
    "watcher": ("The watcher",
                "Stats, timelines, live presence, session lists, scheduled commands and the Inbox's session feed "
                "stop updating."),
    "connections poller": ("The connections poller",
                           "New items from connected apps (such as email) stop reaching the Inbox."),
    "github poller": ("The GitHub poller", "Projects' GitHub issue copies stop refreshing."),
    "usage sync": ("Usage reporting", "Usage stops being reported to XO."),
    "relay poller": ("The project-sharing relay",
                     "Shared projects stop receiving collaborators' commits, and newly shared projects aren't cloned."),
    "gateway reconcile": ("The connector gateway sweep",
                          "Agents' connector settings are no longer kept pointed at this Space."),
}


def _label(name: str) -> tuple[str, str]:
    return COMPONENTS.get(name, (name.capitalize(), "Its work stops."))


def _when(ts: object, now: float) -> str:
    return moment(ts, now) if isinstance(ts, (int, float)) else "an unknown time"


def _stopped(ctx: Context, name: str, record: dict, *, path: str = "",
             heartbeat_fresh: bool = False) -> Finding:
    """A task that ended while the server runs: crashed (FAIL) or returned."""
    label, stops = _label(name)
    crashed = record.get("state") == "crashed"
    when = _when(record.get("ended_at"), ctx.now)
    evidence = []
    if isinstance(record.get("started_at"), (int, float)):
        evidence.append(ev("Started", moment(record["started_at"], ctx.now)))
    evidence.append(ev("Stopped", when))
    if crashed:
        evidence.append(ev("Error", record.get("error") or "unknown"))
    if heartbeat_fresh:
        evidence.append(ev("Note", "Its heartbeat is still fresh, so another xo-space server is writing this "
                                   "state folder."))
    if name == WATCHER:
        finding_id, level = "watcher.stopped", FAIL
    elif crashed:
        finding_id, level = "component.crashed", FAIL
    else:
        finding_id, level = "component.exited", WARN
    observed = (f"{label} stopped at {when} with {record.get('error') or 'an unknown error'}." if crashed
                else f"{label} exited at {when} and is no longer running.")
    return Finding(finding_id, level, name, path, observed, "",
                   details={"state": record.get("state")},
                   title=f"{label} {'crashed' if crashed else 'stopped'}", evidence=evidence,
                   consequence=stops, self_repair="Nothing restarts it by itself.", next_step=RESTART,
                   problem_key=f"component:{name}")


def watcher(ctx: Context) -> list[Finding]:
    """Crashed, stuck or failing (#188 design §7; decision 3)."""
    if not watcher_enabled():
        return []
    shown = ctx.display(watcher_heartbeat_path())
    record = ctx.components.get(WATCHER)
    age = heartbeat_age(ctx)
    limit = stale_after()
    _, stops = _label(WATCHER)
    if watcher_dead(ctx):
        return [_stopped(ctx, WATCHER, record, path=shown, heartbeat_fresh=age is not None and age <= limit)]
    out: list[Finding] = []
    if age is None:
        out.append(Finding(
            "watcher.heartbeat", WARN, "watcher", shown,
            "The watcher is enabled but there is no readable heartbeat yet.", "",
            title="The watcher hasn't reported yet", consequence=stops,
            self_repair="The watcher writes a heartbeat after every tick; a starting server writes one within seconds.",
            next_step="If this lasts more than a minute, restart the server.", problem_key="component:watcher"))
    elif age > limit:
        since = ctx.now - age
        observed = f"The watcher is enabled but last ticked {ago(age)} ago."
        if record is not None:
            observed += " Its task is still running, so a tick is stuck or fails before it finishes."
        evidence = [ev("Last tick", moment(since, ctx.now)), ev("Expected every", f"{limit:g} seconds or less")]
        if record is not None and record.get("last_failure"):
            evidence.append(ev("Last error", record["last_failure"]))
        out.append(Finding(
            "watcher.heartbeat", FAIL if age > WATCHER_FAIL_AFTER_S else WARN, "watcher", shown, observed, "",
            title="The watcher is stuck" if record is not None else "The watcher has stopped ticking",
            evidence=evidence, consequence=f"{stops} Nothing has updated since {moment(since, ctx.now)}.",
            self_repair="Nothing restarts it by itself.", next_step=RESTART, problem_key="component:watcher"))
    failures = record.get("consecutive_failures", 0) if record is not None else 0
    if failures >= WATCHER_FAILING_WARN:
        out.append(Finding(
            "watcher.failing", FAIL if failures >= WATCHER_FAILING_FAIL else WARN, "watcher", shown,
            f"The watcher keeps ticking, but part of its work failed on each of the last {failures:,} ticks.", "",
            title="Part of the watcher's work keeps failing",
            evidence=[ev("Last error", record.get("last_failure") or "unknown"),
                      ev("Last fully clean tick", _when(record.get("last_tick_ok_at"), ctx.now)
                         if record.get("last_tick_ok_at") else "none since the server started")],
            consequence=("Whatever the failing step feeds (usage totals, timelines, session lists or the "
                         "Space-wide views) stops updating, although the heartbeat looks fresh."),
            self_repair="The watcher retries every tick; it recovers only when the cause goes away.",
            next_step=("Read the error above; the server log has the full traceback. Fix the file or setting it "
                       "names, then run checks again."),
            problem_key="component:watcher:failing"))
    return out


def components(ctx: Context) -> list[Finding]:
    """Every other registered task: crashed, returned unexpectedly, or failing."""
    out: list[Finding] = []
    for name, record in sorted(ctx.components.items()):
        if name == WATCHER:
            continue
        state = record.get("state")
        if state == "crashed" or (state == "returned" and not record.get("finishes_by_design")):
            out.append(_stopped(ctx, name, record))
            continue
        failures = record.get("consecutive_failures", 0)
        if state == "running" and failures >= COMPONENT_FAILING_WARN:
            label, stops = _label(name)
            ok_at = record.get("last_tick_ok_at")
            out.append(Finding(
                "component.failing", WARN, name, "", f"{label} is running, but its last {failures} passes failed.",
                "", title=f"{label} keeps failing",
                evidence=[ev("Last error", record.get("last_failure") or "unknown"),
                          ev("Last success", _when(ok_at, ctx.now) if ok_at else "none since the server started")],
                consequence=stops, self_repair="It retries on its own schedule; it recovers only when the cause goes away.",
                next_step="Read the error above; the server log has the details.",
                problem_key=f"component:{name}:failing"))
    return out
