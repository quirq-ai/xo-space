"""Admission: which ready session goes next, and whether its pool lets a new
turn start (ch 07 §5; w2-w3 design §3.4, §5). No caps: these gates follow
real limits and the founder's intent, and fairness only orders.

* **The pool** (one per agent's account) is computed, never kept as a
  truth of its own: from the space advisory's facts for the account, the
  founder's ``paid`` intent and the clock (``standing``). ``open`` admits
  everything; ``warn`` (the vendor's warning, a window at
  ``WARN_UTILIZATION`` or more, or an overloaded service) admits ``now``
  and ``next`` and holds ``later``; ``closed`` (a vendor limit: a window
  limited, or used up with no paid usage to go on to, until its reset) and
  ``paid`` (the paid gate holds it) admit no new turn.
* **A window's facts** are the advisory's, as of now. Once one ages out,
  its last value for the same reset stands until that reset
  (``windows_now``): within one reset a window's share used and status
  never fall below the truth, so the last value is a lower bound.
* **The paid gate**, under the founder's ``paid`` mode (``never`` with no
  intent at all; ``ask``; ``always``). It reads the share used, never a
  status, which a vendor may not give. Under ``never`` and ``ask`` an
  account that can go on to paid usage is held once any current window
  reaches ``PAID_THRESHOLD``, and always while paid usage is in use; one
  that can't (``possible`` known false) never meets the gate. An unknown
  that matters holds it too, with no time limit: an account at the
  threshold that may go paid (it ends running turns, like a known hold),
  one that can go paid with no share used known, or one storm knows nothing
  of yet, while a read can settle it (``readable``: a runtime that can't
  read is told by its first turn's report). An account known to be well
  below the threshold runs whatever is unknown about paid usage: one turn
  can't climb from there into paid. Under ``ask`` each hold is the
  founder's question (its ``subject``, asked once per window; for an
  account with no window, such as an API key, once per
  ``ALLOWANCE_S``), until they answer, and the answer is for that window
  only: Continue lifts the hold, Wait keeps it until the window's reset.
  Neither changes the founder's ``paid`` mode. Under ``always`` nothing is
  held or asked for paid usage, and a used-up window closes the account
  only when it can't go on to paid usage. Holds on known facts end running
  turns too (``Pool.halts``).
* **Fairness:** priority class first (a ``later`` wake older than 10 minutes
  counts as ``next``), then round-robin across projects, then oldest first.
* **Pacing** (in the engine): new processes start about two a second, at most
  four starting at once (boot re-kicks go four at a time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from services.advisory import Account, Paid
from services.advisory.facts import iso_at
from services.cowork_agent.storm.engine.model import PRIORITY_RANK
from services.timestamps import parse_ts

PoolState = Literal["open", "warn", "closed", "paid"]

#: Later work waits once a window is this far used (or the vendor warns).
WARN_UTILIZATION = 0.8
#: The paid gate's threshold: a window at or past this share used holds an
#: account that can go on to paid usage. Measured on 2026-11-12, read-only,
#: from Codex's own reports of the weekly window of the account that went
#: paid on 11-09/10 (~/.codex/sessions: 50 rollouts, 15,741 ``token_count``
#: reports, 482 turns):
#: * one report: near the top the account's share rises at most one point
#:   over its report before (Codex reports whole percents);
#: * one turn, from the account's share when it started to its share at its
#:   end: at most 4 points for the 151 turns started at 90 % or more
#:   (92 → 96; 96 → 100, where the vendor stops counting), 6 for those
#:   started at 80 % or more (82 → 88).
#: So a turn admitted just under the threshold ends under 100 % even if
#: nothing stopped it: 1 − 0.04 (the largest one-turn rise near the top) −
#: 0.01 (one report's resolution). A runtime that reports as it goes is
#: stopped sooner, at its first report at the threshold (the threshold
#: watcher). Claude's recorded events (tests/fixtures/advisory) come only at
#: its warning thresholds (0.75, 0.9, 1.0), too coarse to measure a step,
#: and its account can't go paid, so the gate doesn't apply to it.
PAID_THRESHOLD = 0.95
#: How long the founder's Continue lasts under ``ask`` for an account with
#: no usage window (an API key: every token is billed): one question a day.
ALLOWANCE_S = 24 * 3600.0
#: The paid modes (``records.intents`` holds the founder's).
MODES = ("never", "ask", "always")
AGING_S = 600.0
SPAWN_INTERVAL_S = 0.5
MAX_STARTING = 4
#: Below every priority class: the start of a "most urgent so far" search.
LOWEST_RANK = max(PRIORITY_RANK.values()) + 1
#: The states that admit no new turn, and send nothing into a running one.
HOLDS = ("closed", "paid")
#: A paid-usage question's subject: ``paid:<pool>:<reset>``, one per window;
#: ``paid:<pool>:allow:<end>`` for an account with no window (``ALLOWANCE_S``).
PAID_SUBJECT = "paid:"
ALLOWANCE = "allow"

#: Why a pool isn't open (the ``pool`` event's ``reason``).
LIMIT = "limit"                        # a vendor limit, until its reset
WARNING = "warning"                    # near a limit: later work waits
OVERLOADED = "overloaded"              # the service was overloaded: later work waits
PAID_THRESHOLD_REACHED = "paid_threshold"   # it can go paid and a window is at the threshold
PAID_IN_USE = "paid_in_use"            # it is on paid usage
PAID_ASKED = "paid_asked"              # under ask: the founder's answer for this window is awaited
PAID_UNKNOWN = "paid_unknown"          # XO can't tell whether going on would be paid
#: The holds that end running turns too (``PAID_UNKNOWN`` does when a window is at the threshold).
HALTS = frozenset({PAID_THRESHOLD_REACHED, PAID_IN_USE, PAID_ASKED})
#: What a session held by the paid gate waits for, in its words.
SAYS: Mapping[str, str] = {PAID_THRESHOLD_REACHED: "held at the paid-usage threshold",
                           PAID_IN_USE: "held while paid usage is in use",
                           PAID_ASKED: "waiting for the founder's answer on paid usage",
                           PAID_UNKNOWN: "held: XO can't tell whether going on would be paid"}


@dataclass(frozen=True)
class Seen:
    """One usage window as last known: its reset (None when the vendor gave
    none), share used (0..1) and status (None when the vendor didn't say),
    where and when its share used (else its status) was seen, and when it
    ends (``ends``, epoch seconds: the window's reset, exact where the
    advisory's fact said so; None with no reset)."""

    resets_at: str | None
    utilization: float | None
    status: str | None
    source: str
    observed_at: str | None
    ends: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": window_state(self), "resets_at": self.resets_at, "utilization": self.utilization,
                "status": self.status, "source": self.source, "observed_at": self.observed_at}


@dataclass(frozen=True)
class Pool:
    """A pool as it stands: its state, until when (None: not held, or not
    known), why, and the windows it stands on. ``subject`` is the paid
    question this window would ask (under ``ask``, not yet answered);
    ``unknown`` says an unknown fact would hold it, whether or not it may
    still wait for one fresh read."""

    id: str
    state: PoolState = "open"
    resets_at: str | None = None
    reason: str | None = None
    windows: Mapping[str, Seen] = field(default_factory=dict)
    subject: str | None = None
    unknown: bool = False
    #: Under ``ask``: the question this hold stands on (``subject`` is set
    #: only until it is asked); ``cause``: what the hold is about (the
    #: reason it would have under ``never``).
    question: str | None = None
    cause: str | None = None

    def admits(self, rank: int) -> bool:
        """May a new turn whose most urgent wake has ``rank`` start?"""
        if self.state == "open":
            return True
        return self.state == "warn" and rank <= PRIORITY_RANK["next"]

    @property
    def halts(self) -> bool:
        """The paid gate holds it so that its running turns end too: on known
        facts, or at the threshold with paid usage not known to be off."""
        return self.reason in HALTS or (self.reason == PAID_UNKNOWN and bool(_crossing(self.windows.values())))

    def why(self) -> str:
        """Why a session waits on it, in one clause."""
        until = f" until {self.resets_at}" if self.resets_at else ""
        return f"pool {self.state}{until}" + (f": {SAYS[self.reason]}" if self.state == "paid" else "")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "resets_at": self.resets_at, "reason": self.reason,
                "windows": {name: w.as_dict() for name, w in sorted(self.windows.items())}}


def window_state(window: Seen) -> PoolState:
    """One window's own state, as its facts say (the paid gate is the
    account's, not a window's)."""
    used = window.utilization or 0.0
    if window.status == "limited" or used >= 1.0:
        return "closed"
    return "warn" if window.status == "warning" or used >= WARN_UTILIZATION else "open"


def windows_now(account: Account | None, last: Mapping[str, Seen], now_s: float) -> dict[str, Seen]:
    """The account's current windows as of ``now_s``: the advisory's facts
    (``Fact.at``: unknown once too old or past the window's reset), else a
    window's ``last`` known value until its reset (a lower bound). A window
    with neither is left out."""
    out: dict[str, Seen] = {}
    for name, window in (account.windows.items() if account is not None else ()):
        used, status, resets = window.utilization.at(now_s), window.status.at(now_s), window.resets_at.at(now_s)
        if used.known or status.known:
            seen = used if used.known else status
            out[name] = Seen(resets.value, used.value, status.value, seen.source, seen.observed_at,
                             _ends(resets.value, seen.stale_after))
    for name, kept in last.items():
        if name not in out and kept.ends is not None and kept.ends > now_s:
            out[name] = kept
    return out


def _ends(resets_at: str | None, stale_after: float | None) -> float | None:
    """When a window ends: the advisory publishes its reset to the minute,
    but a fact it holds goes unknown at the exact reset, so a fact's end
    within half a minute of the published reset is the reset itself."""
    if resets_at is None:
        return None
    published = _seconds(resets_at, 0.0)
    return stale_after if stale_after is not None and abs(stale_after - published) <= 30.0 else published


def standing(pool_id: str, windows: Mapping[str, Seen], paid: Paid, *, mode: str, answers: Mapping[str, str],
             readable: bool, overloaded: bool = False, now_s: float) -> Pool:
    """The pool from its ``windows`` (``windows_now``), the account's
    ``paid`` facts, the founder's paid ``mode`` (``MODES``) and their
    ``answers`` (a paid question's subject: ``open``, ``continue`` or
    ``wait``). ``readable``: a model-free read of the account can come, so an
    account storm knows nothing of yet waits for it."""
    possible, in_use = paid.possible.at(now_s).value, paid.in_use.at(now_s).value
    crossing = _crossing(windows.values())
    can_go_paid = possible is True or in_use is True

    def pool(state: PoolState, reason: str | None, resets_at: str | None = None, **kw: Any) -> Pool:
        return Pool(pool_id, state, resets_at, reason, dict(windows), **kw)

    if mode != "always" and (possible is not False or in_use is True):
        cause = _paid_cause(windows, possible, in_use, crossing, readable)
        if cause is not None:
            if mode != "ask":
                return pool("paid", cause, _last_reset(crossing) if crossing else None,
                            unknown=cause == PAID_UNKNOWN, cause=cause)
            question, until = _question(pool_id, crossing, answers, now_s)
            said = answers.get(question)
            if said != "continue":
                if said in (None, "open"):
                    return pool("paid", PAID_ASKED, until, subject=question if said is None else None,
                                question=question, unknown=cause == PAID_UNKNOWN, cause=cause)
                return pool("paid", cause, until, unknown=cause == PAID_UNKNOWN, question=question, cause=cause)
    # A used-up window ends the account's work unless it goes on to paid usage: under ``always`` unless
    # that is known impossible (the vendor stops it there if it can't), else unless it is known possible.
    stops = (possible is False and in_use is not True) if mode == "always" else not can_go_paid
    limited = [w for w in windows.values()
               if w.status == "limited" or (stops and (w.utilization or 0.0) >= 1.0)]
    if limited:
        return pool("closed", LIMIT, _last_reset(limited))
    warned = [w for w in windows.values() if window_state(w) == "warn"]
    if warned:
        return pool("warn", WARNING, _last_reset(warned))
    return pool("warn", OVERLOADED) if overloaded else pool("open", None)


def _crossing(windows: Iterable[Seen]) -> list[Seen]:
    """The windows at or past the paid threshold."""
    return [w for w in windows if (w.utilization or 0.0) >= PAID_THRESHOLD]


def _paid_cause(windows: Mapping[str, Seen], possible: bool | None, in_use: bool | None, crossing: list[Seen],
                readable: bool) -> str | None:
    """Why ``never`` holds an account that isn't known unable to go paid,
    or None when it runs: on paid usage, at the threshold with paid usage
    possible, or an unknown that matters (at the threshold with paid usage
    not known; possible with no share used known; nothing known at all),
    the last two only while a read can tell."""
    if in_use is True:
        return PAID_IN_USE
    if crossing:
        return PAID_THRESHOLD_REACHED if possible is True else PAID_UNKNOWN
    if not readable:
        return None
    if not windows or (possible is True and not any(w.utilization is not None for w in windows.values())):
        return PAID_UNKNOWN
    return None


def _question(pool_id: str, crossing: list[Seen], answers: Mapping[str, str], now_s: float) -> tuple[str, str | None]:
    """The paid question an ``ask`` hold stands on, and when its answer
    lapses: one per window (the last crossing window's reset), else one per
    ``ALLOWANCE_S`` (the allowance asked last, while it lasts)."""
    reset = _last_reset(crossing) if crossing else None
    if reset is not None:
        return f"{PAID_SUBJECT}{pool_id}:{reset}", reset
    prefix = f"{PAID_SUBJECT}{pool_id}:{ALLOWANCE}:"
    ends = [subject[len(prefix):] for subject in answers if subject.startswith(prefix)]
    live = [end for end in ends if _seconds(end, 0.0) > now_s]
    end = max(live, key=lambda e: _seconds(e, 0.0)) if live else iso_at(60.0 * -(-(now_s + ALLOWANCE_S) // 60.0))
    return f"{prefix}{end}", end


def _last_reset(windows: Iterable[Seen]) -> str | None:
    """When the last of these windows resets; None when one has no known reset."""
    resets = [w.resets_at for w in windows]
    return None if None in resets else max(resets, key=lambda r: _seconds(r, 0.0), default=None)


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
