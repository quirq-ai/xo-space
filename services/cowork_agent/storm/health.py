"""Whether each runtime is usable now: one view the routes, the chats and
the runtime choice read (mixed-runtime plan, step 3).

A runtime's health is what is already known, combined; nothing here probes
on its own:

* **installed / logged in / account / version:** the runtime's
  ``describe()``, which keeps its own probes (cheap after the first);
  ``installed`` is false when there is no adapter, or its CLI is missing
  (``cli_missing``);
* **its usage limits:** the engine's pool for that account (its state, its
  reset and the windows it stands on), computed from the space advisory's
  facts (``services/advisory``), which turns report and the advisory also
  reads with no model call while nothing runs; with no engine here, the
  advisory's windows alone;
* **the last failure:** ``describe()``'s ``RuntimeFailure``, if it raised;
* **an update it wants:** ``describe()``'s ``update`` probe, a runtime's
  note that its CLI is older than the one it is tested on. It still runs, so
  it is said here and never counts against ``usable``.

``usable`` is the one answer callers need: installed, logged in, no failure,
and its pool admits new work (``open`` or ``warn``). On a server that runs no
engine the pool is unknown, and doesn't count against it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from services import advisory as space_advisory
from services.advisory.runtimes import MISSING
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.describe_once import describe_once
from services.cowork_agent.storm.engine import admission
from services.cowork_agent.storm.engine.admission import Pool
from services.timestamps import now_iso

#: Pool states in which a new turn may start (``warn`` holds only ``later`` work).
ADMITTING = ("open", "warn")


@dataclass(frozen=True)
class Health:
    agent: str
    installed: bool
    logged_in: bool = False
    account: str | None = None
    cli_version: str | None = None
    pool: str | None = None
    limit: str | None = None                 # the pool's state; None when no engine runs here
    resets_at: str | None = None
    windows: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, str] | None = None
    update: str | None = None                # why its CLI should be updated; None when it is current
    checked_at: str = ""

    @property
    def usable(self) -> bool:
        return (self.installed and self.logged_in and self.error is None
                and (self.limit is None or self.limit in ADMITTING))

    @property
    def why_not(self) -> str | None:
        """Why it isn't usable, in one clause; None when it is."""
        if not self.installed:
            return f"it isn't installed ({self.error['message']})" if self.error else "it has no storm runtime"
        if self.error is not None:
            return f"it can't be reached ({self.error['message']})"
        if not self.logged_in:
            return "it isn't logged in"
        if self.limit not in (None, *ADMITTING):
            until = f" until {self.resets_at}" if self.resets_at else ""
            return f"its usage limit is {self.limit}{until}"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "usable": self.usable, "why_not": self.why_not, "installed": self.installed,
                "logged_in": self.logged_in, "account": self.account, "cli_version": self.cli_version,
                "pool": self.pool, "limit": self.limit, "resets_at": self.resets_at, "windows": dict(self.windows),
                "error": dict(self.error) if self.error else None, "update": self.update,
                "checked_at": self.checked_at}


async def check(agent: str, runtime: c.Runtime | None, pools: Callable[[str, str | None], Pool] | None,
                *, advisory: space_advisory.Advisory | None = None) -> Health:
    """One runtime's health. ``pools(agent, account)`` is the engine's pool
    for that account as it stands now; without it (no engine here) the
    limit is unknown, and the windows are the advisory's alone.
    ``advisory`` is this server's by default."""
    if runtime is None:
        return Health(agent, installed=False, checked_at=now_iso())
    try:
        return described(agent, await describe_once(agent, runtime), pools, advisory=advisory)
    except c.RuntimeFailure as exc:
        return described(agent, exc, pools, advisory=advisory)


def described(agent: str, info: c.RuntimeInfo | c.RuntimeFailure,
              pools: Callable[[str, str | None], Pool] | None, *,
              advisory: space_advisory.Advisory | None = None) -> Health:
    """A runtime's health from what its ``describe()`` returned or raised, for
    a caller that has already asked it."""
    now = now_iso()
    if isinstance(info, c.RuntimeFailure):
        # The adapter is here but its CLI isn't: that runtime is not installed.
        return Health(agent, installed=info.code != MISSING, error={"code": info.code, "message": info.message},
                      checked_at=now)
    update = info.probes.get("update") if isinstance(info.probes.get("update"), str) else None
    return _limited(Health(agent, installed=True, logged_in=info.logged_in,
                           account=info.account if info.logged_in else None, cli_version=info.cli_version,
                           update=update, checked_at=now), pools, advisory)


def known(facts: space_advisory.RuntimeFacts, pools: Callable[[str, str | None], Pool] | None, *,
          advisory: space_advisory.Advisory | None = None) -> Health:
    """A runtime's health from the advisory's facts of its CLI, with no probe."""
    health = Health(facts.name, installed=facts.installed, logged_in=facts.signed_in, account=facts.account,
                    cli_version=facts.version, update=facts.update, checked_at=now_iso())
    return _limited(health, pools, advisory) if facts.installed else health


def _limited(health: Health, pools: Callable[[str, str | None], Pool] | None,
             advisory: space_advisory.Advisory | None) -> Health:
    """``health`` with its account's pool and windows."""
    pool = pools(health.agent, health.account) if pools is not None else None
    advisory = advisory or space_advisory.current()
    shown = pool.windows if pool is not None else \
        admission.windows_now(advisory.snapshot().account(health.agent, health.account), {}, advisory.now())
    return replace(health, pool=pool.id if pool is not None else None, limit=pool.state if pool is not None else None,
                   resets_at=pool.resets_at if pool is not None else None,
                   windows={name: w.as_dict() for name, w in sorted(shown.items())})


#: How long one runtime's ``describe()`` may take when every runtime is read
#: together: one that hangs mustn't hold the others' answer.
CHECK_WAIT_S = 5.0


async def check_all(agents: Sequence[str], runtimes: Callable[[str], c.Runtime | None],
                    pools: Callable[[str, str | None], Pool | None] | None, *,
                    wait_s: float = CHECK_WAIT_S, advisory: space_advisory.Advisory | None = None) -> list[Health]:
    """Every agent's health, read in parallel; one that doesn't answer within
    ``wait_s`` is reported as failing."""
    async def bounded(agent: str) -> Health:
        try:
            return await asyncio.wait_for(check(agent, runtimes(agent), pools, advisory=advisory), wait_s)
        except TimeoutError:
            return slow(agent, wait_s)
    return list(await asyncio.gather(*(bounded(a) for a in agents)))


def slow(agent: str, wait_s: float = CHECK_WAIT_S) -> Health:
    """The health of a runtime whose ``describe()`` didn't answer within ``wait_s``."""
    return Health(agent, installed=True, checked_at=now_iso(), error=slow_error(wait_s))


def slow_error(wait_s: float = CHECK_WAIT_S) -> dict[str, str]:
    return {"code": "slow", "message": f"it didn't answer within {wait_s:g} s"}
