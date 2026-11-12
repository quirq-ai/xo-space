"""The advisory's snapshot: everything it knows, at one version.

Facts only, never a verdict: what each account has used and can still do,
what the machine has, and what each live session costs. Whoever reads it
(the engine's usage check, an agent, the person's UI) decides what to do.

Every type here is frozen, and a snapshot is never changed once published:
a reader holds one whole version, never a half-updated one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from services.advisory.facts import UNKNOWN, Fact, iso_at

#: An account: its agent, and the account label the agent's runtime reports
#: (``RuntimeInfo.account``, a non-secret fingerprint of the login). None is
#: a runtime that named no account: an account of its own, never merged with
#: a label. Whoever pushes facts or reads them names an account this way.
AccountId = tuple[str, str | None]


def frozen(mapping: Mapping[Any, Any] | None = None) -> Mapping[Any, Any]:
    """A read-only copy, for a snapshot's mappings."""
    return MappingProxyType(dict(mapping or {}))


@dataclass(frozen=True)
class Window:
    """One usage window of an account (the vendor's name for it: ``five_hour``,
    ``seven_day``, ``weekly``). Within one window (one reset) utilization and
    status only rise; once its reset passes, all three are unknown until the
    next window is seen."""

    utilization: Fact[float] = UNKNOWN          # 0..1
    resets_at: Fact[str] = UNKNOWN              # UTC, rounded to the minute
    status: Fact[str] = UNKNOWN                 # "ok" | "warning" | "limited"

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {"utilization": self.utilization.as_dict(now_s), "resets_at": self.resets_at.as_dict(now_s),
                "status": self.status.as_dict(now_s)}


@dataclass(frozen=True)
class Paid:
    """What the account can do beyond its plan, in the vendor's words."""

    possible: Fact[bool] = UNKNOWN              # it can move onto paid usage now
    in_use: Fact[bool] = UNKNOWN                # it is on paid usage now
    credits: Fact[float] = UNKNOWN              # its balance, when the vendor reports one
    status: Fact[str] = UNKNOWN                 # the vendor's own word ("rejected", …)
    disabled_reason: Fact[str] = UNKNOWN        # e.g. "org_level_disabled"

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {name: getattr(self, name).as_dict(now_s)
                for name in ("possible", "in_use", "credits", "status", "disabled_reason")}


@dataclass(frozen=True)
class Account:
    agent: str
    account: str | None                         # the runtime's account label, never a credential
    plan: Fact[str] = UNKNOWN
    windows: Mapping[str, Window] = field(default_factory=frozen)
    paid: Paid = field(default_factory=Paid)

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {"agent": self.agent, "account": self.account, "plan": self.plan.as_dict(now_s),
                "windows": {name: w.as_dict(now_s) for name, w in sorted(self.windows.items())},
                "paid": self.paid.as_dict(now_s)}


@dataclass(frozen=True)
class Machine:
    """The container this Space runs in, as its cgroup sees it: what the
    out-of-memory killer and the CPU scheduler act on, not the host's view."""

    cpu_limit: Fact[float] = UNKNOWN            # CPUs this container may use
    cpu_used: Fact[float] = UNKNOWN             # CPUs in use, over the last sample interval
    cpu_pressure: Fact[float] = UNKNOWN         # % of the last 10 s some task waited for a CPU
    memory_limit: Fact[int] = UNKNOWN           # bytes
    memory_used: Fact[int] = UNKNOWN            # bytes charged to the container, page cache included
    memory_working_set: Fact[int] = UNKNOWN     # used minus reclaimable inactive file pages
    memory_available: Fact[int] = UNKNOWN       # limit minus working set
    memory_pressure: Fact[float] = UNKNOWN      # % of the last 10 s some task stalled on memory
    memory_pressure_full: Fact[float] = UNKNOWN  # % of the last 10 s every task stalled on memory
    io_pressure: Fact[float] = UNKNOWN          # % of the last 10 s some task waited on IO
    oom_kills: Fact[int] = UNKNOWN              # processes the OOM killer ended, since the container started
    memory_limit_hits: Fact[int] = UNKNOWN      # times usage reached the limit

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {name: getattr(self, name).as_dict(now_s) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Session:
    """One live session: its memory now, and its last turn's numbers."""

    agent: str | None                           # its agent and account, as the engine named them
    account: str | None
    memory_pss: Fact[int] = UNKNOWN             # bytes, proportional set size over its process tree
    processes: Fact[int] = UNKNOWN              # processes in its tree
    turn_cost_usd: Fact[float] = UNKNOWN        # the last turn's own spend
    context_tokens: Fact[int] = UNKNOWN         # the context's size at the end of the last turn
    context_window: Fact[int] = UNKNOWN         # the model's context window

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {"agent": self.agent, "account": self.account,
                **{name: getattr(self, name).as_dict(now_s) for name in self.__dataclass_fields__
                   if name not in ("agent", "account")}}


@dataclass(frozen=True)
class Snapshot:
    version: int
    as_of: str                                  # UTC, ending in Z
    accounts: Mapping[AccountId, Account] = field(default_factory=frozen)
    machine: Machine = field(default_factory=Machine)
    sessions: Mapping[str, Session] = field(default_factory=frozen)

    def account(self, agent: str, account: str | None) -> Account | None:
        """What is known of ``agent``'s ``account`` (None: the account of a
        runtime that named none), or None when nothing is."""
        return self.accounts.get((agent, account))

    def same_facts(self, other: "Snapshot") -> bool:
        """Whether ``other`` says the same thing (its version and time aside)."""
        return (self.accounts, self.machine, self.sessions) == (other.accounts, other.machine, other.sessions)

    def as_dict(self, now_s: float) -> dict[str, Any]:
        return {"version": self.version, "as_of": self.as_of, "served_at": iso_at(now_s),
                "accounts": [a.as_dict(now_s) for _, a in sorted(self.accounts.items(), key=_by_account)],
                "machine": self.machine.as_dict(now_s),
                "sessions": {sid: s.as_dict(now_s) for sid, s in sorted(self.sessions.items())}}


def _by_account(item: tuple[AccountId, Account]) -> tuple[str, bool, str]:
    (agent, account), _ = item
    return agent, account is not None, account or ""
