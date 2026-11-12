"""The ledger: one entry per change to a record the founder relies on,
chained by hash (record design v2 §2, §5 rules 7 and 8, §6 invariant 9).

Retention prunes most events, so a chain over ``events`` would break: the
ledger is a table of its own that is never pruned. It has no foreign keys,
so it outlives what it names, and its triggers refuse an UPDATE or a
DELETE, so a mistaken edit fails loudly. An entry is written by its
record's own module (``intents.py``, ``goals.py``, ``tasks.py``,
``results.py``, ``timers.py``, ``leases.py``), in the transaction that
changes the record, beside the record's event.

Each entry names its record (an intent is ``intent:<scope>:<scope_id>:<key>``;
a goal, a task, a result, a timer or a lease is ``<kind>:<id>``) and the sha256 of the
record's row as written (``row_sha256``: of the row's canonical JSON,
``null`` for a removal). Its own ``sha256`` covers its
fields with ``prev_sha256``, the hash of the entry before it (``GENESIS``
for the first). ``verify`` walks the chain end to end, then checks that each
record's current row is the one its last entry hashed, and that a record
its last entry removed has no row. It finds an entry edited or removed, and
a row changed, added or removed outside its record's module.

The known limit (record design v2 §8): agents run as the same user as the
Space, so they can reach the store. Verifying catches mistakes and confused
edits, not a deliberate rewrite: whoever can write the file can drop the
triggers and recompute every hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes

#: The actor of a change the founder made (record design v2 §1); any other
#: actor is the session (``cse_…``) that made it, or XO itself.
FOUNDER = "founder"
#: The actor of a change XO made on its own: a timer going off, a lease
#: freed by its holder's exit, what a removed project's chat leaves.
XO = "xo"
#: What the first entry follows.
GENESIS = "0" * 64
#: An intent row's columns, as ``intents`` declares them: what its hash covers.
INTENT_COLUMNS = ("scope", "scope_id", "key", "value", "until", "cites", "set_at")
#: A goal's, a task's, a result's, a timer's and a lease's columns, as their tables declare them.
GOAL_COLUMNS = ("id", "chat_id", "cites", "title", "ends_at", "state", "state_reason", "asked_by", "request_key",
                "created_at", "updated_at")
TASK_COLUMNS = ("id", "goal_id", "chat_id", "thread_ts", "title", "instructions", "state", "last_report",
                "reported_at", "asked_by", "request_key", "created_at", "updated_at")
RESULT_COLUMNS = ("id", "task_id", "submitted_by", "agent", "model", "summary", "artifacts", "cost_usd",
                  "request_key", "created_at")
TIMER_COLUMNS = ("id", "kind", "chat_id", "role", "thread_ts", "due_at", "every_s", "event", "note", "job", "goal_id",
                 "task_id", "state", "state_reason", "fired", "asked_by", "approved_by", "request_key", "created_at",
                 "updated_at")
LEASE_COLUMNS = ("id", "resource", "holder_session", "holder_pid", "holder_start_ticks", "task_id", "acquired_at",
                 "expires_at", "released_at", "released_why")
#: The records named by their row's id (``<kind>:<id>``): each kind's table and columns.
BY_ID: Mapping[str, tuple[str, tuple[str, ...]]] = {"goal": ("goals", GOAL_COLUMNS), "task": ("tasks", TASK_COLUMNS),
                                                    "result": ("results", RESULT_COLUMNS),
                                                    "timer": ("timers", TIMER_COLUMNS),
                                                    "lease": ("leases", LEASE_COLUMNS)}
#: An entry's fields that its own hash covers.
_ENTRY = ("seq", "at", "actor", "action", "record", "row_sha256", "prev_sha256")


def _canonical(value: Any) -> bytes:
    """``value`` as JSON written one way only: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def row_sha256(row: Mapping[str, Any] | None) -> str:
    """The hash of a record's row as written; None is a removal."""
    return hashlib.sha256(_canonical(row)).hexdigest()


#: The ``row_sha256`` of a removal.
REMOVED = row_sha256(None)


def intent_record(scope: str, scope_id: str, key: str) -> str:
    """The ledger's name for one intent. A scope and a key never hold a
    colon, so the name is that intent's alone whatever its scope id holds
    (a pool id has colons)."""
    return f"intent:{scope}:{scope_id}:{key}"


def request_key(*parts: Any) -> str:
    """A request's idempotency key: the hash of what it asks, in canonical
    JSON. The same request (the same asker, tool and arguments) gives the
    same key, so a retry finds the record the first one made."""
    return hashlib.sha256(_canonical(list(parts))).hexdigest()


@dataclass(frozen=True)
class Finding:
    """One thing ``verify`` found, in words (``problem``), with the entry it
    concerns and the record it is about; either is None where there is none
    (entries that are missing; a row that no entry names)."""

    seq: int | None
    record: str | None
    problem: str


def append(w: Writes, *, actor: str, action: str, record: str, row: Mapping[str, Any] | None) -> int:
    """Add the entry for a change to ``record`` (``row`` as written, None
    for a removal) at the end of the chain, in ``w``'s transaction, and
    return its seq. The seq is the next the table has ever given, never a
    removed entry's, so a removal at the end stays a gap ``verify`` finds."""
    last = w.execute("SELECT seq, sha256 FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    seq = max(last[0] if last else 0, _counter(w)) + 1
    entry = {"seq": seq, "at": w.now, "actor": actor, "action": action, "record": record,
             "row_sha256": row_sha256(row), "prev_sha256": last[1] if last else GENESIS}
    w.execute("INSERT INTO ledger (seq, at, actor, action, record, row_sha256, prev_sha256, sha256) "
              "VALUES (:seq, :at, :actor, :action, :record, :row_sha256, :prev_sha256, :sha256)",
              {**entry, "sha256": _entry_sha256(entry)})
    return seq


def append_row(w: Writes, *, actor: str, action: str, kind: str, row_id: str) -> int:
    """``append`` for a record named by its row's id (``BY_ID``), hashing
    the row as its table holds it now, in ``w``'s transaction: exactly what
    ``verify`` reads back."""
    return append(w, actor=actor, action=action, record=f"{kind}:{row_id}", row=_row(w, kind, row_id))


def verify(conn: sqlite3.Connection) -> list[Finding]:
    """Everything wrong with the ledger and the rows it covers, in order:
    the chain first (each entry's seq, link and own hash), then each
    record's current row against its last entry. Empty when all of it holds."""
    findings: list[Finding] = []
    seq, prev = 0, GENESIS
    last: dict[str, tuple[int, str]] = {}           # record: its last entry's seq and row_sha256
    for *values, sha in conn.execute(f"SELECT {', '.join(_ENTRY)}, sha256 FROM ledger ORDER BY seq"):
        entry = dict(zip(_ENTRY, values))
        here, record = entry["seq"], entry["record"]
        if here != seq + 1:
            findings.append(Finding(None, None, _missing(seq + 1, here - 1)))
        if entry["prev_sha256"] != prev:
            findings.append(Finding(here, record, "it doesn't follow the entry before it (its prev_sha256 isn't "
                                                  "that entry's sha256)"))
        if sha != _entry_sha256(entry):
            findings.append(Finding(here, record, "it was changed after it was written (its sha256 doesn't match "
                                                  "its fields)"))
        last[record] = (here, entry["row_sha256"])
        seq, prev = here, sha
    if (counter := _counter(conn)) > seq:
        findings.append(Finding(None, None, _missing(seq + 1, counter)))
    rows = _current_rows(conn)
    for record in sorted(rows.keys() | last.keys()):
        entry_seq, hashed = last.get(record, (None, None))
        row = rows.get(record)
        if hashed is None:
            findings.append(Finding(None, record, "it has a row, but no ledger entry names it"))
        elif row is None and hashed != REMOVED:
            findings.append(Finding(entry_seq, record, "its row is gone, but no entry cleared it"))
        elif row is not None and hashed == REMOVED:
            findings.append(Finding(entry_seq, record, "its last entry cleared it, yet it has a row"))
        elif row is not None and hashed != row_sha256(row):
            findings.append(Finding(entry_seq, record, "its row was changed outside the ledger (it isn't the row "
                                                       "its last entry hashed)"))
    return findings


def _entry_sha256(entry: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(entry)).hexdigest()


def _counter(source: Writes | sqlite3.Connection) -> int:
    """The highest seq the ledger ever gave (SQLite's AUTOINCREMENT count),
    removed entries included."""
    row = source.execute("SELECT seq FROM sqlite_sequence WHERE name = 'ledger'").fetchone()
    return row[0] if row else 0


def _missing(first: int, last: int) -> str:
    return f"entry {first} is missing" if first == last else f"entries {first} to {last} are missing"


def _current_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every record that has a row now, by its ledger name, as its entry
    hashes it. The ledger reads the tables it covers itself (``intents`` and
    ``BY_ID``'s), so their modules import it, never the other way round."""
    rows = {}
    for values in conn.execute(f"SELECT {', '.join(INTENT_COLUMNS)} FROM intents"):
        row = dict(zip(INTENT_COLUMNS, values))
        rows[intent_record(row["scope"], row["scope_id"], row["key"])] = row
    for kind, (table, columns) in BY_ID.items():
        for values in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
            rows[f"{kind}:{values[0]}"] = dict(zip(columns, values))
    return rows


def _row(source: Writes | sqlite3.Connection, kind: str, row_id: str) -> dict[str, Any] | None:
    """A ``BY_ID`` record's row as its table holds it, or None."""
    table, columns = BY_ID[kind]
    values = source.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return dict(zip(columns, values)) if values else None
