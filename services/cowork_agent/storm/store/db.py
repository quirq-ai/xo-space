"""Storm's store: one SQLite file, one writer, one global sequence (ch 08).

Every state change is one transaction: ``Store.write(fn)`` runs ``fn(tx)``
inside ``BEGIN IMMEDIATE`` … ``COMMIT``, where ``fn`` updates projection rows
and appends ``events`` rows (``tx.append``). The events go to the listeners
only after the commit, so nothing outside the store ever sees a change that
could still roll back. ``events.seq`` is the order: a client that reconnects
asks for everything after the last seq it saw.

The connection lives on one thread of its own, and every read and write runs
there, in order. The event loop never blocks on SQLite, and the store has
exactly one owner of its connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar

from services.cowork_agent.storm.redact import redact_data
from services.cowork_agent.storm.store import migrations
from services.errors import ServiceError
from services.storage.layout import engine_dir
from services.timestamps import now_iso

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
#: How long a reader in another process may hold the writer back.
BUSY_TIMEOUT_MS = 5000
#: What the write-ahead log keeps of its file once a checkpoint has copied
#: it into the store: nothing. Its old frames hold rows as they were before
#: a scrub or a prune (a secret as typed, §0.112), and a log that is only
#: reset keeps them until later writes happen to cover them.
WAL_KEPT_BYTES = 0

T = TypeVar("T")


class StoreRefused(ServiceError):
    """The file exists but isn't a store this code may open."""

    def __init__(self, message: str) -> None:
        super().__init__("store_refused", message, status=503)


class StoreFailed(ServiceError):
    """SQLite failed a write or read (the disk is full, the file is locked
    by another process, the file is damaged)."""

    def __init__(self, message: str) -> None:
        super().__init__("store_failed", message, status=503)


def store_path() -> Path:
    return engine_dir() / "xo.db"


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    ts: str
    type: str
    data: Mapping[str, Any]
    chat_id: str | None = None
    session_id: str | None = None
    thread_ts: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    epoch: int | None = None


#: The events table's columns in ``StoredEvent`` order, for readers that build their own query.
EVENT_COLUMNS = "seq, ts, type, data, chat_id, session_id, thread_ts, turn_id, run_id, epoch"


def event_from_row(row: tuple) -> StoredEvent:
    seq, ts, type_, data, *rest = row
    return StoredEvent(seq, ts, type_, json.loads(data), *rest)


class Tx:
    """One open transaction. Only valid inside the function given to
    ``Store.write``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.events: list[StoredEvent] = []
        self.erases = False

    def erasing(self) -> None:
        """This transaction overwrites or deletes what the store may no
        longer keep (§0.112): once it commits, the write-ahead log is
        emptied too, so no earlier frame of it keeps the old text."""
        self.erases = True

    def execute(self, sql: str, params: Mapping[str, Any] | tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def append(self, type: str, data: Mapping[str, Any] | None = None, *, chat_id: str | None = None,
               session_id: str | None = None, thread_ts: str | None = None, turn_id: str | None = None,
               run_id: str | None = None, epoch: int | None = None) -> int:
        """Append one event to the state log; returns its seq. Its data is
        kept redacted (ch 16 §3), and its listeners hear it so."""
        event = StoredEvent(seq=0, ts=now_iso(), type=type, data=redact_data(dict(data or {})), chat_id=chat_id,
                            session_id=session_id, thread_ts=thread_ts, turn_id=turn_id, run_id=run_id,
                            epoch=epoch)
        cur = self.conn.execute(
            "INSERT INTO events (ts, type, data, chat_id, session_id, thread_ts, turn_id, run_id, epoch)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.ts, type, json.dumps(event.data), chat_id, session_id, thread_ts, turn_id, run_id, epoch))
        seq = cur.lastrowid
        self.events.append(StoredEvent(seq, event.ts, type, event.data, chat_id, session_id, thread_ts,
                                       turn_id, run_id, epoch))
        return seq


class Store:
    """Open with ``await Store.open(path)``; close with ``await store.close()``."""

    def __init__(self, path: Path, conn: sqlite3.Connection, thread: ThreadPoolExecutor) -> None:
        self.path = path
        self._conn = conn
        self._thread = thread
        self._listeners: list[Callable[[list[StoredEvent]], None]] = []
        self._closed = False

    @classmethod
    async def open(cls, path: Path | None = None) -> "Store":
        """Open the store at ``path`` (default ``~/.quirq/engine/xo.db``),
        creating the schema in an empty file and migrating an older store in
        place (``migrations.py``). Refuses a file with a newer schema, or one
        that isn't a storm store; never rewrites either."""
        path = path or store_path()
        thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storm-store")
        try:
            conn = await asyncio.get_running_loop().run_in_executor(thread, _connect, path)
        except BaseException as exc:
            thread.shutdown(wait=False)
            if isinstance(exc, sqlite3.Error) and (failure := _store_failure(exc, path)):
                raise failure from exc
            raise
        return cls(path, conn, thread)

    def on_commit(self, listener: Callable[[list[StoredEvent]], None]) -> None:
        """Call ``listener`` with each committed transaction's events, in seq
        order, on the event loop."""
        self._listeners.append(listener)

    async def write(self, fn: Callable[[Tx], T]) -> T:
        """Run ``fn`` as one transaction. If it raises, nothing is kept and
        the exception travels on.

        The store's thread hands each commit's events to the listeners as it
        commits, so the listeners hear every change exactly in seq order,
        before the writer resumes, and even when the writer was cancelled
        mid-write (a client went away): the store never keeps a change it
        doesn't announce. A cancelled writer can't tell whether its change
        was kept; it reads the store to find out."""
        loop = asyncio.get_running_loop()
        published = loop.create_future()
        result = await self._run(self._transaction, fn, loop, published)
        await published
        return result

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn`` against one consistent snapshot of the store."""
        return await self._run(self._snapshot, fn)

    async def head(self) -> int:
        """The latest event's seq (0 for an empty store): where a live stream
        starts when it replays nothing."""
        return await self.read(lambda conn: conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])

    async def events_after(self, seq: int, *, limit: int = 500) -> list[StoredEvent]:
        def query(conn: sqlite3.Connection) -> list[StoredEvent]:
            rows = conn.execute(f"SELECT {EVENT_COLUMNS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                                (seq, limit)).fetchall()
            return [event_from_row(r) for r in rows]
        return await self.read(query)

    async def empty_wal(self) -> None:
        """Copy the write-ahead log into the store and empty its file (the
        daily prune, after deleting rows)."""
        await self._run(self._empty_wal)

    async def close(self) -> None:
        """Idempotent. Work queued before the close still runs; anything
        asked of the store afterwards is refused with `StoreFailed`."""
        if self._closed:
            return
        self._closed = True
        await asyncio.get_running_loop().run_in_executor(self._thread, self._conn.close)
        self._thread.shutdown(wait=False)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        if self._closed:
            raise StoreFailed(f"The store at {self.path} is closed (the server is stopping); retry once it is up.")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._thread, fn, *args)
        except sqlite3.Error as exc:
            if failure := _store_failure(exc, self.path):
                raise failure from exc
            raise

    # The two below run on the store's thread only.

    def _transaction(self, fn: Callable[[Tx], T], loop: asyncio.AbstractEventLoop, published: asyncio.Future) -> T:
        tx = Tx(self._conn)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(tx)
            self._conn.execute("COMMIT")
        except BaseException:
            # Some failures (a full disk at COMMIT) already rolled back.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        loop.call_soon_threadsafe(self._publish, tx.events, published)
        if tx.erases:
            self._empty_wal()
        return result

    def _empty_wal(self) -> None:
        """A ``TRUNCATE`` checkpoint (about 2 ms, whatever the store's size).
        The change is committed whatever happens here: a failed or blocked
        checkpoint is logged, and the next one empties the log. It never waits
        for another process's reader (a backup, a ``sqlite3`` shell): every
        write of the store would wait behind it."""
        try:
            self._conn.execute("PRAGMA busy_timeout=0")
            try:
                busy, _frames, _copied = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except sqlite3.Error as exc:
            log.warning("storm store: the write-ahead log of %s couldn't be emptied (%s); the next scrub or prune "
                        "empties it", self.path, exc)
            return
        if busy:
            log.warning("storm store: another process reads %s, so its write-ahead log couldn't be emptied; the "
                        "next scrub or prune empties it", self.path)

    def _publish(self, events: list[StoredEvent], published: asyncio.Future) -> None:
        """On the event loop, in commit order; then the writer resumes."""
        for listener in self._listeners if events else ():
            try:
                listener(events)
            except Exception:  # noqa: BLE001 - a listener can't undo a commit
                log.exception("storm store: a commit listener failed after seq %s", events[-1].seq)
        if not published.done():        # done: the writer was cancelled
            published.set_result(None)

    def _snapshot(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        self._conn.execute("BEGIN")
        try:
            return fn(self._conn)
        finally:
            self._conn.execute("COMMIT")


#: What SQLite says when the file's tables aren't the ones this build's SQL
#: expects (a store made by an earlier build).
_SCHEMA_WORDS = ("no such table", "no such column", "has no column named", "columns but")
#: SQLite's names (and words, for a raised error without them) for a failing
#: disk or file: only these get the free-space advice.
_DISK_NAMES = ("SQLITE_FULL", "SQLITE_IOERR", "SQLITE_CANTOPEN", "SQLITE_READONLY")
_DISK_WORDS = ("disk is full", "disk i/o error", "unable to open", "readonly database")
_BUSY_NAMES = ("SQLITE_BUSY", "SQLITE_LOCKED")
_BUSY_WORDS = ("database is locked", "database table is locked")


def _store_failure(exc: sqlite3.Error, path: Path) -> StoreFailed | None:
    """The store's own failures as `StoreFailed`, each with what to do; None
    for the rest (a constraint a change broke), which are the caller's. A
    store whose tables aren't this build's is refused at open
    (`StoreRefused`); met later, it is a `StoreFailed` that says the same, so
    the engine's handling of a failed write applies."""
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        name = getattr(exc, "sqlite_errorname", None) or ""
        if any(word in text for word in _SCHEMA_WORDS):
            # The store passed the full structure check at open, so this is
            # this build's code asking for what its schema doesn't make.
            return StoreFailed(f"The store at {path} lacks what this build's code asked for ({exc}). Nothing in "
                               "it was changed. Keep the file and report this.")
        if name.startswith(_DISK_NAMES) or any(word in text for word in _DISK_WORDS):
            return StoreFailed(f"The store at {path} could not be used: {exc}. If the disk is full, free space and "
                               "retry.")
        if name.startswith(_BUSY_NAMES) or any(word in text for word in _BUSY_WORDS):
            return StoreFailed(f"The store at {path} is held by another process ({exc}); retry in a moment.")
        return StoreFailed(f"The store at {path} could not be used: {exc}.")
    if type(exc) is sqlite3.DatabaseError:      # SQLite's own word for a damaged or foreign file
        return StoreFailed(f"The store at {path} is damaged ({exc}). Stop XO Space and move the file aside to "
                           "keep it for inspection; XO Space then starts a new, empty store.")
    return None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
    try:
        # Refuse before anything else: even switching the journal mode
        # rewrites the file's header, and a refused file stays as it is.
        older = _refuse_unknown(conn, path)
        os.chmod(path, 0o600)
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise StoreRefused(f"{path} can't use write-ahead logging (is it on a network disk?).")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # What the scrub and the prune delete (a secret as typed, §0.112) is
        # overwritten, never left in free pages, whatever this build's default.
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute(f"PRAGMA journal_size_limit={WAL_KEPT_BYTES}")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_FILE.read_text(encoding='utf-8')}\n"
                               f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
        elif older is not None:
            try:
                migrations.migrate(conn, older, SCHEMA_VERSION, verify=_difference)
            except migrations.MigrationMismatch as exc:
                raise StoreRefused(_migration_failed_message(path, older, str(exc))) from exc
            except sqlite3.Error as exc:
                if _store_failure(exc, path) is None:
                    # A step its rows broke (a constraint, a unique index): a
                    # crash at boot otherwise. Rolled back: the store is as it was.
                    raise StoreRefused(_migration_failed_message(path, older, str(exc))) from exc
                raise
            except Exception as exc:
                # A fault of this build's (a step's or the migration's own
                # code), not the store's: rolled back and refused, never a
                # crash at boot. The trace is for whoever fixes the build.
                log.exception("storm store: migrating %s from schema %s failed", path, older)
                raise StoreRefused(_migration_failed_message(path, older, f"{type(exc).__name__}: {exc}")) from exc
            log.info("storm store: %s migrated from schema %s to %s", path, older, SCHEMA_VERSION)
    except BaseException:
        conn.close()
        raise
    return conn


def _earlier_build_message(path: Path, why: str) -> str:
    return (f"The store at {path} was made by an earlier storm build ({why}). Move it aside (nothing in it is "
            "needed before release) and restart XO Space; it then starts a new, empty store.")


def _migration_failed_message(path: Path, version: int, why: str) -> str:
    """A store this build can't bring to its schema, which holds real work:
    the advice is to keep it, never to move it aside."""
    return (f"The store at {path} (schema {version}) couldn't be brought to this build's schema {SCHEMA_VERSION}: "
            f"{why}. Nothing in it was changed. Keep the file, run the XO Space version that wrote it, and "
            "report this, so a migration that fits it can be made.")


def _refusal(conn: sqlite3.Connection, path: Path, version: int, why: str) -> str:
    """What to do with a store refused before any step ran: one that holds
    rows holds someone's work, so it is kept whatever made it; only an empty
    one may be moved aside and started afresh."""
    return _migration_failed_message(path, version, why) if _holds_rows(conn) else _earlier_build_message(path, why)


def _holds_rows(conn: sqlite3.Connection) -> bool:
    """Whether any of the file's tables has a row. A table that can't be
    read counts as holding one: the advice then errs toward keeping it."""
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return any(conn.execute(f"SELECT EXISTS (SELECT 1 FROM {_quoted(table)})").fetchone()[0] for table in tables)
    except sqlite3.Error:
        return True


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


#: SQL as SQLite reads it, one token at a time: spacing and comments (dropped),
#: a string or a quoted name (kept as written), a word or a number (case
#: folded: SQLite's keywords and names ignore case), or an operator.
_TOKEN = re.compile(
    r"(?P<skip>\s+|--[^\n]*|/\*.*?(?:\*/|\Z))"
    r"|(?P<quoted>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|\[[^\]]*\])"
    r"|(?P<word>\w+)"
    r"|(?P<op><=|>=|<>|!=|==|\|\||<<|>>|.)", re.DOTALL)


def _tokens(sql: str) -> list[str]:
    return [m.group().lower() if m.lastgroup == "word" else m.group()
            for m in _TOKEN.finditer(sql) if m.lastgroup != "skip"]


def _normalized(sql: str) -> str:
    """``sql`` spaced one way and with its comments dropped: two statements
    that say the same read the same."""
    return " ".join(_tokens(sql))


def _checks(create_table: str) -> tuple[str, ...]:
    """Each CHECK constraint's expression in a CREATE TABLE statement,
    normalised and sorted. A table's SQL is the only place SQLite keeps
    them, and ALTER TABLE rewrites it: ADD COLUMN puts the new column, and
    its CHECK, after the table's own constraints; DROP COLUMN takes the
    column's own CHECK with it. Compared as a multiset, the constraints of a
    migrated table read the same as a new one's whatever their order."""
    tokens = _tokens(create_table)
    found = []
    for start, token in enumerate(tokens):
        if token != "check" or tokens[start + 1:start + 2] != ["("]:
            continue
        depth = 0
        for end in range(start + 1, len(tokens)):
            depth += {"(": 1, ")": -1}.get(tokens[end], 0)
            if depth == 0:
                found.append(" ".join(tokens[start + 2:end]))
                break
    return tuple(sorted(found))


@dataclass(frozen=True)
class _Table:
    #: (name, type, not null, default, primary key, hidden) in order; hidden
    #: is how ``table_xinfo`` tells a generated column.
    columns: tuple[tuple, ...]
    checks: tuple[str, ...]
    #: An index made by CREATE INDEX by its SQL (its name, columns, and the
    #: WHERE of a partial one); one SQLite made for a PRIMARY KEY or UNIQUE
    #: constraint by what it covers (its name is only a position).
    indexes: tuple[str, ...]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class _Structure:
    """What a store must be, exactly as a new store of this build is: the
    tables with their columns, CHECK constraints, indexes and foreign keys,
    and every view and trigger (by its SQL)."""
    tables: Mapping[str, _Table]
    others: tuple[str, ...]


def _structure(conn: sqlite3.Connection) -> _Structure:
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    sql_of = {name: sql for _type, name, sql in rows}
    tables: dict[str, _Table] = {}
    for kind, table, sql in rows:
        if kind != "table":
            continue
        name = _quoted(table)
        indexes = []
        for _seq, index, _unique, origin, _partial in conn.execute(f"PRAGMA index_list({name})"):
            if origin == "c":
                indexes.append(_normalized(sql_of[index]))
            else:
                covers = ", ".join(str(row[2]) for row in conn.execute(f"PRAGMA index_info({_quoted(index)})"))
                indexes.append(f"{'primary key' if origin == 'pk' else 'unique'} ({covers})")
        keys: dict[int, list] = {}
        for key, _seq, parent, child, parent_column, on_update, on_delete, _match in conn.execute(
                f"PRAGMA foreign_key_list({name})"):
            keys.setdefault(key, [parent, [], [], on_update, on_delete])
            keys[key][1].append(child)
            keys[key][2].append(parent_column or "")
        tables[table] = _Table(
            columns=tuple(tuple(col[1:]) for col in conn.execute(f"PRAGMA table_xinfo({name})")),
            checks=_checks(sql),
            indexes=tuple(sorted(indexes)),
            keys=tuple(sorted(f"({', '.join(child)}) references {parent} ({', '.join(columns)}) on update "
                              f"{on_update} on delete {on_delete}"
                              for parent, child, columns, on_update, on_delete in keys.values())))
    others = tuple(sorted(_normalized(sql) for kind, _name, sql in rows if kind in ("view", "trigger")))
    return _Structure(tables, others)


_EXPECTED: _Structure | None = None


def _expected_structure() -> _Structure:
    """What this build's schema makes, built once in memory."""
    global _EXPECTED
    if _EXPECTED is None:
        mem = sqlite3.connect(":memory:")
        try:
            mem.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            _EXPECTED = _structure(mem)
        finally:
            mem.close()
    return _EXPECTED


def _structure_difference(conn: sqlite3.Connection) -> str | None:
    """None when the store's structure is exactly this build's; else the
    first thing that differs, in words."""
    found, want = _structure(conn), _expected_structure()
    if found == want:
        return None
    if missing := sorted(set(want.tables) - set(found.tables)):
        return f"it has no {', '.join(missing)} table" + ("s" if len(missing) > 1 else "")
    for table in sorted(want.tables):
        have, expect = found.tables[table], want.tables[table]
        if lacking := sorted({c[0] for c in expect.columns} - {c[0] for c in have.columns}):
            return f"its {table} table has no {', '.join(lacking)} column" + ("s" if len(lacking) > 1 else "")
        if have.columns != expect.columns:
            return f"its {table} table's columns differ from this build's"
        for part, have_part, want_part in (("CHECK constraints", have.checks, expect.checks),
                                           ("indexes", have.indexes, expect.indexes),
                                           ("foreign keys", have.keys, expect.keys)):
            if have_part != want_part:
                return f"its {table} table's {part} differ from this build's ({_versus(have_part, want_part)})"
    if extra := sorted(set(found.tables) - set(want.tables)):
        return f"it has tables this build doesn't make ({', '.join(extra)})"
    return f"its views or triggers differ from this build's ({_versus(found.others, want.others)})"


def _versus(have: Iterable[str], want: Iterable[str]) -> str:
    """What ``have`` lacks of ``want`` and has besides, each counted."""
    have_count, want_count = Counter(have), Counter(want)
    parts = []
    if lacking := sorted((want_count - have_count).elements()):
        parts.append("lacks " + "; ".join(lacking))
    if extra := sorted((have_count - want_count).elements()):
        parts.append("has " + "; ".join(extra))
    return ", and ".join(parts)


def _difference(conn: sqlite3.Connection) -> str | None:
    """``_structure_difference``, with a fault in the comparison itself (this
    build's, not SQLite's) said as a difference: the store is refused, never
    a crash at boot."""
    try:
        return _structure_difference(conn)
    except sqlite3.Error:
        raise
    except Exception as exc:
        log.exception("storm store: a store's structure couldn't be compared with this build's")
        return f"its structure couldn't be compared with this build's ({type(exc).__name__}: {exc})"


def _refuse_unknown(conn: sqlite3.Connection, path: Path) -> int | None:
    """Raise unless the file is empty, a store of exactly this build's
    schema, or an older store the migrations bring to it whole (rehearsed in
    memory, so a refused file is never touched). Returns that older version,
    else None."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    if version == 0 and tables == 0:
        return None
    if version > SCHEMA_VERSION:
        raise StoreRefused(
            f"{path} was written by a newer XO Space (store schema {version}; this one knows "
            f"{SCHEMA_VERSION}). Update XO Space, or move the file aside to start an empty store.")
    if version < 1:
        raise StoreRefused(
            f"{path} isn't a store this XO Space knows (schema {version}). Move it aside to start an "
            "empty store.")
    if version == SCHEMA_VERSION:
        if why := _difference(conn):
            raise StoreRefused(_refusal(conn, path, version, why))
        return None
    try:
        rehearsal = migrations.rehearse(conn, version, SCHEMA_VERSION)
        try:
            why = _difference(rehearsal)
        finally:
            rehearsal.close()
    except Exception as exc:        # a step that doesn't apply, a missing step, or a fault in either
        raise StoreRefused(_refusal(conn, path, version, f"schema {version} can't be migrated: {exc}")) from exc
    if why:
        raise StoreRefused(_refusal(conn, path, version, f"schema {version}, migrated, {why}"))
    return version
