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
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.storm.store import (
    SCHEMA_VERSION,
    EngineElsewhere,
    EngineLock,
    Store,
    StoreFailed,
    StoreRefused,
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
        store.on_commit(lambda events: heard.append([e.data["n"] for e in events]))
        await asyncio.gather(*(store.write(lambda tx, n=n: [tx.append("x", {"n": n}) for _ in range(3)])
                               for n in range(50)))
        self.assertEqual(sorted(heard), [[n] * 3 for n in range(50)])
        seqs = [e.seq for e in await store.events_after(0, limit=500)]
        self.assertEqual(seqs, list(range(1, 151)))

    async def test_a_wake_is_inserted_once(self) -> None:
        store = await self.open()

        def enqueue(tx):
            tx.execute("INSERT OR IGNORE INTO sessions (id, role, preset, agent, spec, status, created_at, "
                       "updated_at) VALUES ('cse_1', 'plain', 'plain_chat', 'a', '{}', 'cold', 't', 't')")
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
            ("cse_coord0000000", "cold", 2), ("cse_plain0000000", "cold", 2), ("cse_thread000000", "cold", 4)])

        self.assertEqual(report.wakes_requeued, ("msg:cmsg_0000000001",))
        (state, turn, redelivery, envelope), = await self.rows(
            "SELECT state, turn_id, redelivery, envelope FROM wakes WHERE id = 'msg:cmsg_0000000001'")
        self.assertEqual((state, turn, redelivery), ("queued", None, 1))
        self.assertTrue(envelope.startswith("Your previous turn was interrupted after “Read the failing test”"))
        self.assertTrue(envelope.endswith('<wake reason="message">Make the tests pass.</wake>'))

        self.assertEqual(report.calls_parked, ("rv_000000000001",))
        self.assertEqual(await self.rows("SELECT state, parked FROM review_items"), [("open", 1)])
        self.assertEqual(report.sessions_to_rekick, ("cse_thread000000", "cse_coord0000000"))

        self.assertEqual((report.orphans_stopped, await self.rows("SELECT * FROM procs")), (0, []))
        self.assertEqual(report.folders_removed, 2)
        self.assertEqual(sorted(p.name for p in self.run.iterdir()), ["codex", "engine.lock"])
        self.assertEqual(self.heard, ["turn.finished", "session", "session", "wake.requeued", "review.parked"])

    async def test_a_turn_that_finished_keeps_its_consumed_wakes(self) -> None:
        await self.store.write(lambda tx: (
            tx.execute("UPDATE turns SET status = 'finished', ended_at = 't'"),
            tx.execute("UPDATE wakes SET state = 'consumed', done_at = 't' WHERE id = 'msg:cmsg_0000000001'")))
        report = await recover(self.store, self.lock, folders=self.run)
        self.assertEqual((report.turns_interrupted, report.wakes_requeued), ((), ()))
        self.assertEqual(await self.rows("SELECT state, redelivery FROM wakes WHERE id = 'msg:cmsg_0000000001'"),
                         [("consumed", 0)])

    async def test_a_second_recovery_changes_nothing(self) -> None:
        await recover(self.store, self.lock, folders=self.run)
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

        self.assertEqual(report.orphans_stopped, 1)
        self.assertEqual(await asyncio.wait_for(lost.wait(), 10), -15)   # SIGTERM
        self.assertEqual(process_start_ticks(bystander.pid), bystander.start_ticks)
        self.assertIsNone(bystander.returncode)
        self.assertEqual(await self.rows("SELECT * FROM procs"), [])


if __name__ == "__main__":
    unittest.main()
