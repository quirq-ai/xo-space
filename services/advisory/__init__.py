"""The space advisory: live facts about the Space's accounts, machine and
sessions, each with its source and age. Facts only: no threshold, gate or
verdict.

A Space module, not part of any engine: the engine, the agents' tools and
the person's UI all read the same snapshot. It never imports storm, its
types or its keys. Storm imports it, pushes what its turns see
(``record_usage``, ``record_turn``, ``track_process``, ``forget_session``)
and names each account the advisory's way: its agent and the account label
the runtime reported (``snapshot.account(agent, account)``).

``current()`` is the one advisory of this server; ``server.py`` starts its
loops at boot.
"""

from __future__ import annotations

from services.advisory.advisory import Advisory
from services.advisory.facts import Fact
from services.advisory.model import Account, AccountId, Machine, Paid, Session, Snapshot, Window
from services.advisory.runtimes import usage_readers

__all__ = ["Account", "AccountId", "Advisory", "Fact", "Machine", "Paid", "Session", "Snapshot", "Window",
           "current"]

_ADVISORY: Advisory | None = None


def current() -> Advisory:
    """This server's advisory, made on first use."""
    global _ADVISORY
    if _ADVISORY is None:
        _ADVISORY = Advisory(readers=usage_readers)
    return _ADVISORY
