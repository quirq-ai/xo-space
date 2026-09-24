"""Is each background part of the Space alive and succeeding? (#188 design §7)

Two layers. The in-process task record (``services/background.py``, read as
``ctx.components``) says whether a task crashed, when, and with what; it is
empty when the doctor runs outside the server. What each component leaves on
disk (heartbeat, poll records, mirrors, schedules) says whether a loop that
is still alive is stuck, and carries across restarts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from services import background
from services.cowork_agent import runtime_config
from services.cowork_agent.helpers import normalize_agent_id
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


def _poller_recently_started(ctx: Context, name: str, grace: float) -> bool:
    """The task record for ``name`` says it's running and started less than
    ``grace`` seconds ago: it hasn't had its chance to poll again yet since
    this start, so a stale-looking file on disk (from before the restart)
    isn't yet evidence the poller stopped."""
    record = ctx.components.get(name)
    if record is None or record.get("state") != "running":
        return False
    started = record.get("started_at")
    return isinstance(started, (int, float)) and ctx.now - started < grace


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
        # The heartbeat is only evidence of another server when it was
        # written AFTER this one ended, not merely when it looks fresh: a
        # heartbeat that predates the crash is this watcher's own last beat.
        heartbeat_at = None if age is None else ctx.now - age
        ended_at = record.get("ended_at")
        fresh = (heartbeat_at is not None and isinstance(ended_at, (int, float)) and heartbeat_at > ended_at)
        return [_stopped(ctx, WATCHER, record, path=shown, heartbeat_fresh=fresh)]
    out: list[Finding] = []
    if age is not None and record is not None and record.get("state") == "running" \
            and isinstance(record.get("started_at"), (int, float)):
        # A heartbeat older than the process itself is left over from before
        # a restart: judge staleness against how long this process has run,
        # not the file's raw age.
        age = min(age, ctx.now - record["started_at"])
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


#: Grace past a connection's interval before it counts as overdue: two poller
#: ticks plus the worst case of one poll (handshake, collectors, account lookup).
CONNECTIONS_GRACE_S = 300
#: A connection failing longer than max(3 × its interval, this) is reported.
CONNECTIONS_FAILING_MIN_S = 3600
#: A mirror older than max(3 × the poll interval, this) is stale.
GITHUB_STALE_MIN_S = 300


def _ts(value: object) -> Optional[float]:
    stamp = parse_ts(value) if isinstance(value, str) else None
    return None if stamp is None else stamp.timestamp()


def _connections_settings() -> tuple[bool, float]:
    from services.connections.poller import poller_enabled, tick_seconds

    return poller_enabled(), tick_seconds()


def _live_error(toolkit: str, stored: object) -> Optional[str]:
    """The stored error unless it is a stale gate the person has since opened
    (the same filter the Connections card applies)."""
    if not isinstance(stored, str) or not stored:
        return None
    try:
        from services.connections.service import _live_last_error

        return _live_last_error(toolkit, stored)
    except Exception:  # noqa: BLE001 - when in doubt, show the error
        return stored


def _interval(config: dict) -> float:
    value = config.get("interval_s")
    return float(value) if isinstance(value, (int, float)) and 60 <= value <= 86400 else 900.0


def connections(ctx: Context) -> list[Finding]:
    """#188 issues 1 and 2: a stopped poller, a connection failing for hours."""
    enabled, tick = _connections_settings()
    if not enabled:
        return []
    root = ctx.state_root / "connections"
    try:
        folders = sorted(p for p in root.iterdir()
                         if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return []
    config_spec = inventory.spec_for(inventory.STATE, "connections/x/config.json")
    state_spec = inventory.spec_for(inventory.STATE, "connections/x/state.json")
    out: list[Finding] = []
    for folder in folders:
        toolkit, label = folder.name, folder.name.capitalize()
        config = ctx.read(folder / "config.json", config_spec)
        if config.outcome != "ok" or config.value.get("enabled") is not True:
            continue
        state = ctx.read(folder / "state.json", state_spec)
        if state.outcome != "ok":
            continue  # never polled yet, or damaged (the read check reports that)
        interval = _interval(config.value)
        last_poll, last_ok = _ts(state.value.get("last_poll_at")), _ts(state.value.get("last_ok_at"))
        shown = ctx.display(folder / "state.json")
        grace = interval + 2 * tick + CONNECTIONS_GRACE_S
        overdue = (last_poll is not None and ctx.now - last_poll > grace
                  and not _poller_recently_started(ctx, "connections poller", grace))
        if overdue:
            out.append(Finding(
                "connections.overdue", WARN, toolkit, shown,
                f"{label} was last checked {ago(ctx.now - last_poll)} ago; it's due every {ago(interval)}.", "",
                title=f"{label} is no longer being checked",
                evidence=[ev("Last checked", moment(last_poll, ctx.now)), ev("Checked every", ago(interval)),
                          ev("Overdue by", ago(ctx.now - last_poll - interval))],
                consequence=f"New {label} items don't reach the Inbox.",
                self_repair="Nothing: the connections poller appears to have stopped.",
                next_step=RESTART, problem_key=f"connection:{toolkit}:overdue"))
        error = _live_error(toolkit, state.value.get("last_error"))
        failing_for = None if last_ok is None else ctx.now - last_ok
        if error and (failing_for is None or failing_for > max(3 * interval, CONNECTIONS_FAILING_MIN_S)):
            shown_error = background.redact(error)
            synced = "has never synced" if failing_for is None else f"hasn't synced for {ago(failing_for)}"
            out.append(Finding(
                "connections.failing", WARN, toolkit, shown,
                f"{label} {synced}; its last attempt failed with: {shown_error}", "",
                title=f"{label} keeps failing to sync",
                evidence=[ev("Last success", moment(last_ok, ctx.now) if last_ok else "never"),
                          ev("Last attempt", moment(last_poll, ctx.now) if last_poll else "unknown"),
                          ev("Error", shown_error)],
                consequence=f"New {label} items don't reach the Inbox until this is fixed.",
                self_repair=f"It's retried every {ago(interval)}, but the same error comes back.",
                next_step=(f"Fix what the error says (for example, turn {label} on for this workspace in "
                           f"Connections), then use Poll now on its card."),
                problem_key=f"connection:{toolkit}:failing"))
    return out


_GITHUB_NEXT = {
    "not_found": "Check that the project's git remote points at a repository that exists and that you can see.",
    "forbidden": "Check your GitHub access to this repository.",
    "no_cli": "Install the GitHub CLI (gh) on this machine.",
    "not_authenticated": "Sign the GitHub CLI in on this machine (gh auth login).",
    "rate_limited": "Nothing to do: polling resumes when GitHub's hourly limit resets.",
}


def _github_repo(project_json: Optional[dict]) -> Optional[str]:
    git = project_json.get("git") if isinstance(project_json, dict) else None
    if not isinstance(git, dict):
        return None
    from services.cowork_agent.connectors.github.issues import parse_remote_url

    ref = parse_remote_url(git.get("remote_url"))
    return ref.slug if ref is not None and ref.is_github_com else None


def _github_pauses(ctx: Context) -> tuple[str, set[str]]:
    """In the server only: the poller's pause reason ("" when running) and
    the repositories in cooldown. Outside the server: nothing is known."""
    if "github poller" not in ctx.components:
        return "", set()
    try:
        from services.cowork_agent import github_poller

        snapshot = github_poller.budget_snapshot()
        now = time.monotonic()
        cooling = {repo for repo, until in dict(github_poller._cooldowns).items() if until > now}
        return (snapshot.get("pause_reason") or "") if snapshot.get("paused") else "", cooling
    except Exception:  # noqa: BLE001
        return "", set()


def github(ctx: Context) -> list[Finding]:
    """#188 issue 4: a mirror that stopped refreshing, or a repository that fails."""
    from services.cowork_agent.github_poller import poll_interval_seconds, poller_enabled

    if not poller_enabled():
        return []
    interval = poll_interval_seconds()
    pause, cooling = _github_pauses(ctx)
    spec = inventory.spec_for(inventory.STATE, "projects/x/github/issues.json")
    out: list[Finding] = []
    stale_while_paused = 0
    for project in ctx.projects():
        if project.read.outcome != "ok":
            continue
        repo = _github_repo(project.read.value)
        if repo is None:
            continue
        key = project.pid or normalize_agent_id(project.name)
        path = ctx.state_root / "projects" / key / "github" / "issues.json"
        mirror = ctx.read(path, spec)
        if mirror.outcome != "ok":
            continue  # not fetched yet, or damaged (the read check reports that)
        shown = ctx.display(path)
        fetched = _ts(mirror.value.get("fetched_at"))
        error = mirror.value.get("error")
        last_good = moment(fetched, ctx.now) if fetched else "never"
        if isinstance(error, dict) and error.get("kind"):
            kind = str(error["kind"])
            message = background.redact(str(error.get("message") or ""))
            out.append(Finding(
                "github.failing", WARN, project.name, shown,
                f"Refreshing {repo}'s issues fails: {kind}" + (f" ({message})." if message else "."), "",
                title=f"Project {project.name}'s GitHub issues can't be refreshed",
                evidence=[ev("Repository", repo), ev("Error", kind),
                          ev("First seen", _when(_ts(error.get("at")), ctx.now)), ev("Last good refresh", last_good)],
                consequence="The project's GitHub issues, and the Inbox's issue items, show what was last fetched.",
                self_repair="The poller keeps retrying, pausing after some errors.",
                next_step=_GITHUB_NEXT.get(kind, "The server log has the details."),
                problem_key=f"github:{project.name}:failing"))
            continue
        stale_threshold = max(3 * interval, GITHUB_STALE_MIN_S)
        if fetched is None or ctx.now - fetched <= stale_threshold or repo in cooling:
            continue
        if _poller_recently_started(ctx, "github poller", stale_threshold):
            continue
        if pause:
            stale_while_paused += 1
            continue
        out.append(Finding(
            "github.stale", WARN, project.name, shown,
            f"{repo}'s issues were last refreshed {ago(ctx.now - fetched)} ago; they refresh every {ago(interval)}.",
            "", title=f"Project {project.name}'s GitHub issues stopped refreshing",
            evidence=[ev("Repository", repo), ev("Last refresh", last_good), ev("Refreshed every", ago(interval))],
            consequence="The project's GitHub issues, and the Inbox's issue items, go stale.",
            self_repair="Nothing: the GitHub poller appears to have stopped.",
            next_step=RESTART, problem_key=f"github:{project.name}:stale"))
    if pause and stale_while_paused:
        kind = pause.split(":", 1)[0].strip()
        shown_pause = background.redact(pause)
        out.append(Finding(
            "github.paused", WARN, "github poller", "", f"The GitHub poller is paused: {shown_pause}.", "",
            title="GitHub polling is paused", evidence=[ev("Projects waiting", stale_while_paused)],
            consequence="GitHub issue copies aren't refreshed while it's paused.",
            self_repair="It resumes by itself when the pause ends.",
            next_step=_GITHUB_NEXT.get(kind, "Nothing to do; it resumes by itself."),
            problem_key="component:github poller:paused"))
    return out


#: A schedule slot missed by more than max(this, 5 ticks) is overdue.
SCHEDULER_GRACE_S = 300
#: A run still marked running this long past its timeout is stuck.
SCHEDULER_STUCK_GRACE_S = 300
#: The usage report runs daily; a probe older than this means it stopped.
USAGE_STALE_S = 26 * 3600
_OK_RESULTS = frozenset({"ok", "skipped"})


def scheduler(ctx: Context) -> list[Finding]:
    """Scheduled commands that didn't run, are stuck, or failed. The
    scheduler runs inside the watcher, so both must be enabled."""
    from utils.commands.scheduler import max_concurrent, scheduler_enabled

    if not watcher_enabled() or not scheduler_enabled():
        return []
    jobs = ctx.read(ctx.state_root / "scheduler" / "jobs.json",
                    inventory.spec_for(inventory.STATE, "scheduler/jobs.json"))
    state = ctx.read(ctx.state_root / "scheduler" / "state.json",
                     inventory.spec_for(inventory.STATE, "scheduler/state.json"))
    if jobs.outcome != "ok" or state.outcome != "ok":
        return []  # absent (no commands) or damaged (the read check reports that)
    job_map, state_map = jobs.value.get("jobs"), state.value.get("jobs")
    if not isinstance(job_map, dict) or not isinstance(state_map, dict):
        return []
    grace = max(float(SCHEDULER_GRACE_S), 5 * runtime_env.watcher_tick_interval_seconds())
    running_now = sum(1 for entry in state_map.values()
                      if isinstance(entry, dict) and entry.get("running_since"))
    at_capacity = running_now >= max_concurrent()  # a due job then waits by design
    shown = ctx.display(ctx.state_root / "scheduler" / "state.json")
    out: list[Finding] = []
    for job_id, job in sorted(job_map.items()):
        entry = state_map.get(job_id)
        if not isinstance(job, dict) or not isinstance(entry, dict) or not job.get("enabled", True):
            continue
        name = str(job.get("name") or job_id)
        command = job.get("command") if isinstance(job.get("command"), dict) else {}
        timeout = command.get("timeout") if isinstance(command.get("timeout"), (int, float)) else 3600
        running = _ts(entry.get("running_since"))
        if running is not None:
            if ctx.now - running > timeout + SCHEDULER_STUCK_GRACE_S:
                out.append(Finding(
                    "scheduler.stuck", WARN, job_id, shown,
                    f"It started {ago(ctx.now - running)} ago and is still marked running; its timeout is {ago(timeout)}.",
                    "", title=f"Scheduled command '{name}' is still running after {ago(ctx.now - running)}",
                    evidence=[ev("Started", moment(running, ctx.now)), ev("Timeout", ago(timeout))],
                    consequence="Its next runs are skipped while this one is still marked running.",
                    self_repair="When the server restarts, a run that was cut off is recorded as lost.",
                    next_step="Look at the command's output on the Schedules page; restart the server if its process is gone.",
                    problem_key=f"job:{job_id}:stuck"))
            continue
        due = _ts(entry.get("next_run"))
        if due is not None and ctx.now - due > grace and not at_capacity:
            out.append(Finding(
                "scheduler.overdue", WARN, job_id, shown,
                f"It was due {ago(ctx.now - due)} ago and hasn't started.", "",
                title=f"Scheduled command '{name}' didn't run on time",
                evidence=[ev("Was due", moment(due, ctx.now))],
                consequence="The command doesn't run while the scheduler isn't ticking.",
                self_repair="The scheduler runs inside the watcher; it catches up as soon as the watcher ticks again.",
                next_step="Check the watcher above; restarting the server restarts both.",
                problem_key=f"job:{job_id}:overdue"))
        result = entry.get("last_result")
        status = result.get("status") if isinstance(result, dict) else None
        if isinstance(status, str) and status not in _OK_RESULTS:
            code = result.get("returncode")
            out.append(Finding(
                "scheduler.last_failed", WARN, job_id, shown,
                f"Its last run ended with status {status}" + (f" (exit code {code})." if code is not None else "."),
                "", title=f"Scheduled command '{name}' failed its last run",
                evidence=[ev("Finished", _when(_ts(result.get("finished_at")), ctx.now)), ev("Status", status)],
                consequence="Whatever the command does didn't happen on its last run.",
                self_repair="It runs again at its next scheduled time.",
                next_step="Open the command on the Schedules page and read its output.",
                problem_key=f"job:{job_id}:last_failed"))
    return out


def _usage_token_present() -> bool:
    try:
        from routers.auth.auth import get_auth_token

        return bool(get_auth_token())
    except Exception:  # noqa: BLE001
        return False


def usage(ctx: Context) -> list[Finding]:
    """Usage reporting that stopped (daily probe gone stale) or was refused."""
    if not _usage_token_present():
        return []  # no key: nothing is reported, by design
    path = usage_state_path(ctx)
    if path is None:
        return []
    bookmark = ctx.read(path, inventory.spec_for(inventory.STATE, "usage/x.json"))
    if bookmark.outcome != "ok" or not isinstance(bookmark.value.get("key_probe"), dict):
        return []  # first sync still to run, or damaged (the read check reports that)
    probe = bookmark.value["key_probe"]
    at = _ts(probe.get("at"))
    shown = ctx.display(path)
    if probe.get("outcome") == "rejected":
        return [Finding(
            "usage.rejected", WARN, "usage sync", shown,
            f"XO refused this Space's API key on the last report (HTTP {probe.get('status')}).", "",
            title="XO refused this Space's API key", evidence=[ev("Refused", _when(at, ctx.now))],
            consequence="No usage is reported to XO.", self_repair="It tries again at the next daily run.",
            next_step="Check the XO API key in Setup.", problem_key="component:usage sync:rejected")]
    if at is not None and ctx.now - at > USAGE_STALE_S:
        return [Finding(
            "usage.stale", WARN, "usage sync", shown,
            f"The last usage report attempt was {ago(ctx.now - at)} ago; it runs daily.", "",
            title="Usage hasn't been reported for over a day", evidence=[ev("Last attempt", moment(at, ctx.now))],
            consequence="Usage stops being reported to XO.",
            self_repair="Nothing: the daily run appears to have stopped.",
            next_step="Restart the server; it catches up when it starts.",
            problem_key="component:usage sync:stale")]
    return []


def relay(ctx: Context) -> list[Finding]:
    """The sharing relay keeps its state in memory only, so it is judged only
    inside the server (when its task is in the record)."""
    if "relay poller" not in ctx.components:
        return []
    try:
        from services.cowork_agent.project_sharing import config, status

        snapshot, interval = status.snapshot(), config.poll_interval()
    except Exception:  # noqa: BLE001
        return []
    if snapshot.get("cadence") != "running":
        return []  # parked (sharing off, no Space id, not signed in): by design
    last = _ts(snapshot.get("last_poll_at"))
    if last is not None and ctx.now - last > 2 * interval * 1.2 + 60:
        return [Finding(
            "relay.overdue", WARN, "relay poller", "",
            f"The project-sharing relay last polled {ago(ctx.now - last)} ago; it polls about every {ago(interval)}.",
            "", title="The project-sharing relay stopped polling", evidence=[ev("Last poll", moment(last, ctx.now))],
            consequence=COMPONENTS["relay poller"][1],
            self_repair="Nothing: a git fetch may be hanging (they have no timeout).",
            next_step=RESTART, problem_key="component:relay poller:overdue")]
    if snapshot.get("last_poll_ok") is False:
        return [Finding(
            "relay.unreachable", WARN, "relay poller", "", "The project-sharing relay can't reach XO.", "",
            title="The project-sharing relay can't reach XO",
            evidence=[ev("Last attempt", _when(last, ctx.now))],
            consequence="Shared projects don't exchange commits until XO is reachable again.",
            self_repair="It keeps retrying every poll.", next_step="Check this machine's network connection.",
            problem_key="component:relay poller:unreachable")]
    return []
