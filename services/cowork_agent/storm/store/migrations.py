"""Bringing an older store to this build's schema, in place (ch 08 §2).

``STEPS[n]`` is the SQL that takes a store at schema ``n - 1`` to ``n``. A
store older than ``SCHEMA_VERSION`` is first migrated in memory: its own
tables are copied (their SQL, no rows) into an empty database, every step
runs there, and the result must be exactly this build's tables. Only then
do the steps run on the file, in one transaction with the new
``user_version``, so a store is either migrated whole or left as it was. A
store whose tables a step doesn't expect is refused unchanged, with the
same advice as before.

The rehearsal has no rows, so it can't see a step that fails on the data a
real store holds (a constraint its rows break). The run on the file is
therefore checked too: the steps and ``verify`` (the migrated tables,
indexes and foreign keys against this build's) share one transaction, and
anything that fails, or a ``verify`` that finds a difference, rolls the
whole of it back. The file is then exactly as it was before.

A step only adds: a table, an index, or a column (which ALTER TABLE puts
last, so ``schema.sql`` lists it last in its table too). Nothing a person
or an agent wrote is ever rewritten by a migration.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

#: The SQL each schema version adds to the one before it.
STEPS: dict[int, str] = {
    2: """
ALTER TABLE threads ADD COLUMN summary TEXT CHECK (summary IS NULL OR length(summary) <= 90);
ALTER TABLE threads ADD COLUMN summary_at TEXT;

CREATE TABLE thread_git (
    chat_id       TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (json_valid(state)),
    checked_at    TEXT NOT NULL,
    pr_checked_at TEXT,
    PRIMARY KEY (chat_id, thread_ts),
    FOREIGN KEY (chat_id, thread_ts) REFERENCES threads (chat_id, ts) ON DELETE CASCADE
);

CREATE TABLE chat_seen (
    chat_id  TEXT PRIMARY KEY REFERENCES chats (id) ON DELETE CASCADE,
    seen_at  TEXT NOT NULL,
    seen_seq INTEGER NOT NULL
);

CREATE TABLE made (
    id         TEXT PRIMARY KEY,
    seq        INTEGER NOT NULL,
    chat_id    TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts  TEXT,
    message_id TEXT,
    turn_id    TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('file', 'image', 'link', 'branch')),
    source     TEXT NOT NULL CHECK (source IN ('share', 'write', 'reply', 'branch')),
    path       TEXT,
    url        TEXT,
    name       TEXT NOT NULL,
    title      TEXT,
    mime       TEXT,
    bytes      INTEGER,
    copy_path  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pinned_at  TEXT,
    CHECK ((kind = 'link') = (url IS NOT NULL)),
    CHECK (kind NOT IN ('file', 'image') OR path IS NOT NULL)
);
CREATE INDEX made_by_chat ON made (chat_id, updated_at);
""",
}


def steps_from(version: int, target: int) -> list[str]:
    """The steps that take a store at ``version`` to ``target``; a missing
    step is a ``KeyError`` (a build that can't migrate that store)."""
    return [STEPS[n] for n in range(version + 1, target + 1)]


def rehearse(conn: sqlite3.Connection, version: int, target: int) -> sqlite3.Connection:
    """An in-memory database with ``conn``'s tables and indexes (no rows),
    migrated to ``target``. Raises ``sqlite3.Error`` when a step doesn't
    apply to them, ``KeyError`` when a step is missing. The caller closes it."""
    mem = sqlite3.connect(":memory:")
    try:
        for (sql,) in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, rowid"):
            mem.execute(sql)
        for step in steps_from(version, target):
            mem.executescript(step)
    except BaseException:
        mem.close()
        raise
    return mem


class MigrationMismatch(Exception):
    """The steps ran, but the store they made isn't this build's (``verify``
    said how); everything was rolled back."""


def migrate(conn: sqlite3.Connection, version: int, target: int,
            verify: Callable[[sqlite3.Connection], str | None] | None = None) -> None:
    """Run the steps on the store, check the result with ``verify`` (None:
    as expected; else what differs), and set the new ``user_version``, all
    in one transaction: a step that fails on the store's own rows, or a
    difference ``verify`` finds, leaves the file as it was.
    (``executescript`` commits what was open first; the connection is in
    autocommit mode, so nothing is, and the script leaves its own
    ``BEGIN`` open for the check.)"""
    body = "\n".join(steps_from(version, target))
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{body}\n")
        if verify is not None and (why := verify(conn)):
            raise MigrationMismatch(why)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:         # a step or the check failed: the store stays as it was
            conn.execute("ROLLBACK")
        raise
