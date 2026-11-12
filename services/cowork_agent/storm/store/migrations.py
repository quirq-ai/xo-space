"""Bringing an older store to this build's schema, in place (ch 08 §2).

``STEPS[n]`` is the SQL that takes a store at schema ``n - 1`` to ``n``. A
store older than ``SCHEMA_VERSION`` is first migrated in memory: its own
tables are copied (their SQL, no rows) into an empty database, every step
runs there, and the result must be exactly this build's structure. Only
then do the steps run on the file, in one transaction with the new
``user_version``, so a store is either migrated whole or left as it was. A
store whose tables a step doesn't expect is refused unchanged.

The rehearsal has no rows, so it can't see a step that fails on the data a
real store holds (a constraint its rows break). The run on the file is
therefore checked too, before its COMMIT: ``verify`` compares the migrated
structure (tables, columns, CHECK constraints, indexes, foreign keys, views
and triggers) with this build's, ``PRAGMA foreign_key_check`` finds rows a
step left pointing nowhere, and ``PRAGMA quick_check`` finds a damaged file.
Anything that fails, a difference, or a single finding rolls the whole of
it back: the file is then exactly as it was before.

A step adds tables, indexes, triggers and columns (ALTER TABLE puts a
column last, so ``schema.sql`` lists it last in its table too), and may
drop a column with ALTER TABLE ... DROP COLUMN. SQLite drops only a column
nothing else names (no index, foreign key, view, trigger, generated column
or other CHECK); the column's own CHECK goes with it. No step rewrites a
table: changing a CHECK constraint would take a rebuild, which this module
doesn't do. Rows stay as a person or an agent wrote them, but for a dropped
column's values.
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
    3: """
ALTER TABLE review_items ADD COLUMN subject TEXT;
CREATE UNIQUE INDEX review_one_per_subject ON review_items (subject)
    WHERE subject IS NOT NULL AND state IN ('open', 'decided');

-- Why a session was retired (W7); one retired before this build says legacy.
ALTER TABLE sessions ADD COLUMN retired_reason TEXT CHECK (retired_reason IN ('context_full', 'upgrade',
    'agent_switch', 'project_removed', 'legacy'));
UPDATE sessions SET retired_reason = 'legacy' WHERE retired_at IS NOT NULL;

CREATE TABLE intents (
    scope    TEXT NOT NULL CHECK (scope IN ('space', 'chat', 'coordinator', 'thread', 'session', 'pool')),
    scope_id TEXT NOT NULL,
    key      TEXT NOT NULL CHECK (key IN ('run', 'paid')),
    value    TEXT NOT NULL,
    until    TEXT,
    cites    TEXT,
    set_at   TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id, key),
    CHECK ((scope = 'space') = (scope_id = '')),
    CHECK (key <> 'run' OR (value IN ('stopped', 'paused') AND scope <> 'pool')),
    CHECK (key <> 'paid' OR (value IN ('never', 'ask') AND scope IN ('space', 'pool')))
);

CREATE TABLE ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    record      TEXT NOT NULL,
    row_sha256  TEXT NOT NULL,
    prev_sha256 TEXT NOT NULL,
    sha256      TEXT NOT NULL
);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: an entry is never changed');
END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: an entry is never removed');
END;

CREATE TABLE goals (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL REFERENCES chats (id),
    cites        TEXT NOT NULL,
    title        TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('active', 'paused', 'stalled', 'done', 'failed', 'cancelled',
                                                'expired')),
    state_reason TEXT,
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX goals_by_chat ON goals (chat_id, state);

CREATE TABLE tasks (
    id           TEXT PRIMARY KEY,
    goal_id      TEXT REFERENCES goals (id),
    chat_id      TEXT NOT NULL,
    thread_ts    TEXT NOT NULL,
    title        TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('open', 'done', 'failed', 'stopped', 'cancelled')),
    last_report  TEXT,
    reported_at  TEXT,
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (chat_id, thread_ts) REFERENCES threads (chat_id, ts)
);
CREATE UNIQUE INDEX tasks_one_per_thread ON tasks (chat_id, thread_ts);

CREATE TABLE results (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks (id),
    submitted_by TEXT NOT NULL CHECK (submitted_by GLOB 'cse_*'),
    agent        TEXT NOT NULL,
    model        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    artifacts    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifacts)),
    cost_usd     REAL,
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);
CREATE INDEX results_by_task ON results (task_id, created_at);

CREATE TABLE timers (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('at', 'every', 'on')),
    chat_id      TEXT NOT NULL REFERENCES chats (id),
    role         TEXT NOT NULL CHECK (role IN ('workspace-coordinator', 'project-coordinator', 'thread')),
    thread_ts    TEXT,
    due_at       TEXT,
    every_s      INTEGER CHECK (every_s IS NULL OR every_s > 0),
    event        TEXT,
    note         TEXT NOT NULL,
    job          TEXT CHECK (job IS NULL OR json_valid(job)),
    goal_id      TEXT REFERENCES goals (id),
    task_id      TEXT REFERENCES tasks (id),
    state        TEXT NOT NULL CHECK (state IN ('armed', 'fired', 'cancelled', 'expired')),
    state_reason TEXT,
    fired        TEXT CHECK (fired IS NULL OR json_valid(fired)),
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    approved_by  TEXT,
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK ((kind = 'on') = (event IS NOT NULL)),
    CHECK ((kind = 'every') = (every_s IS NOT NULL)),
    CHECK ((job IS NOT NULL) = (approved_by IS NOT NULL)),
    CHECK (job IS NOT NULL OR (kind <> 'every' AND (kind = 'on' OR due_at IS NOT NULL))),
    CHECK (CASE WHEN job IS NOT NULL THEN role = 'thread' AND thread_ts IS NULL
                ELSE (role = 'thread') = (thread_ts IS NOT NULL) END)
);
CREATE INDEX timers_due ON timers (due_at) WHERE state = 'armed';
CREATE INDEX timers_by_place ON timers (chat_id, role, thread_ts, state);
CREATE INDEX timers_on_event ON timers (event) WHERE state = 'armed';

CREATE TABLE leases (
    id                 TEXT PRIMARY KEY,
    resource           TEXT NOT NULL,
    holder_session     TEXT NOT NULL REFERENCES sessions (id),
    holder_pid         INTEGER,
    holder_start_ticks INTEGER,
    task_id            TEXT REFERENCES tasks (id),
    acquired_at        TEXT NOT NULL,
    expires_at         TEXT,
    released_at        TEXT,
    released_why       TEXT,
    CHECK ((holder_pid IS NULL) = (holder_start_ticks IS NULL)),
    CHECK ((released_at IS NULL) = (released_why IS NULL))
);
CREATE UNIQUE INDEX leases_one_holder ON leases (resource) WHERE released_at IS NULL;
CREATE INDEX leases_by_holder ON leases (holder_session) WHERE released_at IS NULL;

-- A pool is computed from the space advisory's facts, the founder's intent and the clock:
-- the engine's own copy of its windows and the old "continue until" go.
ALTER TABLE pools DROP COLUMN snapshot;
ALTER TABLE pools DROP COLUMN paid_continue_until;
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
    """Run the steps on the store, check the result (``verify``: None when
    as expected, else what differs; then the store's own checks), and set
    the new ``user_version``, all in one transaction: a step that fails on
    the store's own rows, or anything a check finds, leaves the file as it
    was. (``executescript`` commits what was open first; the connection is
    in autocommit mode, so nothing is, and the script leaves its own
    ``BEGIN`` open for the checks.)"""
    body = "\n".join(steps_from(version, target))
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{body}\n")
        if verify is not None and (why := verify(conn)):
            raise MigrationMismatch(why)
        if why := _findings(conn):
            raise MigrationMismatch(why)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:         # a step or a check failed: the store stays as it was
            conn.execute("ROLLBACK")
        raise


def _findings(conn: sqlite3.Connection) -> str | None:
    """What ``PRAGMA foreign_key_check`` and ``PRAGMA quick_check`` find in
    the store as ``conn`` sees it (the open transaction's changes included),
    in words; None when both find nothing. ``quick_check`` is the integrity
    check less its index-to-table cross-check: one pass over the file, and
    it runs only when a migration does."""
    broken: dict[tuple[str, str], int] = {}
    for table, _rowid, parent, _key in conn.execute("PRAGMA foreign_key_check"):
        broken[(table, parent)] = broken.get((table, parent), 0) + 1
    if broken:
        return "rows point at rows that aren't there: " + "; ".join(
            f"{count} in {table} (to {parent})" for (table, parent), count in sorted(broken.items()))
    problems = [row[0] for row in conn.execute("PRAGMA quick_check(5)")]     # the first five are enough to act on
    if problems != ["ok"]:
        return "SQLite's integrity check found: " + "; ".join(problems)
    return None
