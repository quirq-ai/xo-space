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
  window ends, and when to read again after it); it is published rounded to
  the minute.
* **Within one window, what turns report only rises.** Processes that share
  an account each report what their own call saw, so a call made just
  before another can report after it. A lower reported value for the same
  window is never taken: the higher one still holds, and the later reading
  confirms it, published as ``derived`` at that reading's time. The status
  (ok < warning < limited) follows the same rule.
* **The vendor's own read is the truth now.** A measured reading replaces
  what it states, lower or not (credits bought, a plan changed mid-window);
  what it doesn't state (a status it has no word for) keeps the last.
* **A reading with no number keeps the last number** for the same window.
* **An earlier window is gone.** A report for a reset before the one kept
  comes from a process that hasn't seen the reset yet, and is dropped; the
  vendor's own read is the truth now, and is taken.
* **A reset is never assumed.** Once a window's reset passes, its facts are
  unknown until the next window is seen.
* **What the account can do beyond its plan** (``PaidUsage``): each field
  keeps its last value until the vendor says another; None is "didn't say".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from services.advisory.facts import Fact, Reading, Source, fact_of, iso_at
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


def merge(old: WindowState | None, new: WindowState, now_s: float, source: Source) -> WindowState:
    """``old`` after the reading ``new`` (one of whose values may be None),
    taken by ``source``, by the module's rules."""
    if old is None or old.ended(now_s):
        return new
    old_reset = old.resets.value if old.resets else None
    new_reset = new.resets.value if new.resets else None
    if old_reset is not None and new_reset is not None and abs(new_reset - old_reset) > SAME_WINDOW_S:
        if new_reset > old_reset or source == "measured":
            return new                  # the next window, or the vendor's own word on which window it is
        return old                      # a process that hasn't seen the reset yet
    if source == "measured" or old.resets is None or new.resets is None:
        resets = new.resets or old.resets
    else:                               # the same window, seen again: its reset stays the one first seen
        resets = Reading(old.resets.value, new.resets.source, new.resets.at)
    if (old_reset is None and new_reset is None) or source == "measured":
        # The vendor's word now, or nothing that says these are one window: the newest values
        # stand, the last kept where none came.
        return WindowState(resets, new.utilization or old.utilization, new.status or old.status)
    return WindowState(resets, _rise(old.utilization, new.utilization, float),
                       _rise(old.status, new.status, SEVERITY.__getitem__))


def _rise(old: Reading | None, new: Reading | None, rank) -> Reading | None:
    """Within one window: the higher value. A lower reading confirms the kept
    one still holds at its time (``derived``)."""
    if new is None:
        return old
    if old is None or rank(new.value) >= rank(old.value):
        return new
    return Reading(old.value, "derived", max(old.at, new.at))


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
        new = WindowState(Reading(reset, source, at) if reset is not None else None,
                          Reading(float(utilization), source, at) if utilization is not None else None,
                          Reading(status, source, at) if status in SEVERITY else None)
        state.windows[name] = merge(state.windows.get(name), new, at, source)

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
    resets = fact_of(state.resets, now_s, "usage")
    if resets.known:
        resets = replace(resets, value=iso_at(to_minute(state.resets.value)))
    return Window(utilization=fact_of(state.utilization, now_s, "usage"), resets_at=resets,
                  status=fact_of(state.status, now_s, "usage"))
