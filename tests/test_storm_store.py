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
)
from services.cowork_agent.storm.store.db import SCHEMA_FILE
from utils.commands import process_start_ticks, run_sync, spawn_stream

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "quirq-state" / "engine" / "xo.db.sql"


def load_sample(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SAMPLE.read_text(encoding="utf-8"))
    conn.close()


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
    async def test_a_new_store_is_schema_v1_in_wal_with_foreign_keys(self) -> None:
        store = await self.open()
        version, journal, fks = await store.read(lambda c: (
            c.execute("PRAGMA user_version").fetchone()[0], c.execute("PRAGMA journal_mode").fetchone()[0],
            c.execute("PRAGMA foreign_keys").fetchone()[0]))
        self.assertEqual((version, journal, fks), (SCHEMA_VERSION, "wal", 1))
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    async def test_the_sample_is_schema_v1_exactly_and_opens(self) -> None:
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
        conn.executescript("CREATE TABLE future (x); PRAGMA user_version = 2;")
        conn.close()
        before = digest(self.db)
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("newer XO Space", caught.exception.message)
        self.assertEqual((caught.exception.code, caught.exception.status), ("store_refused", 503))
        self.assertEqual(digest(self.db), before)

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
        cases = {"no such column: paused": "made by an earlier storm build",
                 "no such table: grants": "made by an earlier storm build",
                 "table wakes has no column named meta": "made by an earlier storm build",
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
        # What an earlier build's store does to a real query once open.
        await store.write(lambda tx: tx.execute("ALTER TABLE sessions DROP COLUMN paused"))
        with self.assertRaises(StoreFailed) as caught:
            await store.read(lambda c: c.execute("SELECT paused FROM sessions").fetchall())
        self.assertIn("made by an earlier storm build (no such column: paused)", caught.exception.message)
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

    async def test_head_is_the_latest_seq(self) -> None:
        self.assertEqual(await self.store.head(), 8)              # the sample's last event
        await self.store.write(lambda tx: tx.append("session", {"status": "cold"}))
        self.assertEqual(await self.store.head(), 9)


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
        self.assertTrue(envelope.startswith("Your previous turn was interrupted after “Read the failing test”"))
        self.assertTrue(envelope.endswith('<wake reason="message">Make the tests pass.</wake>'))

        # The held call is reported for the review queue to park (ReviewDesk.park); recovery leaves its row.
        self.assertEqual(report.calls_parked, ("rv_000000000001",))
        self.assertEqual(await self.rows("SELECT state, parked FROM review_items"), [("open", 0)])
        self.assertEqual(report.sessions_to_rekick, ("cse_thread000000", "cse_coord0000000"))

        self.assertEqual((report.orphans_cleared, report.orphans_left), (1, 0))
        self.assertEqual(await self.rows("SELECT * FROM procs"), [])
        self.assertEqual(report.folders_removed, 2)
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["codex", "engine.lock"])
        self.assertEqual(self.heard, ["turn.finished", "session", "session"])
        (data,), = await self.rows("SELECT data FROM events WHERE type = 'session' AND session_id = 'cse_thread000000' "
                                   "ORDER BY seq DESC LIMIT 1")
        self.assertEqual(json.loads(data), {"status": "parked", "reason": "restart",
                                            "wakes_requeued": ["msg:cmsg_0000000001"], "parked": ["rv_000000000001"]})

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
        lost = await spawn_stream([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.dir, env=env,
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
