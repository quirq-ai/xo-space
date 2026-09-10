"""The GitHub issue poller — a standalone loop, deliberately not a watcher sink."""

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
from services.cowork_agent.connectors.github.issues import (
    IssuesResult,
    RateLimit,
    RepoRef,
    fetch_open_issues,
    gh_available,
    parse_remote_url,
)
from services.cowork_agent.visualizer import github_interest
from services.cowork_agent.visualizer import github_mirror
from services.cowork_agent.visualizer import state as watcher_state
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workitems_store import list_workitems
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

#: The hard off switch.
ENV_ENABLED = "XO_GITHUB_POLL_ENABLED"
ENV_INTERVAL = "XO_GITHUB_POLL_INTERVAL_S"
ENV_MAX_PAGES = "XO_GITHUB_POLL_MAX_PAGES"
ENV_WARN_REPOS = "XO_GITHUB_POLL_WARN_REPOS"
ENV_INTEREST_TTL = "XO_GITHUB_POLL_INTEREST_TTL_S"

DEFAULT_INTERVAL_S = 60.0

#: Pages per repo per poll.
DEFAULT_MAX_PAGES = 10

#: §6.3: "W5 logs a warning when the polled-repo count crosses 80."
DEFAULT_WARN_REPOS = 80

#: GitHub's GraphQL budget, and the fraction of it we are willing to imply
#: before saying so out loud.
GRAPHQL_HOURLY_BUDGET = 5000
WARN_BUDGET_FRACTION = 0.8

#: Stop spending when GitHub says this little is left.
BUDGET_RESERVE_POINTS = 250

#: How long a project stays "being looked at" after :func:`note_interest`.
DEFAULT_INTEREST_TTL_S = 300.0

#: Per-repo cool-down after a failure that a retry in 60 s cannot fix.
_COOLDOWN_S: dict[str, float] = {
    "not_found": 900.0,
    "forbidden": 900.0,
    "bad_remote": 3600.0,
    "bad_response": 300.0,
    "unknown": 300.0,
}

#: A failure that is true of the whole machine, not of one repo.
_GLOBAL_KINDS = frozenset({"no_cli", "not_authenticated"})
_GLOBAL_PAUSE_S = 600.0

#: Ceiling on how long a rate-limit backoff may sleep, so a bad ``resetAt``
#: cannot wedge the poller for a day.
_MAX_PAUSE_S = 3600.0

#: Warnings are throttled: a condition that holds every minute must not produce
#: a log line every minute.
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
# D9's other half.

#: Ceiling on how many projects one sweep will consider interesting.
_INTEREST_MAX = 512


def note_interest(project_id: str) -> None:
    """Mark a project as being looked at, for :func:`interest_ttl_seconds`."""
    name = (project_id or "").strip()
    if not name:
        return
    if interest_ttl_seconds() <= 0:
        return
    github_interest.note_interest(name)


def is_interested(project_id: str) -> bool:
    """Whether ``project_id`` carries an unexpired interest mark."""
    return github_interest.is_interesting(
        project_id, ttl=interest_ttl_seconds()
    )


def interested_projects() -> set[str]:
    """Every project currently marked as being looked at."""
    out: set[str] = set()
    for name in list_project_ids():
        if len(out) >= _INTEREST_MAX:
            break
        if is_interested(name):
            out.add(name)
    return out


def clear_interest(project_id: Optional[str] = None) -> None:
    """Forget interest marks. For tests and for a root switch."""
    if project_id is not None:
        github_interest.clear_interest(project_id)
        return
    for name in list_project_ids():
        github_interest.clear_interest(name)


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
    """One global GraphQL budget, in points."""

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
        """
        Back off honouring GitHub's own ``resetAt`` — no extra call needed, the
        poll's own response carried it.
        """
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


#: Module-level, because §6.3 requires **one** global budget rather than per-
#: project timers — a per-project view cannot see the ceiling it is
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
    _inflight.clear()
    # Interest marks are durable now (I2), so they live on disk under the
    # runtime root rather than in this module.


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
    """``project.json:git.remote_url`` → a github.com repo, or ``None``."""
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
    """Whether the project holds workitems adopted from GitHub (D2)."""
    path = project_layout.xo_dir(project) / "workitems.json"
    try:
        return bool(list_workitems(path, kind="github"))
    except Exception:
        return False


def _has_live_session(project: str) -> bool:
    """Whether an agent is currently working in the project."""
    doc = read_json(watcher_state.project_activity_path(project))
    if not isinstance(doc, dict):
        return False
    sessions = doc.get("open_sessions")
    return isinstance(sessions, list) and bool(sessions)


def candidates() -> list[Candidate]:
    """Every project worth polling this tick, and why (D9)."""
    out: list[Candidate] = []
    for project in list_project_ids():
        ref = _remote_ref(project)
        if ref is None:
            continue
        # Asked per project rather than against a prebuilt set: the marks are
        # one small file each in the runtime tier, so this is one read for the
        # project already in hand, where building the set would read every
        # project's file to answer a single lookup.
        if is_interested(project):
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


async def poll_project(
    candidate: Candidate, *, max_pages_override: Optional[int] = None
) -> int:
    """Refresh one project's mirror. Returns the points this poll spent."""
    # ``max_pages_override`` is how a request path asks for one page: a browse
    # view shows the first page anyway, and a one-page result reports
    # ``complete=False``, which ``record_pages`` already handles by merging
    # rather than replacing and by declining to advance the high-water mark.
    cap = max_pages() if max_pages_override is None else max(1, int(max_pages_override))
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
        # A failed poll still spent a point whenever GitHub answered at all, so
        # the budget is charged from the response, not from success.
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
        if not complete and failure is None and max_pages_override is None:
            # Suppressed for a deliberate one-page poll: not fitting is the
            # expected outcome there, not a misconfiguration worth a warning.
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
    """Publish a machine-wide failure into every candidate's mirror."""
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


# ── One poll, on demand (issuesplan Phase 1 + 2) ─────────────────────────────


@dataclass(frozen=True)
class PollOutcome:
    """What :func:`poll_project_now` did. Never an exception."""

    #: ``True`` iff a poll actually ran (it may still have failed against
    #: GitHub — that is recorded in the mirror, not here).
    polled: bool
    #: Why it did not, when it did not: ``disabled``, ``no_remote``,
    #: ``paused``, ``no_cli``, ``cooldown``, ``in_flight``, ``timeout`` or
    #: ``failed``.
    reason: Optional[str] = None
    #: GraphQL points spent. Zero for every skip.
    points: int = 0


#: One lock per project, so N concurrent cold requests cost one poll rather
#: than N.
_inflight: dict[str, asyncio.Lock] = {}


def _project_lock(project: str) -> asyncio.Lock:
    lock = _inflight.get(project)
    if lock is None:
        lock = asyncio.Lock()
        _inflight[project] = lock
    return lock


async def poll_project_now(
    project_id: str,
    *,
    max_pages_override: Optional[int] = None,
    timeout: Optional[float] = None,
) -> PollOutcome:
    """Poll one project **now**, applying every guard the loop applies."""
    name = (project_id or "").strip()
    if not name:
        return PollOutcome(False, "no_remote")
    if not poller_enabled():
        return PollOutcome(False, "disabled")
    if _budget.paused:
        return PollOutcome(False, "paused")

    ref = _remote_ref(name)
    if ref is None:
        return PollOutcome(False, "no_remote")
    if _cooldown(ref.slug):
        return PollOutcome(False, "cooldown")
    if not gh_available():
        return PollOutcome(False, "no_cli")

    lock = _project_lock(name)
    if lock.locked():
        # Someone is already polling this project.
        return PollOutcome(False, "in_flight")

    async with lock:
        # Re-checked inside the lock: a poll that completed while we were
        # acquiring it may have paused the budget or started a cool-down.
        if _budget.paused:
            return PollOutcome(False, "paused")
        if _cooldown(ref.slug):
            return PollOutcome(False, "cooldown")
        candidate = Candidate(project=name, ref=ref, reason="request")
        try:
            if timeout is not None:
                spent = await asyncio.wait_for(
                    poll_project(candidate, max_pages_override=max_pages_override),
                    timeout=timeout,
                )
            else:
                spent = await poll_project(
                    candidate, max_pages_override=max_pages_override
                )
        except asyncio.TimeoutError:
            # The gh call is still running and will finish (or be reaped) on
            # its own; the mirror is either updated by it or left as it was.
            logger.info(
                "github: on-demand poll of %s exceeded %.1fs; serving the "
                "mirror as it stands", name, timeout or 0.0,
            )
            return PollOutcome(False, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "github: on-demand poll of %s failed unexpectedly",
                name, exc_info=True,
            )
            return PollOutcome(False, "failed")
        return PollOutcome(True, None, spent)


async def poll_once() -> dict:
    """One tick: choose the repos, poll them, return a summary. Never raises."""
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
        # absent, local workitems fully functional.
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
    """Say the ceiling is near *before* it is hit (§11)."""
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

#: Boot delay.
_STARTUP_DELAY_S = 20.0


async def start_github_poller() -> None:
    """Entry point for the background task (mirrors ``usage_sync``)."""
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
