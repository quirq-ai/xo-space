"""The usage book: what each account has used, window by window, and what it
can do beyond its plan.

Readings come from two places, through the runtime contract only (no agent
is named here):

* ``reported``: a ``RateLimit`` event a live turn sent (the engine passes
  each one on as it arrives);
* ``measured``: a runtime's model-free read from the vendor
  (``UsageReader.read_usage``), which the advisory makes itself.

The rules, the same for every agent:

* **One window, whatever the jitter.** Vendors send one reset a second or a
  fraction apart from call to call: two resets within ``SAME_WINDOW_S`` of
  each other name the same window. The book keeps the reset exactly (when a
  window ends, and when to read again after it), as first seen for that
  window so it never flips; it is published rounded to the minute.
* **Within one window, a lower value waits.** Processes that share an
  account each report what their own call saw, so a call made just before
  another can report after it, and a read in flight can land after a turn's
  newer report; a vendor's read can also be less precise than its turns'.
  So a lower value (utilization, or status: ok < warning < limited) never
  replaces a higher one from any source until the higher one is
  ``MAX_AGE_S["usage"]`` older than it: the higher one holds, unchanged, and
  then the newer, lower value is the truth (credits bought, a plan changed,
  a limit lifted). Nothing shows lower than the truth for that long, and a
  value that was never true again doesn't stay forever.
* **A reading with no value keeps the last** for the same window (a read
  that gives no status keeps the status the turns reported).
* **An ended or earlier window is gone.** A reading whose reset had passed
  when it was taken, or whose reset is before the one kept, describes a
  window that is over (a process or a read that hasn't seen the reset yet),
  and is dropped.
* **A reset is never assumed.** Once a window's reset passes, its facts are
  unknown until the next window is seen; a published fact says so when
  served, too (its ``stale_after`` is never past the reset).
* **What the account can do beyond its plan** (``PaidUsage``): each field
  keeps its last value until the vendor says another; None is "didn't say".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Mapping

from services.advisory.facts import MAX_AGE_S, Fact, Reading, Source, fact_of, iso_at
from services.advisory.model import Account, AccountId, Paid, Window, frozen
from services.cowork_agent.adapters.contract import PaidUsage, RateLimit, UsageSnapshot
from services.timestamps import parse_ts

#: Milder to worse: within one window a reported status only rises.
SEVERITY: Mapping[str, int] = {"ok": 0, "warning": 1, "limited": 2}
#: Two resets this close (seconds) are one window's.
SAME_WINDOW_S = 60.0
#: The window of a report that names none (a limit the runtime hit unnamed).
UNNAMED = "unnamed"
PAID_FIELDS = ("possible", "in_use", "credits", "status", "disabled_reason")


def reset_seconds(stamp: str | None) -> float | None:
    """A reset time as epoch seconds, exactly as the vendor gave it."""
    when = parse_ts(stamp)
    return when.timestamp() if when is not None else None


def to_minute(seconds: float) -> float:
    """``seconds`` rounded to the nearest minute, as a reset is published."""
    return round(seconds / 60.0) * 60.0


@dataclass(frozen=True)
class WindowState:
    """What the book keeps of one window: the last reading of each value."""

    resets: Reading[float] | None = None          # epoch seconds, exact
    utilization: Reading[float] | None = None
    status: Reading[str] | None = None

    def ended(self, now_s: float) -> bool:
        return self.resets is not None and self.resets.value <= now_s


def merge(old: WindowState | None, new: WindowState, now_s: float) -> WindowState:
    """``old`` after the reading ``new`` (one of whose values may be None),
    taken at ``now_s``, by the module's rules."""
    if old is None or old.ended(now_s):
        return new
    old_reset = old.resets.value if old.resets else None
    new_reset = new.resets.value if new.resets else None
    if old_reset is not None and new_reset is not None and abs(new_reset - old_reset) > SAME_WINDOW_S:
        return new if new_reset > old_reset else old    # the next window; or one that is over
    if old.resets is not None and new.resets is not None:
        # The same window, seen again: its reset stays the one first seen, confirmed now.
        resets = Reading(old.resets.value, new.resets.source, max(old.resets.at, new.resets.at))
    else:
        resets = new.resets or old.resets
    return WindowState(resets, _rise(old.utilization, new.utilization, float),
                       _rise(old.status, new.status, SEVERITY.__getitem__))


def _rise(old: Reading | None, new: Reading | None, rank) -> Reading | None:
    """Within one window: the higher value. A lower one waits until the
    higher is ``MAX_AGE_S["usage"]`` older than it; the higher one is kept
    as it was (never restamped), so it ages like any other reading."""
    if new is None:
        return old
    if old is None or rank(new.value) >= rank(old.value) or new.at - old.at > MAX_AGE_S["usage"]:
        return new
    return old


@dataclass
class AccountState:
    windows: dict[str, WindowState] = field(default_factory=dict)
    plan: Reading[str] | None = None
    paid: dict[str, Reading] = field(default_factory=dict)


class UsageBook:
    """Every account's usage, by agent and account. Not thread-safe: the
    advisory calls it from the event loop only."""

    def __init__(self) -> None:
        self._accounts: dict[AccountId, AccountState] = {}

    def record_event(self, key: AccountId, event: RateLimit, at: float) -> None:
        """One window's report from a live turn."""
        state = self._accounts.setdefault(key, AccountState())
        self._window(state, event.window or UNNAMED, event.status, event.utilization, event.resets_at,
                     "reported", at)
        self._paid(state, event.paid, "reported", at)

    def record_read(self, key: AccountId, snapshot: UsageSnapshot, at: float) -> None:
        """A runtime's whole usage, as it read or last heard it."""
        state = self._accounts.setdefault(key, AccountState())
        source: Source = "measured" if snapshot.source == "measured" else "reported"
        for window in snapshot.windows:
            self._window(state, window.name, window.status, window.utilization, window.resets_at, source, at)
        if snapshot.plan:
            state.plan = Reading(snapshot.plan, source, at)
        self._paid(state, snapshot.paid, source, at)

    def accounts(self, now_s: float) -> dict[AccountId, Account]:
        """Every account as it stands at ``now_s``."""
        return {key: self._publish(key, state, now_s) for key, state in self._accounts.items()}

    def resets(self, key: AccountId) -> list[float]:
        """The exact reset (epoch seconds) of each of the account's windows
        that has one."""
        state = self._accounts.get(key)
        return [w.resets.value for w in state.windows.values() if w.resets is not None] if state else []

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _window(state: AccountState, name: str, status: str | None, utilization: float | None,
                resets_at: str | None, source: Source, at: float) -> None:
        reset = reset_seconds(resets_at)
        if reset is not None and reset <= at:
            return                      # its window had ended when this was seen: it says nothing now
        new = WindowState(Reading(reset, source, at) if reset is not None else None,
                          Reading(float(utilization), source, at) if utilization is not None else None,
                          Reading(status, source, at) if status in SEVERITY else None)
        state.windows[name] = merge(state.windows.get(name), new, at)

    @staticmethod
    def _paid(state: AccountState, paid: PaidUsage | None, source: Source, at: float) -> None:
        if paid is None:
            return
        for name in PAID_FIELDS:
            value = getattr(paid, name)
            if value is not None:
                state.paid[name] = Reading(value, source, at)

    @staticmethod
    def _publish(key: AccountId, state: AccountState, now_s: float) -> Account:
        agent, account = key
        windows = {name: _window_facts(w, now_s) for name, w in state.windows.items()}
        paid = Paid(**{name: fact_of(state.paid.get(name), now_s, "paid") for name in PAID_FIELDS})
        return Account(agent=agent, account=account, plan=fact_of(state.plan, now_s, "paid"),
                       windows=frozen(windows), paid=paid)


def _window_facts(state: WindowState, now_s: float) -> Window:
    if state.ended(now_s):
        # A new window began: nothing about it is known until it is seen.
        return Window(*(Fact(None, "unknown", iso_at(r.at)) if r is not None else Fact()
                        for r in (state.utilization, state.resets, state.status)))
    ends = state.resets.value if state.resets is not None else math.inf

    def until_reset(fact: Fact) -> Fact:
        """A window's fact is unknown when served past its reset, too."""
        if not fact.known or ends == math.inf:
            return fact
        return replace(fact, stale_after=min(fact.stale_after if fact.stale_after is not None else math.inf, ends))
    resets = fact_of(state.resets, now_s, "usage")
    if resets.known:
        resets = replace(resets, value=iso_at(to_minute(state.resets.value)))
    return Window(utilization=until_reset(fact_of(state.utilization, now_s, "usage")),
                  resets_at=until_reset(resets), status=until_reset(fact_of(state.status, now_s, "usage")))
