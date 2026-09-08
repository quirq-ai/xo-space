"""The GitHub issue poller — a standalone loop, deliberately not a watcher sink.

``docs/workitems-plan.md`` §6. Once a minute this refreshes the runtime mirror
(:mod:`services.cowork_agent.visualizer.github_mirror`) for the projects that
actually need it, using the pinned ``gh api graphql`` client in
:mod:`services.cowork_agent.connectors.github_issues`.

**It is not a watcher sink, and it must never become one.** §2's rule is
absolute and ``visualizer/git_provenance.py`` states it in its own words:
``git ls-remote`` would answer the default-branch question authoritatively
"but it hits the network, which a watcher tick must never do". A tick that can
block on DNS stops being a tick. So the precedent this file follows is
:mod:`services.usage_sync` — its own ``asyncio`` task, its own interval, its
own failure isolation, started from the FastAPI lifespan and cancelled with it.
``tests/test_github_poller.py`` asserts the separation rather than trusting it.

Four properties are load-bearing.

**1. The budget is counted in points, not repos** (§6.2, amendment 7). The
GraphQL budget is 5,000 points/hour and is *separate* from the REST 5,000/hour
that ``git``, the sync module and MCP tooling share — measured, and the reason
D5 chose ``gh``. One page of ≤100 issues costs one point, so the familiar
"~83 repos at 60 s" figure is really "5,000 points/hour", and a 250-issue repo
spends three of them per full poll. :class:`_Budget` therefore accounts in
points, globally — one budget, not per-project timers — and the warning
threshold is on projected point consumption as well as on the repo count §6.3
names.

**2. Polling is lazy** (D9). A repo nobody has open does not need 60-second
freshness, and skipping it is what keeps the ceiling off the critical path.
A project is polled only if its remote is a github.com repo *and* it holds
adopted workitems, has a live agent session, or has been marked interesting
through :func:`note_interest`. That last one is the hook for "being looked
at": there is **no viewing signal in this system today** — nothing records
which project a user has open — so the read routes (W7) are expected to call
it, and until they do the first two conditions carry the feature.

**3. Every failure is survivable and none of them stops the loop.** No ``gh``,
no auth, no network, a deleted repo, a spent budget: each is a *state*, the
client reports which one in ``error_kind``, and the mirror keeps its last good
rows either way. The one thing the poller will not do is keep hammering — a
rate limit pauses everything until GitHub's own ``resetAt``, a missing binary
or a rejected credential pauses everything for a cool-down, and a repo that is
gone or forbidden is not retried every minute.

**4. Nothing it writes lands in ``.xo/``.** R-TIER. The only writer is
:mod:`~services.cowork_agent.visualizer.github_mirror`, which resolves its path
through ``project_layout``'s runtime helpers. This module reads
``project.json`` and ``workitems.json`` out of the synced tier — reads are
free, and the second is how "has adopted items" is answered — and writes
neither. ``tests/test_github_poller.py`` snapshots ``XO_PROJECTS_ROOT`` across
a full poll and asserts not one byte moved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from services.cowork_agent import project_layout
from services.cowork_agent.connectors.github_issues import (
    IssuesResult,
    RateLimit,
    RepoRef,
    fetch_open_issues,
    gh_available,
    parse_remote_url,
)
from services.cowork_agent.visualizer import github_mirror
from services.cowork_agent.visualizer import state as watcher_state
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workitems_store import list_workitems
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

#: The hard off switch. §11 lists it beside the rate budget and the backoff as
#: an acceptance criterion rather than polish: this is the first thing in the
#: system that makes outbound network calls on a timer, and an operator who
#: wants it stopped must not have to uninstall anything.
ENV_ENABLED = "XO_GITHUB_POLL_ENABLED"
ENV_INTERVAL = "XO_GITHUB_POLL_INTERVAL_S"
ENV_MAX_PAGES = "XO_GITHUB_POLL_MAX_PAGES"
ENV_WARN_REPOS = "XO_GITHUB_POLL_WARN_REPOS"
ENV_INTEREST_TTL = "XO_GITHUB_POLL_INTEREST_TTL_S"

DEFAULT_INTERVAL_S = 60.0

#: Pages per repo per poll. Ten pages is 1,000 issues; past that a *seed*
#: cannot complete, the mirror stays unseeded and retries next tick, and the
#: warning below names the repo. The cap exists so one enormous repository
#: cannot spend the whole global budget in a single tick.
DEFAULT_MAX_PAGES = 10

#: §6.3: "W5 logs a warning when the polled-repo count crosses 80."
DEFAULT_WARN_REPOS = 80

#: GitHub's GraphQL budget, and the fraction of it we are willing to imply
#: before saying so out loud. The ceiling is real and silent until it is not
#: (§11), so the poller says it is approaching rather than waiting to fail.
GRAPHQL_HOURLY_BUDGET = 5000
WARN_BUDGET_FRACTION = 0.8

#: Stop spending when GitHub says this little is left. The reserve is not for
#: us — it leaves room for the interactive GraphQL calls adoption (W7) and
#: assignment (W8) will make on the same budget, which a poll must never
#: starve.
BUDGET_RESERVE_POINTS = 250

#: How long a project stays "being looked at" after :func:`note_interest`.
DEFAULT_INTEREST_TTL_S = 300.0

#: Per-repo cool-down after a failure that a retry in 60 s cannot fix.
#: ``not_found`` (deleted, renamed, transferred), ``forbidden`` (this token
#: cannot see it) and ``bad_remote`` are all stable states; ``network`` and
#: ``timeout`` are not, and get no cool-down at all.
_COOLDOWN_S: dict[str, float] = {
    "not_found": 900.0,
    "forbidden": 900.0,
    "bad_remote": 3600.0,
    "bad_response": 300.0,
    "unknown": 300.0,
}

#: A failure that is true of the whole machine, not of one repo. Every
#: candidate would fail identically, so the poller records it once for each of
#: them (cheap — no network, and an unchanged error writes nothing) and then
#: stops trying for a while.
_GLOBAL_KINDS = frozenset({"no_cli", "not_authenticated"})
_GLOBAL_PAUSE_S = 600.0

#: Ceiling on how long a rate-limit backoff may sleep, so a bad ``resetAt``
#: cannot wedge the poller for a day.
_MAX_PAUSE_S = 3600.0

#: Warnings are throttled: a condition that holds every minute must not
#: produce a log line every minute.
_WARN_INTERVAL_S = 900.0


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _number(name: str, default: float, *, minimum: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def poller_enabled() -> bool:
    return _flag(ENV_ENABLED, True)


def poll_interval_seconds() -> float:
    return _number(ENV_INTERVAL, DEFAULT_INTERVAL_S, minimum=5.0)


def max_pages() -> int:
    return int(_number(ENV_MAX_PAGES, DEFAULT_MAX_PAGES, minimum=1))


def warn_repo_threshold() -> int:
    return int(_number(ENV_WARN_REPOS, DEFAULT_WARN_REPOS, minimum=1))


def interest_ttl_seconds() -> float:
    return _number(ENV_INTEREST_TTL, DEFAULT_INTEREST_TTL_S, minimum=0.0)


# ── "Being looked at" ────────────────────────────────────────────────────────
#
# D9's other half. There is no viewing signal in this codebase — nothing
# records which project a user has open — so this is the seam for one, kept
# deliberately small: an in-process map of project id to expiry. It is
# machine-local and disposable by construction (a restart forgets it), which
# is the correct tier for "what is on someone's screen right now".

_interest: dict[str, float] = {}
_INTEREST_MAX = 512


def note_interest(project_id: str) -> None:
    """Mark a project as being looked at, for :func:`interest_ttl_seconds`.

    The hook a read route calls when a user opens a project, so its issues
    are fresh while they are looking and not otherwise. Cheap, idempotent and
    safe to call from a request thread: it touches no file and never raises.
    """
    name = (project_id or "").strip()
    if not name:
        return
    ttl = interest_ttl_seconds()
    if ttl <= 0:
        return
    if len(_interest) >= _INTEREST_MAX:
        _expire_interest()
        if len(_interest) >= _INTEREST_MAX:
            _interest.clear()
    _interest[name] = time.monotonic() + ttl


def _expire_interest() -> None:
    now = time.monotonic()
    for name in [key for key, expiry in _interest.items() if expiry <= now]:
        _interest.pop(name, None)


def interested_projects() -> set[str]:
    """Project ids currently marked as being looked at."""
    _expire_interest()
    return set(_interest)


def clear_interest() -> None:
    """Forget every interest mark. For tests and for a root switch."""
    _interest.clear()


# ── The global budget ────────────────────────────────────────────────────────


def _parse_reset(reset_at: Optional[str]) -> Optional[float]:
    """``resetAt`` → seconds from now, or ``None`` if it cannot be read."""
    if not isinstance(reset_at, str) or not reset_at.strip():
        return None
    text = reset_at.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return None
    return min(delta, _MAX_PAUSE_S)


@dataclass
class _Budget:
    """One global GraphQL budget, in points.

    Two sources, and the order between them matters. GitHub's own
    ``rateLimit.remaining`` — read from *inside* the query, because
    ``gh api rate_limit`` was measured reporting a full budget regardless of
    consumption — is authoritative whenever it is fresh. Local accounting over
    a rolling hour is the fallback for everything before the first answer and
    for the window after a failure that never reached GitHub.

    Neither is trusted to invent a number: an unknown budget spends, because
    refusing to poll on no evidence would disable the feature the first time a
    response came back short.
    """

    #: (monotonic stamp, points) for the last hour, for local accounting.
    _spend: deque = field(default_factory=deque)
    remaining: Optional[int] = None
    limit: Optional[int] = None
    reset_at: Optional[str] = None
    _observed_at: float = 0.0
    _paused_until: float = 0.0
    _pause_reason: str = ""

    def observe(self, rate: RateLimit) -> None:
        if rate.cost is not None:
            self.charge(rate.cost)
        if rate.known:
            self.remaining = rate.remaining
            self.reset_at = rate.reset_at
            self._observed_at = time.monotonic()
        if rate.limit is not None:
            self.limit = rate.limit

    def charge(self, points: int) -> None:
        if points <= 0:
            return
        now = time.monotonic()
        self._spend.append((now, points))
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._spend and self._spend[0][0] < cutoff:
            self._spend.popleft()

    @property
    def spent_last_hour(self) -> int:
        self._trim(time.monotonic())
        return sum(points for _, points in self._spend)

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    @property
    def pause_reason(self) -> str:
        return self._pause_reason if self.paused else ""

    def pause(self, seconds: float, reason: str) -> None:
        seconds = max(0.0, min(float(seconds), _MAX_PAUSE_S))
        until = time.monotonic() + seconds
        if until > self._paused_until:
            self._paused_until = until
            self._pause_reason = reason
            logger.warning(
                "github poller: pausing for %.0fs (%s)", seconds, reason
            )

    def pause_until_reset(self, rate: RateLimit, reason: str) -> None:
        """Back off honouring GitHub's own ``resetAt`` — no extra call needed,
        the poll's own response carried it."""
        seconds = _parse_reset(rate.reset_at or self.reset_at)
        self.pause(seconds if seconds is not None else 300.0, reason)

    def can_spend(self) -> bool:
        """Whether another page may be fetched right now."""
        if self.paused:
            return False
        if self.remaining is not None and self.remaining <= BUDGET_RESERVE_POINTS:
            return False
        if self.spent_last_hour >= GRAPHQL_HOURLY_BUDGET - BUDGET_RESERVE_POINTS:
            return False
        return True


#: Module-level, because §6.3 requires **one** global budget rather than
#: per-project timers — a per-project view cannot see the ceiling it is
#: collectively approaching.
_budget = _Budget()

#: repo slug → monotonic time before which it is not worth polling again.
_cooldowns: dict[str, float] = {}

#: warning key → monotonic time of its last emission.
_warned: dict[str, float] = {}

#: A machine-wide failure seen during this tick, published into every
#: candidate's mirror once the tick is over rather than during it.
_pending_global_failure: Optional[IssuesResult] = None


def reset_state() -> None:
    """Drop every in-process poller cache. For tests."""
    global _budget, _pending_global_failure
    _budget = _Budget()
    _pending_global_failure = None
    _cooldowns.clear()
    _warned.clear()
    _interest.clear()


def budget_snapshot() -> dict:
    """The budget as it stands, for a status surface or a test."""
    return {
        "spent_last_hour": _budget.spent_last_hour,
        "remaining": _budget.remaining,
        "limit": _budget.limit,
        "reset_at": _budget.reset_at,
        "paused": _budget.paused,
        "pause_reason": _budget.pause_reason,
    }


def _warn_once(key: str, message: str, *args: object) -> None:
    now = time.monotonic()
    last = _warned.get(key)
    if last is not None and (now - last) < _WARN_INTERVAL_S:
        return
    _warned[key] = now
    logger.warning(message, *args)


# ── Which projects to poll (D9) ──────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One project worth a point of budget, and why."""

    project: str
    ref: RepoRef
    reason: str

    @property
    def repo(self) -> str:
        return self.ref.slug


def _remote_ref(project: str) -> Optional[RepoRef]:
    """``project.json:git.remote_url`` → a github.com repo, or ``None``.

    The durable copy in ``.xo/project.json``, written by the git refresher
    (``sinks/project_json.refresh_git``) and newly trustworthy since O-K wired
    it and O-B stopped SSH remotes being mangled. Read, never written — and
    read from the file rather than by shelling out to ``git``, because this
    runs once a minute across every project and ``git_provenance`` costs two
    subprocesses per repository.

    A non-github.com remote returns ``None`` **without spawning ``gh``**: the
    budget is global, and a project whose origin is GitLab must not cost a
    point every minute to rediscover that it is not a GitHub repo.
    """
    meta = project_layout.load_project(project)
    if not isinstance(meta, dict):
        return None
    git = meta.get("git")
    if not isinstance(git, dict):
        return None
    ref = parse_remote_url(git.get("remote_url"))
    if ref is None or not ref.is_github_com:
        return None
    return ref


def _has_adopted_items(project: str) -> bool:
    """Whether the project holds workitems adopted from GitHub (D2).

    A read of the synced tier, which is allowed; the poller writes nothing
    there. A corrupt or unreadable ``workitems.json`` is not this loop's
    problem to solve — the store raises so the CRUD routes can refuse
    (O-E) — so it is caught here and read as "no adopted items", which
    costs the project its polling and nothing else.
    """
    path = project_layout.xo_dir(project) / "workitems.json"
    try:
        return bool(list_workitems(path, kind="github"))
    except Exception:
        return False


def _has_live_session(project: str) -> bool:
    """Whether an agent is currently working in the project.

    ``open_sessions`` in the per-project ``activity.json`` is rebuilt each
    watcher tick from the active source's ``poll_presence()``, so a session
    that stops being present simply stops appearing — observed, not declared
    (§5.4, and T22's heartbeat before it). It is the closest thing this system
    has to "someone is here", and it costs one JSON read.
    """
    doc = read_json(watcher_state.project_activity_path(project))
    if not isinstance(doc, dict):
        return False
    sessions = doc.get("open_sessions")
    return isinstance(sessions, list) and bool(sessions)


def candidates() -> list[Candidate]:
    """Every project worth polling this tick, and why (D9).

    Order is stable — the project list is sorted — so a budget that runs out
    mid-tick starves the same tail every time rather than a random one. That
    is deliberate: a deterministic shortfall is diagnosable and the warning
    below names it.
    """
    interested = interested_projects()
    out: list[Candidate] = []
    for project in list_project_ids():
        ref = _remote_ref(project)
        if ref is None:
            continue
        if project in interested:
            reason = "interest"
        elif _has_adopted_items(project):
            reason = "adopted"
        elif _has_live_session(project):
            reason = "active"
        else:
            continue
        out.append(Candidate(project=project, ref=ref, reason=reason))
    return out


# ── One poll ─────────────────────────────────────────────────────────────────


def _cooldown(repo: str) -> bool:
    until = _cooldowns.get(repo)
    if until is None:
        return False
    if time.monotonic() >= until:
        _cooldowns.pop(repo, None)
        return False
    return True


def _apply_cooldown(repo: str, kind: Optional[str]) -> None:
    seconds = _COOLDOWN_S.get(kind or "")
    if seconds:
        _cooldowns[repo] = time.monotonic() + seconds


async def poll_project(candidate: Candidate) -> int:
    """Refresh one project's mirror. Returns the points this poll spent.

    The two-query rule of §6.3, applied here and nowhere else:

    * **no stored high-water mark** — seed: no ``since``, ``states: [OPEN]``,
      because seeding on ``[OPEN, CLOSED]`` would drag the repository's whole
      closed history through the page budget;
    * **a stored mark** — steady state: ``since`` set and
      ``include_closed=True``, because an issue closed since the mark stops
      matching ``[OPEN]`` and an incremental merge would then leave a stale
      ``open`` row in the mirror forever.

    Pagination stops at the page cap or when the budget says stop, and the
    mirror is told whether the poll *completed*, because the high-water mark
    may only advance when it did.

    Never raises. Every failure the client can report is a state, and the
    mirror keeps its last good rows through all of them.
    """
    cap = max_pages()
    state = github_mirror.load_state(candidate.project, repo=candidate.repo)
    since = state.since
    include_closed = since is not None

    pages: list[IssuesResult] = []
    after: Optional[str] = None
    complete = False
    spent = 0
    failure: Optional[IssuesResult] = None

    for _ in range(cap):
        if not _budget.can_spend():
            _warn_once(
                "budget",
                "github poller: GraphQL budget exhausted (%s spent in the last "
                "hour, GitHub reports %s remaining); %s will finish on a later "
                "tick",
                _budget.spent_last_hour, _budget.remaining, candidate.repo,
            )
            break
        result = await fetch_open_issues(
            candidate.ref,
            since=since,
            after=after,
            include_closed=include_closed,
        )
        # A failed poll still spent a point whenever GitHub answered at all,
        # so the budget is charged from the response, not from success.
        _budget.observe(result.rate)
        spent += result.rate.cost if result.rate.cost is not None else 1
        if not result.ok:
            failure = result
            break
        pages.append(result)
        if not result.has_next_page or not result.end_cursor:
            complete = True
            break
        after = result.end_cursor

    # Neither branch fires when the budget stopped us before the first page:
    # nothing was fetched and nothing failed, so the mirror is left exactly as
    # it was — which is the correct answer, not a missing one.
    if pages:
        github_mirror.record_pages(
            candidate.project, repo=candidate.repo, pages=pages, complete=complete
        )
        if not complete and failure is None:
            _warn_once(
                f"pages:{candidate.repo}",
                "github poller: %s did not fit in %d page(s); the high-water "
                "mark is held back so nothing is stranded, but it will not "
                "settle into an incremental poll until it does",
                candidate.repo, cap,
            )
    if failure is not None:
        # Ordered after ``record_pages`` deliberately: the rows that *did*
        # arrive are real and are kept, and the error is then stamped over the
        # top so the UI shows both "refreshed at" and "but the last try
        # failed".
        github_mirror.record_failure(
            candidate.project, repo=candidate.repo, result=failure
        )
        _handle_failure(candidate, failure)
    return spent


def _handle_failure(candidate: Candidate, result: IssuesResult) -> None:
    """Turn one failed poll into a backoff decision. Never raises."""
    global _pending_global_failure
    kind = result.error_kind or "unknown"
    if kind == "rate_limited":
        _budget.pause_until_reset(result.rate, f"rate limited on {candidate.repo}")
        return
    if kind in _GLOBAL_KINDS:
        _budget.pause(_GLOBAL_PAUSE_S, f"{kind}: {result.error or kind}")
        _pending_global_failure = result
        return
    _apply_cooldown(candidate.repo, kind)
    _warn_once(
        f"repo:{candidate.repo}:{kind}",
        "github poller: %s failed (%s): %s",
        candidate.repo, kind, result.error or "",
    )


def _record_global_failure(rows: Sequence[Candidate], result: IssuesResult) -> None:
    """Publish a machine-wide failure into every candidate's mirror.

    ``not_authenticated`` is true of the machine, not of one repository, so
    every project's UI needs the same "connect GitHub" affordance — and none
    of them can learn it from a poll that never happens. No network is
    involved, and an unchanged error writes nothing (``error.at`` is the
    mirror's one volatile path), so the cost is one comparison per project.
    """
    for candidate in rows:
        try:
            github_mirror.record_failure(
                candidate.project, repo=candidate.repo, result=result
            )
        except Exception:  # pragma: no cover - a mirror write must not stop the loop
            logger.warning(
                "github poller: could not record %s for %s",
                result.error_kind, candidate.project, exc_info=True,
            )


async def poll_once() -> dict:
    """One tick: choose the repos, poll them, return a summary. Never raises.

    The summary is what the loop logs and what the tests assert on:
    ``{"candidates", "polled", "skipped", "points", "paused"}``.
    """
    summary = {"candidates": 0, "polled": 0, "skipped": 0, "points": 0, "paused": False}

    if _budget.paused:
        summary["paused"] = True
        return summary

    try:
        rows = candidates()
    except Exception:
        logger.warning("github poller: could not enumerate projects", exc_info=True)
        return summary
    summary["candidates"] = len(rows)
    if not rows:
        return summary

    if not gh_available():
        # §6.3's degradation, exactly as written: no gh → no poller, mirror
        # absent, local workitems fully functional. Nothing is written,
        # because writing a ``no_cli`` error into every project's runtime tree
        # once a minute would be churn in service of a fact the UI can
        # establish for itself in one ``which``.
        _warn_once(
            "no_cli",
            "github poller: `gh` is not installed; GitHub issue polling is "
            "off for %d project(s) until it is",
            len(rows),
        )
        summary["skipped"] = len(rows)
        return summary

    threshold = warn_repo_threshold()
    if len(rows) > threshold:
        _warn_once(
            "ceiling",
            "github poller: %d repositories are being polled, above the %d "
            "the %.0fs interval was sized for (§6.2). Raise "
            "XO_GITHUB_POLL_INTERVAL_S or expect polls to start failing "
            "rather than slowing",
            len(rows), threshold, poll_interval_seconds(),
        )

    for candidate in rows:
        if _budget.paused:
            summary["paused"] = True
            summary["skipped"] += 1
            continue
        if _cooldown(candidate.repo):
            summary["skipped"] += 1
            continue
        try:
            spent = await poll_project(candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The client promises never to raise; the mirror can still fail on
            # a full disk. One project must not cost the others their tick.
            logger.warning(
                "github poller: %s failed unexpectedly",
                candidate.project, exc_info=True,
            )
            summary["skipped"] += 1
            continue
        summary["polled"] += 1
        summary["points"] += spent

    global _pending_global_failure
    if _pending_global_failure is not None:
        _record_global_failure(rows, _pending_global_failure)
        _pending_global_failure = None

    _warn_if_budget_is_close(summary["points"])
    return summary


def _warn_if_budget_is_close(points_this_tick: int) -> None:
    """Say the ceiling is near *before* it is hit (§11).

    Projection, not history: the hourly figure a tick implies is what tells
    you the interval is wrong, and waiting for a real hour of data would mean
    the first warning arrives an hour after the problem. Both numbers are
    reported so the projection can be sanity-checked against the measurement.
    """
    if points_this_tick <= 0:
        return
    interval = poll_interval_seconds()
    projected = points_this_tick * (3600.0 / interval)
    if projected < GRAPHQL_HOURLY_BUDGET * WARN_BUDGET_FRACTION:
        return
    _warn_once(
        "projection",
        "github poller: this tick spent %d point(s), which at a %.0fs "
        "interval projects to %.0f/hour against a %d/hour GraphQL budget "
        "(%d spent in the last hour). Raise XO_GITHUB_POLL_INTERVAL_S",
        points_this_tick, interval, projected, GRAPHQL_HOURLY_BUDGET,
        _budget.spent_last_hour,
    )


# ── The loop ─────────────────────────────────────────────────────────────────

#: Boot delay. The first tick waits for the watcher to have written at least
#: one ``activity.json`` and for ``fill_identity`` to have minted pids, so the
#: first poll resolves runtime homes by pid rather than by folder name.
_STARTUP_DELAY_S = 20.0


async def start_github_poller() -> None:
    """Entry point for the background task (mirrors ``usage_sync``).

    Returns immediately when the poller is switched off, so a disabled poller
    holds no task and costs nothing. Otherwise it loops forever: one tick,
    then sleep the interval, with every failure caught — a poll that dies must
    not take the server's event loop with it, and the next tick is always
    another chance.

    Cancellation is normal shutdown (the lifespan cancels this task), so
    ``CancelledError`` propagates rather than being swallowed.
    """
    if not poller_enabled():
        logger.info("github poller: disabled by %s", ENV_ENABLED)
        return

    await asyncio.sleep(_STARTUP_DELAY_S)
    interval = poll_interval_seconds()
    logger.info(
        "github poller: started (interval %.0fs, %d page(s) per repo, "
        "GraphQL budget %d/hour)",
        interval, max_pages(), GRAPHQL_HOURLY_BUDGET,
    )

    while True:
        try:
            summary = await poll_once()
            if summary["polled"] or summary["skipped"]:
                logger.debug("github poller: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("github poller: tick failed (non-fatal)", exc_info=True)
        await asyncio.sleep(poll_interval_seconds())
