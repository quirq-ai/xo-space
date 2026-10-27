"""Admission: which ready session goes next, and whether its pool lets a new
turn start (ch 07 §5). No caps: these gates follow real limits, and fairness
only orders.

* **The pool** (one per agent's account): ``open`` admits everything;
  ``warn`` (a warning, or a window ≥ 80 % used) admits ``now`` and ``next``
  and holds ``later``; ``closed`` (a window rejected until its reset) and
  ``paid`` (the account moved onto paid extra usage, until the person
  chooses) admit no new turn. Running turns always finish.
* **Fairness:** priority class first (a ``later`` wake older than 10 minutes
  counts as ``next``), then round-robin across projects, then oldest first.
* **Pacing** (in the engine): new processes start about two a second, at most
  four starting at once (boot re-kicks go four at a time).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from services.cowork_agent.adapters.contract import RateLimit
from services.cowork_agent.storm.engine.model import PRIORITY_RANK
from services.timestamps import parse_ts

PoolState = Literal["open", "warn", "closed", "paid"]

WARN_UTILIZATION = 0.8
AGING_S = 600.0
SPAWN_INTERVAL_S = 0.5
MAX_STARTING = 4
#: Below every priority class: the start of a "most urgent so far" search.
LOWEST_RANK = max(PRIORITY_RANK.values()) + 1


@dataclass
class Pool:
    id: str
    state: PoolState = "open"
    resets_at: str | None = None
    paid_continue_until: str | None = None
    overloaded: bool = False            # warn because the service was overloaded, not a usage window

    def admits(self, rank: int) -> bool:
        """May a new turn whose most urgent wake has ``rank`` start?"""
        if self.state == "open":
            return True
        return self.state == "warn" and rank <= PRIORITY_RANK["next"]


def pool_state(event: RateLimit, pool: Pool, now: str) -> tuple[PoolState, str | None]:
    """The pool's state after a ``RateLimit`` event, and its reset time."""
    if event.overage and not (pool.paid_continue_until and pool.paid_continue_until > now):
        return "paid", event.resets_at
    if event.status == "limited":
        return "closed", event.resets_at
    if event.status == "warning" or (event.utilization is not None and event.utilization >= WARN_UTILIZATION):
        return "warn", event.resets_at
    return "open", None


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
