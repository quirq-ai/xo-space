"""Storm's records (record design v2): what the founder decided and what
the agents were asked and delivered, kept so that every change can be
traced and checked.

* ``intents``: the founder's intents (``run``, ``paid``) per scope, how
  they are written, and which one holds for a session or a pool;
* ``goals``, ``tasks``, ``results``: what the founder asked for, each
  thread's piece of work and what it reported it delivered, recorded as
  reported, each traced to the founder's message or the session that asked;
* ``ledger``: one hash-chained entry per change to a record, never pruned,
  and ``verify``, which checks the chain and each record's row against it.

Write functions take the engine's ``Writes`` (one engine transaction); a
caller outside the engine wraps its ``Tx`` as ``Writes(tx, now)``. Nothing
here imports the engine, so the engine may import this package.
"""

from __future__ import annotations

from services.cowork_agent.storm.records.ledger import FOUNDER
from services.errors import ServiceError


class RecordInvalid(ServiceError):
    """A goal, task or result the store wouldn't hold; the message says why."""

    def __init__(self, message: str) -> None:
        super().__init__("record_invalid", message)


def check_asker(asked_by: str) -> None:
    """``asked_by`` is the founder or a session id (invariant 1)."""
    if asked_by != FOUNDER and not (isinstance(asked_by, str) and asked_by.startswith("cse_")):
        raise RecordInvalid(f"Who asked is {FOUNDER!r} or the asking session's id (cse_…); {asked_by!r} isn't.")
