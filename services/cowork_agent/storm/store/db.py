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
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from services.errors import ServiceError
from services.storage.layout import engine_dir
from services.timestamps import now_iso

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
#: How long a reader in another process may hold the writer back.
BUSY_TIMEOUT_MS = 5000

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


_EVENT_COLUMNS = "seq, ts, type, data, chat_id, session_id, thread_ts, turn_id, run_id, epoch"


def _event_from_row(row: tuple) -> StoredEvent:
    seq, ts, type_, data, *rest = row
    return StoredEvent(seq, ts, type_, json.loads(data), *rest)


class Tx:
    """One open transaction. Only valid inside the function given to
    ``Store.write``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.events: list[StoredEvent] = []

    def execute(self, sql: str, params: Mapping[str, Any] | tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def append(self, type: str, data: Mapping[str, Any] | None = None, *, chat_id: str | None = None,
               session_id: str | None = None, thread_ts: str | None = None, turn_id: str | None = None,
               run_id: str | None = None, epoch: int | None = None) -> int:
        """Append one event to the state log; returns its seq."""
        event = StoredEvent(seq=0, ts=now_iso(), type=type, data=dict(data or {}), chat_id=chat_id,
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

    def __init__(self, path: Path, conn: sqlite3.Connection, thread: ThreadPoolExecutor,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.path = path
        self._loop = loop
        self._conn = conn
        self._thread = thread
        self._listeners: list[Callable[[list[StoredEvent]], None]] = []
        self._closed = False

    @classmethod
    async def open(cls, path: Path | None = None) -> "Store":
        """Open the store at ``path`` (default ``~/.quirq/engine/xo.db``),
        creating schema v1 in an empty file. Refuses a file with a newer
        schema, or one that isn't a storm store; never rewrites either."""
        path = path or store_path()
        thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storm-store")
        try:
            conn = await asyncio.get_running_loop().run_in_executor(thread, _connect, path)
        except BaseException as exc:
            thread.shutdown(wait=False)
            if isinstance(exc, sqlite3.Error) and (failure := _store_failure(exc, path)):
                raise failure from exc
            raise
        return cls(path, conn, thread, asyncio.get_running_loop())

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
        published = self._loop.create_future()
        result = await self._run(self._transaction, fn, published)
        await published
        return result

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn`` against one consistent snapshot of the store."""
        return await self._run(self._snapshot, fn)

    async def events_after(self, seq: int, *, limit: int = 500) -> list[StoredEvent]:
        def query(conn: sqlite3.Connection) -> list[StoredEvent]:
            rows = conn.execute(f"SELECT {_EVENT_COLUMNS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                                (seq, limit)).fetchall()
            return [_event_from_row(r) for r in rows]
        return await self.read(query)

    async def close(self) -> None:
        """Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._run(self._conn.close)
        self._thread.shutdown(wait=True)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._thread, fn, *args)
        except sqlite3.Error as exc:
            if failure := _store_failure(exc, self.path):
                raise failure from exc
            raise

    # The two below run on the store's thread only.

    def _transaction(self, fn: Callable[[Tx], T], published: asyncio.Future) -> T:
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
        self._loop.call_soon_threadsafe(self._publish, tx.events, published)
        return result

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


def _store_failure(exc: sqlite3.Error, path: Path) -> StoreFailed | None:
    """The store's own failures as `StoreFailed`; None for the rest
    (a constraint a change broke, a bad statement), which are the caller's."""
    if isinstance(exc, sqlite3.OperationalError):
        return StoreFailed(f"The store at {path} could not be used: {exc}. If the disk is full, free space and retry.")
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
        _refuse_unknown(conn, path)
        os.chmod(path, 0o600)
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise StoreRefused(f"{path} can't use write-ahead logging (is it on a network disk?).")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_FILE.read_text(encoding='utf-8')}\n"
                               f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
    except BaseException:
        conn.close()
        raise
    return conn


def _refuse_unknown(conn: sqlite3.Connection, path: Path) -> None:
    """Raise unless the file is empty or a store of this schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    if version == 0 and tables == 0:
        return
    if version > SCHEMA_VERSION:
        raise StoreRefused(
            f"{path} was written by a newer XO Space (store schema {version}; this one knows "
            f"{SCHEMA_VERSION}). Update XO Space, or move the file aside to start an empty store.")
    elif version != SCHEMA_VERSION:
        raise StoreRefused(
            f"{path} isn't a store this XO Space knows (schema {version}). Move it aside to start an "
            "empty store.")
