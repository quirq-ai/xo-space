"""Admission: which ready session goes next, and whether its pool lets a new
turn start (ch 07 §5). No caps: these gates follow real limits, and fairness
only orders.

* **The pool** (one per agent's account): ``open`` admits everything;
  ``warn`` (a warning, or a window ≥ 80 % used) admits ``now`` and ``next``
  and holds ``later``; ``closed`` (a window rejected until its reset) and
  ``paid`` (the account moved onto paid extra usage, until the person
  chooses) admit no new turn. Running turns always finish.
* **Its windows:** an account has several usage windows (a 5-hour and a
  7-day one, say), and a runtime reports them one at a time. The pool keeps each window's own state and stands at the
  worst of them (``standing``), so a report on one window never undoes
  another's. Within one window, a report milder than the last one, for the
  same reset, is taken only ``SETTLE_S`` after it: the processes that share
  an account report what their own calls saw, and a call made just before
  the limit can report after one that hit it (``report``).
* **Fairness:** priority class first (a ``later`` wake older than 10 minutes
  counts as ``next``), then round-robin across projects, then oldest first.
* **Pacing** (in the engine): new processes start about two a second, at most
  four starting at once (boot re-kicks go four at a time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal, Mapping

from services.cowork_agent.adapters.contract import RateLimit
from services.cowork_agent.storm.engine.model import PRIORITY_RANK
from services.timestamps import TS_FORMAT, parse_ts

PoolState = Literal["open", "warn", "closed", "paid"]

WARN_UTILIZATION = 0.8
AGING_S = 600.0
SPAWN_INTERVAL_S = 0.5
MAX_STARTING = 4
#: Below every priority class: the start of a "most urgent so far" search.
LOWEST_RANK = max(PRIORITY_RANK.values()) + 1
#: How long a window's report stands against a milder one for the same reset
#: (one from a call already on its way when the limit hit).
SETTLE_S = 15.0
#: The window of a report that names none (a send the runtime refused on a limit).
UNNAMED = "unnamed"
#: Pool states from mildest to worst: the pool stands at its worst window's.
SEVERITY: Mapping[str, int] = {"open": 0, "warn": 1, "paid": 2, "closed": 3}


@dataclass(frozen=True)
class Window:
    """One usage window's state as last reported: ``since`` is when it took
    this state for this reset."""

    state: PoolState
    resets_at: str | None
    since: str
    utilization: float | None = None

    def as_dict(self) -> dict:
        return {"state": self.state, "resets_at": self.resets_at, "since": self.since,
                "utilization": self.utilization}

    @classmethod
    def of(cls, data: Mapping) -> "Window | None":
        state = data.get("state")
        if state not in SEVERITY or not isinstance(data.get("since"), str):
            return None
        utilization = data.get("utilization")
        return cls(state, data.get("resets_at"), data["since"],
                   float(utilization) if isinstance(utilization, (int, float)) else None)


@dataclass
class Pool:
    id: str
    state: PoolState = "open"
    resets_at: str | None = None
    paid_continue_until: str | None = None
    overloaded: bool = False            # warn because the service was overloaded, not a usage window
    windows: Mapping[str, Window] = field(default_factory=dict)

    def admits(self, rank: int) -> bool:
        """May a new turn whose most urgent wake has ``rank`` start?"""
        if self.state == "open":
            return True
        return self.state == "warn" and rank <= PRIORITY_RANK["next"]


def pool_state(event: RateLimit, pool: Pool, now: str) -> tuple[PoolState, str | None]:
    """The state one ``RateLimit`` event reports for its window, and its
    reset time."""
    if event.overage and not (pool.paid_continue_until and pool.paid_continue_until > now):
        return "paid", event.resets_at
    if event.status == "limited":
        return "closed", event.resets_at
    if event.status == "warning" or (event.utilization is not None and event.utilization >= WARN_UTILIZATION):
        return "warn", event.resets_at
    return "open", None


def report(windows: Mapping[str, Window], name: str | None, state: PoolState, resets_at: str | None,
           now_s: float, utilization: float | None = None, *,
           paid_continue_until: str | None = None) -> dict[str, Window]:
    """The windows after a report of ``state`` for window ``name``. A milder
    report for the same reset within ``SETTLE_S`` of the window's last change
    is dropped (it was seen before that change). A report with no number
    keeps the last one for the same window and reset. A named report settles
    a limit that named no window, once that limit is ``SETTLE_S`` old: the
    named windows say the account's state from then on. A ``paid`` window the
    person chose to go on with (``paid_continue_until``) counts as open."""
    key = name or UNNAMED
    old = windows.get(key)
    was = old and _effective(old, _iso(now_s), paid_continue_until)
    if old is not None and old.resets_at == resets_at and SEVERITY[state] < SEVERITY[was] \
            and now_s - _seconds(old.since, 0.0) < SETTLE_S:
        return dict(windows)
    if utilization is None and old is not None and old.resets_at == resets_at:
        utilization = old.utilization
    same = old is not None and (old.state, old.resets_at) == (state, resets_at)
    out = {**windows, key: Window(state, resets_at, old.since if same else _iso(now_s), utilization)}
    unnamed = out.get(UNNAMED)
    if key != UNNAMED and unnamed is not None and now_s - _seconds(unnamed.since, 0.0) >= SETTLE_S:
        del out[UNNAMED]
    return out


def current(windows: Mapping[str, Window], now_s: float) -> dict[str, Window]:
    """The windows whose reset hasn't passed: one past its reset is a new
    window, of unknown state until it is reported."""
    return {k: w for k, w in windows.items()
            if w.resets_at is None or (when := parse_ts(w.resets_at)) is None or when.timestamp() > now_s}


def standing(windows: Mapping[str, Window], now_s: float, *, paid_continue_until: str | None = None,
             overloaded: bool = False) -> tuple[PoolState, str | None]:
    """The pool's state, its worst current window's, and when that ends: the
    last reset of the windows in that state (None when one has no known
    reset). A ``paid`` window counts as open while the person chose to go on
    (``paid_continue_until``); an overloaded service makes an open pool warn."""
    now = _iso(now_s)
    states = {k: _effective(w, now, paid_continue_until) for k, w in current(windows, now_s).items()}
    worst: PoolState = max(states.values(), key=SEVERITY.__getitem__, default="open")
    if worst == "open":
        return ("warn" if overloaded else "open"), None
    resets = [windows[k].resets_at for k, s in states.items() if s == worst]
    return worst, (None if None in resets else max(resets, key=lambda r: _seconds(r, now_s)))


def next_reset(windows: Mapping[str, Window], now_s: float) -> str | None:
    """The soonest reset of a window that isn't open: when the pool's
    standing may change with no report. One that has passed already is
    returned too (the pool is looked at again at once, and drops it)."""
    resets = [w.resets_at for w in windows.values() if w.state != "open" and w.resets_at]
    return min(resets, key=lambda r: _seconds(r, now_s), default=None)


def windows_of(snapshot: Mapping | None) -> dict[str, Window]:
    """The windows a stored pool snapshot keeps (none from an older one)."""
    raw = (snapshot or {}).get("windows")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): w for k, v in raw.items() if isinstance(v, Mapping) and (w := Window.of(v)) is not None}


def _effective(window: Window, now: str, paid_continue_until: str | None) -> PoolState:
    return "open" if window.state == "paid" and paid_continue_until and paid_continue_until > now else window.state


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(TS_FORMAT)


def _seconds(ts: str, default: float) -> float:
    when = parse_ts(ts)
    return when.timestamp() if when is not None else default


@dataclass(frozen=True)
class Candidate:
    """A session with queued wakes: its most urgent rank (after aging), its
    oldest wake, and the project it belongs to (round-robin key)."""

    session_id: str
    rank: int
    oldest: str
    project: str


def effective_rank(priority: str, oldest: str, now_s: float) -> int:
    """A ``later`` wake older than ``AGING_S`` counts as ``next``."""
    rank = PRIORITY_RANK[priority]
    if priority == "later":
        created = parse_ts(oldest)
        if created is not None and now_s - created.timestamp() >= AGING_S:
            return PRIORITY_RANK["next"]
    return rank


def fair_order(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Priority class first; within it every project's oldest session, then
    every project's second oldest, and so on (round-robin); then oldest."""
    by_class: dict[int, dict[str, list[Candidate]]] = {}
    for cand in candidates:
        by_class.setdefault(cand.rank, {}).setdefault(cand.project, []).append(cand)
    ordered: list[Candidate] = []
    for rank in sorted(by_class):
        rows = []
        for sessions in by_class[rank].values():
            for turn_in_project, cand in enumerate(sorted(sessions, key=lambda x: x.oldest)):
                rows.append((turn_in_project, cand.oldest, cand))
        ordered.extend(cand for _, _, cand in sorted(rows, key=lambda r: (r[0], r[1])))
    return ordered
