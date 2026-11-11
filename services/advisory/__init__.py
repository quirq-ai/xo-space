"""The space advisory: live facts about the Space's accounts, machine and
sessions, each with its source and age, and never a verdict (storm plan W4).

Space-level: the engine, the agents' tools and the person's UI all read the
same snapshot. It never imports storm; storm imports it and pushes what its
turns see (``record_usage``, ``record_turn``, ``track_process``,
``forget_session``).

``current()`` is the one advisory of this server; ``server.py`` starts its
loops at boot.
"""

from __future__ import annotations

from services.advisory.advisory import Advisory
from services.advisory.facts import Fact
from services.advisory.model import Account, Machine, Paid, Session, Snapshot, Window, account_key
from services.advisory.runtimes import usage_readers

__all__ = ["Account", "Advisory", "Fact", "Machine", "Paid", "Session", "Snapshot", "Window", "account_key",
           "current"]

_ADVISORY: Advisory | None = None


def current() -> Advisory:
    """This server's advisory, made on first use."""
    global _ADVISORY
    if _ADVISORY is None:
        _ADVISORY = Advisory(readers=usage_readers)
    return _ADVISORY
