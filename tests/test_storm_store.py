"""Storm's store, the engine lock and boot recovery (design ch 08; ch 07 §9).

The proofs ch 18 §2 asks of this piece: schema v1 comes from the text sample
(``tests/fixtures/quirq-state/engine/xo.db.sql``) and matches the code's; a
store with a newer schema is refused and left as it is; boot recovery hands
taken wakes back. Around them: one transaction per change with events
published only after the commit, a seq that is never reused, the lock failing
closed, and recovery stopping only the processes the registry names.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.storm.control import marker
from services.cowork_agent.storm.control.marker import Mark
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.records import intents
from services.cowork_agent.storm.store import (
    MAX_REDELIVERIES,
    RESUME_NOTE,
    SCHEMA_VERSION,
    EngineElsewhere,
    EngineLock,
    Store,
    StoreFailed,
    StoreRefused,
    dropped_message,
    hand_back,
    recover,
    scrub,
)
from services.cowork_agent.storm.store import db
from services.cowork_agent.storm.store.db import SCHEMA_FILE
from services.timestamps import now_iso
from utils.commands import process_start_ticks, run_sync, spawn_stream

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "quirq-state" / "engine" / "xo.db.sql"
#: A store as the first storm build wrote it (schema 1), for the migration.
V1_SAMPLE = Path(__file__).resolve().parent / "fixtures" / "storm_store_v1.sql"


def load_sample(path: Path, sample: Path = SAMPLE) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(sample.read_text(encoding="utf-8"))
    conn.close()


def columns_of(path: Path) -> dict[str, list[tuple]]:
    """Each table's columns as PRAGMA table_info gives them (cid dropped)."""
    conn = sqlite3.connect(path)
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                        "'sqlite_%' ORDER BY name")]
    shape = {n: [tuple(c[1:]) for c in conn.execute(f'PRAGMA table_info("{n}")')] for n in names}
    conn.close()
    return shape


def schema_of(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    conn.close()
    return rows


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Temp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "engine" / "xo.db"

    async def open(self) -> Store:
        store = await Store.open(self.db)
        self.addAsyncCleanup(store.close)
        return store


class SchemaTests(_Temp):
    async def test_a_new_store_is_this_schema_in_wal_with_foreign_keys(self) -> None:
        store = await self.open()
        version, journal, fks = await store.read(lambda c: (
            c.execute("PRAGMA user_version").fetchone()[0], c.execute("PRAGMA journal_mode").fetchone()[0],
            c.execute("PRAGMA foreign_keys").fetchone()[0]))
        self.assertEqual((version, journal, fks), (SCHEMA_VERSION, "wal", 1))
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    async def test_deleted_bytes_are_overwritten_whatever_the_builds_default(self) -> None:
        """The scrub (§0.112) must not leave a secret in free pages: on a
        SQLite built with secure_delete off, the store turns it on."""
        connect = sqlite3.connect

        def off_by_default(*args, **kwargs) -> sqlite3.Connection:
            conn = connect(*args, **kwargs)
            conn.execute("PRAGMA secure_delete=OFF")
            return conn
        with patch.object(db.sqlite3, "connect", off_by_default):
            store = await self.open()
        self.assertEqual(await store.read(lambda c: c.execute("PRAGMA secure_delete").fetchone()[0]), 1)

    async def test_the_sample_is_this_schema_exactly_and_opens(self) -> None:
        await (await self.open()).close()
        sample = self.dir / "sample.db"
        load_sample(sample)
        self.assertEqual(schema_of(sample), schema_of(self.db),
                         f"{SAMPLE.name} and {SCHEMA_FILE.name} disagree: copy the schema into the sample")
        store = await Store.open(sample)
        self.addAsyncCleanup(store.close)
        self.assertEqual(await store.read(lambda c: c.execute("SELECT count(*) FROM sessions").fetchone()[0]), 3)

    async def test_a_newer_schema_is_refused_and_left_untouched(self) -> None:
        self.db.parent.mkdir()
        conn = sqlite3.connect(self.db)
        conn.executescript(f"CREATE TABLE future (x); PRAGMA user_version = {SCHEMA_VERSION + 1};")
        conn.close()
        before = digest(self.db)
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("newer XO Space", caught.exception.message)
        self.assertEqual((caught.exception.code, caught.exception.status), ("store_refused", 503))
        self.assertEqual(digest(self.db), before)

    async def test_a_v1_store_is_migrated_in_place_keeping_every_row(self) -> None:
        """A store the first build wrote opens: the new tables and columns are
        added in one transaction, and every row it had is kept as it was."""
        await (await self.open()).close()              # this build's own shape, for comparison
        self.db.parent.mkdir(exist_ok=True)
        old = self.dir / "old.db"
        load_sample(old, V1_SAMPLE)
        before = sqlite3.connect(old)
        threads = before.execute("SELECT chat_id, ts, title, branch FROM threads ORDER BY ts").fetchall()
        events = before.execute("SELECT count(*), max(seq) FROM events").fetchone()
        before.close()
        store = await Store.open(old)
        self.addAsyncCleanup(store.close)
        version, now_threads, now_events, summaries = await store.read(lambda c: (
            c.execute("PRAGMA user_version").fetchone()[0],
            c.execute("SELECT chat_id, ts, title, branch FROM threads ORDER BY ts").fetchall(),
            c.execute("SELECT count(*), max(seq) FROM events").fetchone(),
            c.execute("SELECT summary, summary_at FROM threads").fetchall()))
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual((now_threads, now_events), (threads, events))
        self.assertTrue(threads and all(s == (None, None) for s in summaries))
        self.assertEqual(columns_of(old), columns_of(self.db))
        # And it writes: a new table and a new column take rows.
        await store.write(lambda tx: tx.execute(
            "INSERT INTO chat_seen (chat_id, seen_at, seen_seq) SELECT id, '2026-11-03T00:00:00Z', 1 FROM chats"))
        await store.close()
        reopened = await Store.open(old)                # migrated once: now it is this build's store
        self.addAsyncCleanup(reopened.close)

    async def test_an_older_store_a_migration_doesnt_fit_is_refused_untouched(self) -> None:
        """Refused before any step runs, and, since it holds rows, kept:
        never "move it aside, nothing in it is needed"."""
        self.db.parent.mkdir()
        load_sample(self.db, V1_SAMPLE)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE made (x)")           # what step 2 adds is there already, otherwise
        conn.commit()
        conn.close()
        before = digest(self.db)
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("schema 1 can't be migrated", caught.exception.message)
        self.assertIn("Keep the file", caught.exception.message)
        self.assertNotIn("Move it aside", caught.exception.message)
        self.assertEqual(digest(self.db), before)

    async def test_a_failed_migration_leaves_the_store_as_it_was(self) -> None:
        self.db.parent.mkdir()
        load_sample(self.db, V1_SAMPLE)
        before = columns_of(self.db)
        broken = {**db.migrations.STEPS, 2: db.migrations.STEPS[2] + "\nCREATE TABLE threads (x);"}
        with patch.object(db.migrations, "STEPS", broken), \
                patch.object(db, "_refuse_unknown", lambda conn, path: 1):
            with self.assertRaises(StoreFailed):
                await Store.open(self.db)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        conn.close()
        self.assertEqual(columns_of(self.db), before)

    async def test_the_v1_sample_migrates_to_exactly_this_builds_structure(self) -> None:
        """Tables, columns, indexes and foreign keys: a migrated store is a new
        store of this build, not just one with the same columns."""
        await (await self.open()).close()
        old = self.dir / "old.db"
        load_sample(old, V1_SAMPLE)
        await (await Store.open(old)).close()
        fresh, migrated = sqlite3.connect(self.db), sqlite3.connect(old)
        self.addCleanup(fresh.close)
        self.addCleanup(migrated.close)
        self.assertEqual(db._structure(migrated), db._structure(fresh))

    async def test_a_step_its_rows_break_is_refused_and_the_store_kept_as_it_was(self) -> None:
        """The rehearsal has no rows; a step that fails only on a real store's
        data (here a unique index its events break) is refused, never a crash
        at boot, and every row stays."""
        self.db.parent.mkdir()
        load_sample(self.db, V1_SAMPLE)
        conn = sqlite3.connect(self.db)
        repeated = conn.execute("SELECT type FROM events GROUP BY type HAVING count(*) > 1").fetchall()
        rows = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        conn.close()
        self.assertTrue(repeated, "the sample needs two events of one type for this test")
        before = columns_of(self.db)
        breaks = {**db.migrations.STEPS, 2: db.migrations.STEPS[2] + "\nCREATE UNIQUE INDEX one_per_type ON events (type);"}
        with patch.object(db.migrations, "STEPS", breaks):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("Nothing in it was changed", caught.exception.message)
        self.assertNotIn("Move it aside", caught.exception.message)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], rows)
        self.assertEqual(columns_of(self.db), before)

    async def test_a_migration_that_leaves_an_index_out_is_rolled_back(self) -> None:
        """Columns alike isn't enough: a step that forgets an index this
        build's schema has is refused and rolled back."""
        self.db.parent.mkdir()
        load_sample(self.db, V1_SAMPLE)
        before = columns_of(self.db)
        step = db.migrations.STEPS[2].replace("CREATE INDEX made_by_chat ON made (chat_id, updated_at);", "")
        self.assertNotEqual(step, db.migrations.STEPS[2])
        with patch.object(db.migrations, "STEPS", {**db.migrations.STEPS, 2: step}), \
                patch.object(db, "_refuse_unknown", lambda conn, path: 1):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("indexes differ", caught.exception.message)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(columns_of(self.db), before)

    async def test_a_database_that_is_not_a_store_is_refused(self) -> None:
        self.db.parent.mkdir()
        conn = sqlite3.connect(self.db)
        conn.executescript("CREATE TABLE notes (x);")
        conn.close()
        with self.assertRaises(StoreRefused):
            await Store.open(self.db)

    async def test_a_store_an_earlier_build_made_is_refused_with_what_to_do(self) -> None:
        """Same schema version, other tables: refused at open with the move-it-
        aside advice, never "free space", and the file left as it was."""
        for change, why in (("ALTER TABLE sessions DROP COLUMN paused", "its sessions table has no paused column"),
                            ("DROP TABLE grants", "it has no grants table")):
            with self.subTest(change=change):
                if self.db.exists():
                    self.db.unlink()
                store = await Store.open(self.db)
                await store.close()
                conn = sqlite3.connect(self.db)
                conn.execute(change)
                conn.commit()
                conn.close()
                before = digest(self.db)
                with self.assertRaises(StoreRefused) as caught:
                    await Store.open(self.db)
                message = caught.exception.message
                self.assertIn("made by an earlier storm build", message)
                self.assertIn(why, message)
                self.assertIn("Move it aside", message)
                self.assertNotIn("free space", message)
                self.assertEqual((caught.exception.code, caught.exception.status), ("store_refused", 503))
                self.assertEqual(digest(self.db), before)

    async def test_a_damaged_file_is_an_actionable_error_and_left_untouched(self) -> None:
        self.db.parent.mkdir()
        self.db.write_bytes(b"not a database " * 300)
        before = digest(self.db)
        with self.assertRaises(StoreFailed) as caught:
            await Store.open(self.db)
        self.assertIn("move the file aside", caught.exception.message)
        self.assertEqual((caught.exception.code, caught.exception.status), ("store_failed", 503))
        self.assertEqual(digest(self.db), before)

    async def test_constraints_hold(self) -> None:
        store = await self.open()

        def bad_wake_state(tx):
            tx.execute("INSERT INTO sessions (id, role, preset, agent, spec, status, created_at, updated_at) "
                       "VALUES ('cse_1', 'plain', 'plain_chat', 'a', '{}', 'cold', 't', 't')")
            tx.execute("INSERT INTO wakes (id, session_id, priority, reason, state, created_at) "
                       "VALUES ('w', 'cse_1', 'now', 'r', 'bogus', 't')")

        def orphan_turn(tx):
            tx.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) "
                       "VALUES ('turn_1', 'cse_missing', 0, 'person', 'running', 't')")

        def bad_json(tx):
            tx.append("x", None)
            tx.execute("UPDATE events SET data = 'not json'")

        for change in (bad_wake_state, orphan_turn, bad_json):
            with self.subTest(change.__name__), self.assertRaises(sqlite3.IntegrityError):
                await store.write(change)


class TransactionTests(_Temp):
    async def test_events_get_seqs_and_reach_listeners_after_commit(self) -> None:
        store = await self.open()
        heard = []
        store.on_commit(lambda events: heard.append([(e.seq, e.type) for e in events]))
        await store.write(lambda tx: (tx.append("a"), tx.append("b", {"k": 1}, session_id="cse_1")))
        await store.write(lambda tx: tx.append("c"))
        self.assertEqual(heard, [[(1, "a"), (2, "b")], [(3, "c")]])
        after = await store.events_after(1)
        self.assertEqual([(e.seq, e.type, e.data, e.session_id) for e in after],
                         [(2, "b", {"k": 1}, "cse_1"), (3, "c", {}, None)])
        self.assertEqual([e.seq for e in await store.events_after(0, limit=2)], [1, 2])

    async def test_a_failed_change_keeps_nothing_and_publishes_nothing(self) -> None:
        store = await self.open()
        heard = []
        store.on_commit(heard.append)

        def half_done(tx):
            tx.append("a")
            raise ValueError("stopped halfway")

        with self.assertRaises(ValueError):
            await store.write(half_done)
        self.assertEqual((heard, await store.events_after(0)), ([], []))
        self.assertEqual(await store.write(lambda tx: tx.append("b")), 1)

    async def test_a_seq_is_never_reused(self) -> None:
        store = await self.open()
        await store.write(lambda tx: (tx.append("a"), tx.append("b")))
        await store.write(lambda tx: tx.execute("DELETE FROM events"))
        self.assertEqual(await store.write(lambda tx: tx.append("c")), 3)

    async def test_concurrent_changes_never_interleave(self) -> None:
        store = await self.open()
        heard = []
        store.on_commit(lambda events: heard.append([(e.seq, e.data["n"]) for e in events]))
        await asyncio.gather(*(store.write(lambda tx, n=n: [tx.append("x", {"n": n}) for _ in range(3)])
                               for n in range(50)))
        # Each change is heard whole, and listeners hear the changes in seq order.
        self.assertEqual(sorted(len({n for _, n in batch}) for batch in heard), [1] * 50)
        self.assertEqual([seq for batch in heard for seq, _ in batch], list(range(1, 151)))
        seqs = [e.seq for e in await store.events_after(0, limit=500)]
        self.assertEqual(seqs, list(range(1, 151)))

    async def test_a_cancelled_write_is_still_committed_and_announced(self) -> None:
        # The caller went away while SQLite was working: the change must not
        # be kept without the live stream hearing of it.
        store = await self.open()
        heard = []
        store.on_commit(lambda events: heard.extend(e.seq for e in events))
        working = threading.Event()

        def slow(tx):
            tx.append("a")
            working.set()
            time.sleep(0.2)

        task = asyncio.create_task(store.write(slow))
        self.assertTrue(await asyncio.to_thread(working.wait, 5))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await store.write(lambda tx: tx.append("b"))
        self.assertEqual(heard, [1, 2])

    async def test_a_store_closed_refuses_with_a_typed_error(self) -> None:
        store = await Store.open(self.db)
        await store.close()
        with self.assertRaises(StoreFailed) as caught:
            await store.write(lambda tx: tx.append("a"))
        self.assertIn("closed", caught.exception.message)

    async def test_a_write_is_announced_on_the_loop_it_runs_on(self) -> None:
        # Opened on a loop that has since closed (a test, a restart of the
        # app's loop): a later write must still be announced, not fail after
        # its commit.
        store = await asyncio.to_thread(asyncio.run, Store.open(self.db))
        self.addAsyncCleanup(store.close)
        heard = []
        store.on_commit(lambda events: heard.extend(e.seq for e in events))
        self.assertEqual(await store.write(lambda tx: tx.append("a")), 1)
        self.assertEqual(heard, [1])

    async def test_a_wake_is_inserted_once(self) -> None:
        store = await self.open()

        def enqueue(tx):
            tx.execute("INSERT OR IGNORE INTO sessions (id, seq, role, preset, agent, spec, status, created_at, "
                       "updated_at) VALUES ('cse_1', 1, 'plain', 'plain_chat', 'a', '{}', 'cold', 't', 't')")
            return tx.execute("INSERT INTO wakes (id, session_id, priority, reason, state, created_at) "
                              "VALUES ('msg:cmsg_1', 'cse_1', 'now', 'message', 'queued', 't') "
                              "ON CONFLICT (id) DO NOTHING").rowcount

        self.assertEqual([await store.write(enqueue) for _ in range(3)], [1, 0, 0])

    async def test_a_listener_failure_never_undoes_a_commit(self) -> None:
        store = await self.open()
        store.on_commit(lambda events: 1 / 0)
        with self.assertLogs("services.cowork_agent.storm.store.db", level="ERROR"):
            await store.write(lambda tx: tx.append("a"))
        self.assertEqual(len(await store.events_after(0)), 1)

    async def test_sqlite_failures_become_an_actionable_error(self) -> None:
        store = await self.open()

        def full(tx):
            raise sqlite3.OperationalError("database or disk is full")

        with self.assertRaises(StoreFailed) as caught:
            await store.write(full)
        self.assertIn("free space", caught.exception.message)
        self.assertEqual(caught.exception.status, 503)

    async def test_only_a_disk_failure_gets_the_free_space_advice(self) -> None:
        store = await self.open()
        cases = {"no such column: paused": "Keep the file",
                 "no such table: grants": "Keep the file",
                 "table wakes has no column named meta": "Keep the file",
                 "database is locked": "held by another process",
                 "near \"SELEC\": syntax error": "could not be used",
                 "disk I/O error": "free space"}
        for text, says in cases.items():
            def fail(tx, text=text):
                raise sqlite3.OperationalError(text)
            with self.subTest(text=text):
                with self.assertRaises(StoreFailed) as caught:
                    await store.write(fail)
                self.assertIn(says, caught.exception.message)
                if says != "free space":
                    self.assertNotIn("free space", caught.exception.message)
        # SQLite's own error name decides when it is there.
        full = sqlite3.OperationalError("some disk trouble")
        full.sqlite_errorname = "SQLITE_FULL"
        with self.assertRaises(StoreFailed) as caught:
            await store.write(lambda tx: (_ for _ in ()).throw(full))
        self.assertIn("free space", caught.exception.message)
        # A query asking for what the store lacks once open: the store passed
        # the structure check, so it holds work and is kept, never moved aside.
        await store.write(lambda tx: tx.execute("ALTER TABLE sessions DROP COLUMN paused"))
        with self.assertRaises(StoreFailed) as caught:
            await store.read(lambda c: c.execute("SELECT paused FROM sessions").fetchall())
        self.assertIn("(no such column: paused)", caught.exception.message)
        self.assertIn("Keep the file", caught.exception.message)
        self.assertNotIn("move it aside", caught.exception.message.lower())
        self.assertNotIn("free space", caught.exception.message)


class HandBackTests(_Temp):
    """The rule boot recovery and the engine share for a turn that ended
    without its result (ch 07 §9.3), bounded so a failure can't loop."""

    async def asyncSetUp(self) -> None:
        self.db.parent.mkdir()
        load_sample(self.db)
        self.store = await self.open()

    async def take_again(self) -> None:
        await self.store.write(lambda tx: tx.execute(
            "UPDATE wakes SET state = 'taken', turn_id = 'turn_000000000001' WHERE id = 'msg:cmsg_0000000001'"))

    async def test_a_wake_whose_turn_fails_is_handed_back_three_times_then_dropped(self) -> None:
        results = []
        for n in range(MAX_REDELIVERIES + 1):
            if n:
                await self.take_again()
            note = RESUME_NOTE.format(step=f"step {n}", why="because the CLI stopped unexpectedly")
            results.append(await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", note, now="t",
                                                                       counts=True)))
        self.assertEqual(results, [(["msg:cmsg_0000000001"], [])] * MAX_REDELIVERIES
                         + [([], ["msg:cmsg_0000000001"])])
        (state, redelivery, envelope), = await self.store.read(lambda c: c.execute(
            "SELECT state, redelivery, envelope FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall())
        self.assertEqual((state, redelivery), ("dropped", MAX_REDELIVERIES))
        # One note, the latest, never stacked.
        self.assertEqual(envelope.count("Your previous turn was interrupted"), 1)
        self.assertIn("step 2", envelope)
        self.assertTrue(envelope.endswith('<wake reason="message">Make the tests pass.</wake>'))
        self.assertIn(str(MAX_REDELIVERIES + 1), dropped_message("because the CLI stopped unexpectedly"))

    async def test_one_note_per_hand_back_however_many_wakes(self) -> None:
        """D4 (live run): a turn's wakes go back batched; the note is said
        once, before the first, and never stacks on a second hand-back."""
        def second(tx) -> None:
            tx.execute("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, turn_id, "
                       "created_at) SELECT 'review:rv_1:9', session_id, 'now', 'review', 'The person allowed it.', "
                       "'{\"origin\": \"system\"}', 'taken', turn_id, created_at FROM wakes "
                       "WHERE id = 'msg:cmsg_0000000001'")
        await self.take_again()
        await self.store.write(second)
        for n in range(2):
            if n:
                await self.store.write(lambda tx: tx.execute(
                    "UPDATE wakes SET state = 'taken', turn_id = 'turn_000000000001' "
                    "WHERE id IN ('msg:cmsg_0000000001', 'review:rv_1:9')"))
            back, _ = await self.store.write(lambda tx: hand_back(
                tx, "turn_000000000001", RESUME_NOTE.format(step="“make”", why="because the server restarted"),
                now="t", counts=False))
            self.assertEqual(back, ["msg:cmsg_0000000001", "review:rv_1:9"])
            rows = await self.store.read(lambda c: c.execute(
                "SELECT id, envelope, json_extract(meta, '$.resume_note'), json_extract(meta, '$.handed_back') "
                "FROM wakes WHERE id IN ('msg:cmsg_0000000001', 'review:rv_1:9') ORDER BY rowid").fetchall())
            text = "".join(r[1] for r in rows)
            self.assertEqual(text.count("Your previous turn was interrupted"), 1)
            self.assertTrue(rows[0][1].startswith("Your previous turn was interrupted"))
            self.assertEqual(rows[1][1], "The person allowed it.")
            self.assertEqual([(r[2], r[3]) for r in rows], [(1, 1), (0, 1)])

    async def test_a_restart_never_counts_toward_the_bound(self) -> None:
        for _ in range(MAX_REDELIVERIES + 2):
            await self.take_again()
            back, gone = await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", "restarted", now="t",
                                                                     counts=False))
            self.assertEqual((back, gone), (["msg:cmsg_0000000001"], []))
        self.assertEqual(await self.store.read(lambda c: c.execute(
            "SELECT state, redelivery FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall()), [("queued", 0)])

    async def test_a_send_now_handed_back_no_longer_confirms_the_old_processs_runs(self) -> None:
        """The count a "send now" confirmed (§0.68) was of runs of the process
        that is gone: the next process's interrupt asks again."""
        await self.store.write(lambda tx: tx.execute(
            "UPDATE wakes SET meta = json_set(meta, '$.delivery', 'now', '$.confirmed_runs', 2) "
            "WHERE id = 'msg:cmsg_0000000001'"))
        await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", "NOTE", now="t", counts=True))
        self.assertEqual(await self.store.read(lambda c: c.execute(
            "SELECT state, json_extract(meta, '$.delivery'), json_type(meta, '$.confirmed_runs') FROM wakes "
            "WHERE id = 'msg:cmsg_0000000001'").fetchall()), [("queued", "now", None)])

    async def test_only_the_engines_own_note_is_replaced(self) -> None:
        """A person's message that starts like the note is never trimmed: the
        note is known by the wake's marker, not by its words."""
        words = "Your previous turn was interrupted, I know.\n\nPlease try again."
        await self.store.write(lambda tx: tx.execute("UPDATE wakes SET envelope = ? WHERE id = 'msg:cmsg_0000000001'",
                                                     (words,)))
        await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", "NOTE", now="t", counts=False))
        (envelope,), = await self.store.read(lambda c: c.execute(
            "SELECT envelope FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall())
        self.assertEqual(envelope, f"NOTE\n\n{words}")
        await self.take_again()
        await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", "NOTE 2", now="t", counts=False))
        (envelope,), = await self.store.read(lambda c: c.execute(
            "SELECT envelope FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall())
        self.assertEqual(envelope, f"NOTE 2\n\n{words}")

    async def test_a_turn_in_a_scope_the_founder_stopped_never_hands_its_input_back(self) -> None:
        """A Stop drops what is queued (w2-w3 design §3.1): a turn it cut
        short before its halt (a crash, a restart) is no way back in."""
        await self.take_again()
        (session,), = await self.store.read(lambda c: c.execute(
            "SELECT session_id FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall())
        await self.store.write(lambda tx: intents.set_intent(Writes(tx, "2026-11-12T08:00:00Z"), scope="session",
                                                             scope_id=session, key="run", value="stopped"))
        self.assertEqual(await self.store.write(lambda tx: hand_back(tx, "turn_000000000001", "NOTE", now=now_iso(),
                                                                     counts=False)), ([], []))
        self.assertEqual(await self.store.read(lambda c: c.execute(
            "SELECT state FROM wakes WHERE id = 'msg:cmsg_0000000001'").fetchall()), [("dropped",)])
        (data,), = await self.store.read(lambda c: c.execute(
            "SELECT data FROM events WHERE type = 'wake.dropped' AND session_id = ?", (session,)).fetchall())
        self.assertEqual(json.loads(data), {"ids": ["msg:cmsg_0000000001"], "reason": "stopped"})

    async def test_head_is_the_latest_seq(self) -> None:
        self.assertEqual(await self.store.head(), 13)             # the sample's last event (a run.finished)
        await self.store.write(lambda tx: tx.append("session", {"status": "cold"}))
        self.assertEqual(await self.store.head(), 14)


class WriteAheadLogTests(_Temp):
    """What a scrub overwrites (§0.112) is gone from the store's files, the
    write-ahead log included, as soon as the change commits (go/no-go 2, S2)."""

    TYPED = "made-up-typed-secret-0b7c41e9d2"

    async def asyncSetUp(self) -> None:
        self.store = await self.open()
        await self.store.write(lambda tx: (
            tx.execute("INSERT INTO sessions (id, seq, role, preset, agent, spec, status, created_at, updated_at) "
                       "VALUES ('cse_1', 0, 'plain', 'plain_chat', 'a', '{}', 'cold', 't', 't')"),
            tx.execute("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, created_at) "
                       "VALUES ('w1', 'cse_1', 'now', 'message', ?, json(?), 'queued', 't')",
                       (f"<wake>{self.TYPED}</wake>", json.dumps({"kept": "<wake>[redacted]</wake>"})))))

    def on_disk(self) -> bool:
        wal = self.db.with_name("xo.db-wal")
        return self.TYPED.encode() in self.db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")

    async def test_a_scrubbed_wake_leaves_nothing_of_its_text_in_the_store_s_files(self) -> None:
        self.assertTrue(self.on_disk())
        await self.store.write(lambda tx: (
            tx.execute("UPDATE wakes SET state = 'consumed', done_at = 't' WHERE id = 'w1'"), scrub(tx, ["w1"])))
        self.assertEqual(await self.store.read(lambda c: c.execute(
            "SELECT envelope FROM wakes WHERE id = 'w1'").fetchone()), ("<wake>[redacted]</wake>",))
        self.assertFalse(self.on_disk(), "the typed text is still in the store's files")

    async def test_another_process_reading_never_stalls_the_store_s_writes(self) -> None:
        """A backup or a ``sqlite3`` shell holding a read open: the scrub's
        checkpoint gives up at once (the next scrub or prune empties the log),
        instead of waiting out the busy timeout with every write behind it."""
        reader = sqlite3.connect(self.db)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM wakes").fetchone()
        started = time.monotonic()
        with self.assertLogs("services.cowork_agent.storm.store.db", "WARNING"):
            await self.store.write(lambda tx: (
                tx.execute("UPDATE wakes SET state = 'consumed', done_at = 't' WHERE id = 'w1'"), scrub(tx, ["w1"])))
        self.assertLess(time.monotonic() - started, 1.0)
        reader.execute("COMMIT")
        self.assertEqual(await self.store.read(lambda c: c.execute("PRAGMA busy_timeout").fetchone()), (5000,))

    async def test_a_write_that_scrubs_nothing_leaves_the_log_to_sqlite(self) -> None:
        await self.store.write(lambda tx: scrub(tx, ["w1"]))       # still queued: nothing to scrub
        self.assertGreater(self.db.with_name("xo.db-wal").stat().st_size, 0)


class LockTests(_Temp):
    def lock_file(self) -> Path:
        return self.dir / "run" / "engine.lock"

    def test_one_holder_at_a_time(self) -> None:
        lock = EngineLock.acquire(self.lock_file())
        self.addCleanup(lock.release)
        with self.assertRaises(EngineElsewhere) as caught:
            EngineLock.acquire(self.lock_file())
        self.assertEqual((caught.exception.code, caught.exception.status), ("engine_elsewhere", 503))
        self.assertIn(f"pid {os.getpid()}", caught.exception.message)
        self.assertEqual(stat.S_IMODE(self.lock_file().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.lock_file().parent.stat().st_mode), 0o700)
        lock.release()
        self.assertFalse(lock.held)
        EngineLock.acquire(self.lock_file()).release()

    def test_a_dead_holder_frees_the_lock(self) -> None:
        holder = ("import fcntl, os, sys, time\n"
                  "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                  "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                  "print('held', flush=True)\n"
                  "os._exit(0)\n")
        self.lock_file().parent.mkdir(mode=0o700)
        self.assertTrue(run_sync([sys.executable, "-c", holder, str(self.lock_file())]).ok)
        EngineLock.acquire(self.lock_file()).release()


class RecoveryTests(_Temp):
    async def asyncSetUp(self) -> None:
        self.db.parent.mkdir()
        load_sample(self.db)
        self.store = await self.open()
        self.run = self.dir / "run"
        self.lock = EngineLock.acquire(self.run / "engine.lock")
        self.addCleanup(self.lock.release)
        self.heard = []
        self.store.on_commit(lambda events: self.heard.extend(e.type for e in events))

    async def rows(self, sql: str) -> list[tuple]:
        return await self.store.read(lambda c: c.execute(sql).fetchall())

    async def test_recovery_needs_the_lock(self) -> None:
        self.lock.release()
        with self.assertRaises(RuntimeError):
            await recover(self.store, self.lock, folders=self.run)

    async def test_a_store_caught_mid_turn(self) -> None:
        (self.run / "cse_thread000000").mkdir()
        (self.run / "cse_gone").symlink_to(self.dir)
        (self.run / "codex").mkdir()   # not a session's folder: kept

        report = await recover(self.store, self.lock, folders=self.run)

        self.assertEqual(report.turns_interrupted, ("turn_000000000001",))
        self.assertEqual(await self.rows("SELECT status, stop_reason FROM turns"), [("interrupted", "restart")])
        self.assertEqual(report.sessions_fenced, ("cse_coord0000000", "cse_thread000000"))
        self.assertEqual(await self.rows("SELECT id, status, epoch FROM sessions ORDER BY id"), [
            ("cse_coord0000000", "cold", 2), ("cse_plain0000000", "cold", 2), ("cse_thread000000", "parked", 4)])

        self.assertEqual(report.wakes_requeued, ("msg:cmsg_0000000001",))
        (state, turn, redelivery, envelope), = await self.rows(
            "SELECT state, turn_id, redelivery, envelope FROM wakes WHERE id = 'msg:cmsg_0000000001'")
        self.assertEqual((state, turn, redelivery), ("queued", None, 0))       # a restart isn't the message's fault
        self.assertTrue(envelope.startswith("Your previous turn was interrupted after “Read the failing test” because "
                                            "the server restarted;"))
        # The session's next input says how many sub-agents the restart stopped (ch 14 §5.3).
        self.assertEqual(await self.rows("SELECT id, runs_lost_untold FROM sessions ORDER BY id"), [
            ("cse_coord0000000", 0), ("cse_plain0000000", 0), ("cse_thread000000", 2)])
        self.assertTrue(envelope.endswith('<wake reason="message">Make the tests pass.</wake>'))

        # The held call is reported for the review queue to park (ReviewDesk.park); recovery leaves its row.
        self.assertEqual(report.calls_parked, ("rv_000000000001",))
        self.assertEqual(await self.rows("SELECT state, parked FROM review_items"), [("open", 0)])
        self.assertEqual(report.sessions_to_rekick, ("cse_thread000000", "cse_coord0000000"))

        self.assertEqual((report.orphans_cleared, report.orphans_left), (1, 0))
        self.assertEqual(await self.rows("SELECT * FROM procs"), [])
        self.assertEqual(report.folders_removed, 2)
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["codex", "engine.lock"])
        # The working sub-agents (one at depth 2) died with the process: lost.
        self.assertEqual(report.runs_lost, ("run_000000000002", "run_000000000003"))
        self.assertEqual(await self.rows("SELECT id, state, ended_at IS NOT NULL FROM runs ORDER BY seq"), [
            ("run_000000000001", "finished", 1), ("run_000000000002", "lost", 1), ("run_000000000003", "lost", 1)])
        self.assertEqual(self.heard, ["turn.finished", "run.finished", "run.finished", "session", "session"])
        (data,), = await self.rows("SELECT data FROM events WHERE type = 'session' AND session_id = 'cse_thread000000' "
                                   "ORDER BY seq DESC LIMIT 1")
        self.assertEqual(json.loads(data), {"status": "parked", "reason": "restart",
                                            "wakes_requeued": ["msg:cmsg_0000000001"], "parked": ["rv_000000000001"],
                                            "runs_lost": ["run_000000000002", "run_000000000003"]})
        lost = await self.rows("SELECT run_id, json_extract(data, '$.run_id'), json_extract(data, '$.state'), "
                               "json_extract(data, '$.status') FROM events WHERE type = 'run.finished' AND seq > 13 "
                               "ORDER BY seq")
        self.assertEqual(lost, [("task-0000000002", "run_000000000002", "lost", None),
                                ("task-0000000003", "run_000000000003", "lost", None)])

    async def test_a_turn_that_finished_keeps_its_consumed_wakes(self) -> None:
        await self.store.write(lambda tx: (
            tx.execute("UPDATE turns SET status = 'finished', ended_at = 't'"),
            tx.execute("UPDATE wakes SET state = 'consumed', done_at = 't' WHERE id = 'msg:cmsg_0000000001'")))
        report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((report.turns_interrupted, report.wakes_requeued), ((), ()))
        self.assertEqual(await self.rows("SELECT state, redelivery FROM wakes WHERE id = 'msg:cmsg_0000000001'"),
                         [("consumed", 0)])

    async def test_a_second_recovery_changes_nothing(self) -> None:
        first = await recover(self.store, self.lock, folders=self.run)
        await self.store.write(lambda tx: tx.conn.executemany(       # what the engine's desk does with the report
            "UPDATE review_items SET parked = 1 WHERE id = ?", [(i,) for i in first.calls_parked]))
        before = await self.rows("SELECT count(*) FROM events")
        again = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((again.turns_interrupted, again.sessions_fenced, again.wakes_requeued, again.calls_parked),
                         ((), (), (), ()))
        self.assertEqual(await self.rows("SELECT count(*) FROM events"), before)

    async def test_only_the_registered_process_is_stopped(self) -> None:
        env = {"PATH": os.environ.get("PATH", "")}
        lost = await spawn_stream([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.dir,
                                  env=marker.marked(env, Mark(marker.space_of(self.db), "cse_thread000000", 3)),
                                  label="lost")
        bystander = await spawn_stream([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.dir,
                                       env=env, label="bystander")
        self.addAsyncCleanup(lost.terminate, 1.0)
        self.addAsyncCleanup(bystander.terminate, 1.0)
        await self.store.write(lambda tx: tx.execute(
            "INSERT INTO procs VALUES (?, ?, 'cse_thread000000', 3, 'sample-agent', 't')",
            (lost.pid, lost.start_ticks)))

        report = await recover(self.store, self.lock, folders=self.run)

        self.assertEqual(report.orphans_cleared, 2)     # the lost process, and the sample's long-gone row
        self.assertEqual(await asyncio.wait_for(lost.wait(), 10), -15)   # SIGTERM
        self.assertEqual(process_start_ticks(bystander.pid), bystander.start_ticks)
        self.assertIsNone(bystander.returncode)
        self.assertEqual(await self.rows("SELECT * FROM procs"), [])

    async def test_a_process_not_proven_this_spaces_is_never_stopped_and_its_session_is_held_back(self) -> None:
        # A store copied from a live Space holds that Space's process rows: a
        # server booted on the copy stopped the live sessions' CLIs (11-11,
        # 11-12). Only a process proven this store's server's work is stopped.
        path = {"PATH": os.environ.get("PATH", "")}
        children = {
            "another Space's marker": (marker.marked(path, Mark("ba9876543210", "cse_thread000000", 3)),
                                       "cse_thread000000"),
            "no marker, another root": ({**path, "HOME": str(self.dir / "live")}, "cse_coord0000000"),
            "our marker": (marker.marked(path, Mark(marker.space_of(self.db), "cse_plain0000000", 2)),
                           "cse_plain0000000"),
            "no marker, our root": ({**path, "QUIRQ_STATE_ROOT": str(self.dir)}, "cse_plain0000000"),
        }
        procs = {}
        for name, (env, session_id) in children.items():
            procs[name] = proc = await spawn_stream([sys.executable, "-c", "import time; time.sleep(60)"],
                                                    cwd=self.dir, env=env, label=name)
            self.addAsyncCleanup(proc.terminate, 1.0)
            await self.store.write(lambda tx: tx.execute("INSERT INTO procs VALUES (?, ?, ?, 1, 'cli', ?)", (
                proc.pid, proc.start_ticks, session_id, now_iso())))
            (self.run / session_id).mkdir(exist_ok=True)

        # This server's state root is where the store is kept (a server's own store, not a copy opened elsewhere).
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.dir)}), \
                self.assertLogs("services.cowork_agent.storm.store.recovery", level="WARNING"):
            report = await recover(self.store, self.lock, folders=self.run)

        for name in ("another Space's marker", "no marker, another root"):
            self.assertIsNone(procs[name].returncode, name)
            self.assertEqual(process_start_ticks(procs[name].pid), procs[name].start_ticks, name)
        for name in ("our marker", "no marker, our root"):
            self.assertEqual(await asyncio.wait_for(procs[name].wait(), 10), -15, name)     # SIGTERM
        self.assertEqual((report.orphans_cleared, report.orphans_left), (3, 2))     # and the sample's long-gone row
        self.assertEqual(await self.rows("SELECT pid, session_id FROM procs ORDER BY session_id"), [
            (procs["no marker, another root"].pid, "cse_coord0000000"),
            (procs["another Space's marker"].pid, "cse_thread000000")])
        # The rows kept hold their sessions back (the engine reads them); their folders stay.
        self.assertEqual(sorted(p.name for p in self.run.iterdir()),
                         ["cse_coord0000000", "cse_thread000000", "engine.lock"])


    async def bystander(self):
        proc = await spawn_stream([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.dir,
                                  env={"PATH": os.environ.get("PATH", "")}, label="bystander")
        self.addAsyncCleanup(proc.terminate, 1.0)
        return proc

    async def test_a_row_from_before_this_boot_names_nothing_and_is_cleared(self) -> None:
        # Start ticks count from the boot: after a reboot the same pid and
        # ticks can name an unrelated process, which must be left alone.
        proc = await self.bystander()
        await self.store.write(lambda tx: tx.execute(
            "INSERT INTO procs VALUES (?, ?, NULL, NULL, 'sample-agent', '2000-01-01T00:00:00Z')",
            (proc.pid, proc.start_ticks)))
        report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((report.orphans_cleared, report.orphans_left), (2, 0))
        self.assertIsNone(proc.returncode)
        self.assertEqual(process_start_ticks(proc.pid), proc.start_ticks)

    async def test_a_row_whose_process_may_survive_is_kept_with_its_folder(self) -> None:
        (self.run / "cse_thread000000").mkdir()
        (self.run / "cse_other").mkdir()
        with patch("services.cowork_agent.storm.store.recovery.reap_orphan", AsyncMock(return_value=False)), \
                patch("services.cowork_agent.storm.store.recovery.boot_time", return_value=None), \
                self.assertLogs("services.cowork_agent.storm.store.recovery", level="WARNING"):
            report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((report.orphans_cleared, report.orphans_left), (0, 1))
        self.assertEqual(await self.rows("SELECT pid FROM procs"), [(4194300,)])
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["cse_thread000000", "engine.lock"])

    async def test_a_row_that_fails_to_check_never_stops_the_boot(self) -> None:
        failing = AsyncMock(side_effect=PermissionError(1, "not permitted"))
        with patch("services.cowork_agent.storm.store.recovery.reap_orphan", failing), \
                patch("services.cowork_agent.storm.store.recovery.boot_time", return_value=None), \
                self.assertLogs("services.cowork_agent.storm.store.recovery", level="ERROR"):
            report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((report.orphans_left, report.turns_interrupted), (1, ("turn_000000000001",)))

    async def test_a_folder_that_cant_be_removed_never_stops_the_boot(self) -> None:
        stuck = self.run / "cse_stuck" / "locked"
        stuck.mkdir(parents=True)
        (stuck / "file").write_text("x")
        stuck.chmod(0o500)
        self.addCleanup(stuck.chmod, 0o700)
        (self.run / "cse_fine").mkdir()
        with self.assertLogs("services.cowork_agent.storm.store.recovery", level="WARNING"):
            report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual(report.folders_removed, 1)
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["cse_stuck", "engine.lock"])


if __name__ == "__main__":
    unittest.main()
