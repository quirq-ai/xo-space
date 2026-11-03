"""Whether each runtime is usable now: one view the routes, the chats and
the runtime choice read (mixed-runtime plan, step 3).

A runtime's health is what is already known, combined; nothing here probes
on its own:

* **installed / logged in / account / version:** the runtime's
  ``describe()``, which keeps its own probes (cheap after the first);
* **its usage limits:** the engine's pool for that account (its state, its
  reset and each window's state), which rate-limit events keep current;
* **the last failure:** ``describe()``'s ``RuntimeFailure``, if it raised.

``usable`` is the one answer callers need: installed, logged in, no failure,
and its pool admits new work (``open`` or ``warn``). On a server that runs no
engine the pool is unknown, and doesn't count against it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from services.cowork_agent.adapters import contract as c
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
    checked_at: str = ""

    @property
    def usable(self) -> bool:
        return (self.installed and self.logged_in and self.error is None
                and (self.limit is None or self.limit in ADMITTING))

    @property
    def why_not(self) -> str | None:
        """Why it isn't usable, in one clause; None when it is."""
        if not self.installed:
            return "it has no storm runtime"
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
                "error": dict(self.error) if self.error else None, "checked_at": self.checked_at}


async def check(agent: str, runtime: c.Runtime | None, pools: Callable[[str, str | None], Pool | None] | None
                ) -> Health:
    """One runtime's health. ``pools(agent, account)`` is the engine's pool
    for that account (None for one not seen yet); without it (no engine
    here) the limit is unknown."""
    now = now_iso()
    if runtime is None:
        return Health(agent, installed=False, checked_at=now)
    try:
        info = await runtime.describe()
    except c.RuntimeFailure as exc:
        return Health(agent, installed=True, error={"code": exc.code, "message": exc.message}, checked_at=now)
    account = info.account if info.logged_in else None
    pool = pools(agent, account) if pools is not None else None
    limit = (pool.state if pool is not None else "open") if pools is not None else None
    return Health(agent, installed=True, logged_in=info.logged_in, account=account, cli_version=info.cli_version,
                  pool=pool.id if pool is not None else None, limit=limit,
                  resets_at=pool.resets_at if pool is not None else None,
                  windows={k: w.as_dict() for k, w in pool.windows.items()} if pool is not None else {},
                  checked_at=now)


async def check_all(agents: list[str], runtimes: Callable[[str], c.Runtime | None],
                    pools: Callable[[str, str | None], Pool | None] | None) -> list[Health]:
    """Every agent's health, read in parallel."""
    return list(await asyncio.gather(*(check(a, runtimes(a), pools) for a in agents)))
