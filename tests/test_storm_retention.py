"""The daily retention prune (design ch 08 §6, §0.106; storm B4 PR 22).

Every row of §6's table on a real store with a fixed "now": what is older
goes, what is newer or always kept stays. Then the batches (another write
lands between two of them), a failure mid-prune (the store stays whole), the
lock, and the daily task's life with the engine."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.store import EngineLock, Store, StoreFailed, retention
from services.cowork_agent.storm.store.retention import DAY_S
from services.timestamps import iso
from tests.storm_engine_kit import Kit
from tests.test_storm_store import load_sample

NOW = datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc).timestamp()
SESSION = "cse_a"


def ago(days: float, now: float = NOW) -> str:
    return iso(datetime.fromtimestamp(now - days * DAY_S, timezone.utc))


class _Store(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.logs = self.dir / "engine" / "turns"
        self.store = await self.open_store()
        self.lock = EngineLock.acquire(self.dir / "run" / "engine.lock")
        self.addCleanup(self.lock.release)

    async def open_store(self) -> Store:
        store = await Store.open(self.dir / "engine" / "xo.db")
        self.addAsyncCleanup(store.close)
        await store.write(lambda tx: tx.execute(
            "INSERT INTO sessions (id, seq, role, preset, agent, spec, status, created_at, updated_at) "
            "VALUES (?, 0, 'plain', 'plain_chat', 'sample_agent', '{}', 'cold', ?, ?)", (SESSION, ago(60), ago(60))))
        return store

    async def prune(self) -> retention.Pruned:
        return await retention.prune(self.store, self.lock, now=NOW, logs=self.logs)

    async def event(self, type_: str, days: float, *, turn_id: str | None = None) -> int:
        return await self.store.write(lambda tx: tx.execute(
            "INSERT INTO events (ts, type, session_id, turn_id) VALUES (?, ?, ?, ?)",
            (ago(days), type_, SESSION, turn_id)).lastrowid)

    async def turn(self, turn_id: str, status: str) -> None:
        await self.store.write(lambda tx: tx.execute(
            "INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES (?, ?, 1, 'person', ?, ?)",
            (turn_id, SESSION, status, ago(60))))

    async def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await self.store.read(lambda conn: conn.execute(sql, params).fetchall())

    async def types_left(self) -> list[str]:
        return [t for (t,) in await self.rows("SELECT type FROM events ORDER BY seq")]


class EventsTests(_Store):
    async def test_step_progress_and_run_events_go_after_30_days_and_every_other_event_stays(self) -> None:
        pruned = ["step.tool", "step.text", "step.permission", "progress.run", "run.started", "run.state",
                  "run.finished"]
        kept_always = ["message", "session", "turn.started", "turn.finished", "review.opened", "worktree.kept",
                       "comms.call", "steps.tool", "stepper", "run", "running.x", "progress", "pool"]
        for type_ in pruned:
            await self.event(type_, 31)
        await self.event("step.tool", 30 + 1 / DAY_S)          # one second past the 30 days
        for type_ in kept_always:
            await self.event(type_, 400)
        young = [await self.event(type_, 29) for type_ in pruned]
        await self.event("step.tool", 30 - 1 / DAY_S)           # one second short of them
        await self.event("pool", 0)

        result = await self.prune()

        self.assertEqual(result.events, len(pruned) + 1)
        self.assertEqual(await self.types_left(), [*kept_always, *pruned, "step.tool", "pool"])
        self.assertLessEqual(set(young), {seq for (seq,) in await self.rows("SELECT seq FROM events")})

    async def test_the_newest_event_stays_so_the_head_never_moves_back(self) -> None:
        await self.event("step.tool", 40)
        head = await self.event("run.finished", 40)
        await self.prune()
        self.assertEqual((await self.types_left(), await self.store.head()), (["run.finished"], head))
        await self.event("message", 0)
        await self.prune()
        self.assertEqual(await self.types_left(), ["message"])

    async def test_a_running_turn_or_one_with_an_open_review_item_keeps_its_events(self) -> None:
        await self.turn("turn_running", "running")
        await self.turn("turn_asked", "finished")
        await self.turn("turn_decided", "finished")
        await self.turn("turn_done", "finished")
        for n, (turn_id, state) in enumerate((("turn_asked", "open"), ("turn_decided", "decided"))):
            await self.store.write(lambda tx, n=n, turn_id=turn_id, state=state: tx.execute(
                "INSERT INTO review_items (id, seq, session_id, turn_id, origin, kind, title, state, created_at) "
                "VALUES (?, ?, ?, ?, 'cli', 'permission', 'Run it', ?, ?)",
                (f"rv_{n}", 1000 + n, SESSION, turn_id, state, ago(40))))
        for turn_id in ("turn_running", "turn_asked", "turn_decided", "turn_done"):
            await self.event("step.tool", 40, turn_id=turn_id)
            await self.event("run.started", 40, turn_id=turn_id)
        await self.event("pool", 0)

        self.assertEqual((await self.prune()).events, 4)
        self.assertEqual(await self.rows("SELECT DISTINCT turn_id FROM events WHERE turn_id IS NOT NULL ORDER BY 1"),
                         [("turn_asked",), ("turn_running",)])


class RowsTests(_Store):
    async def test_grants_go_30_days_after_they_were_used_or_expired(self) -> None:
        grants = {  # id: (used_at, expires_at)
            "used_long_ago": (ago(31), ago(31 - 1 / 48)),
            "used_lately": (ago(29), ago(29 - 1 / 48)),
            "expired_long_ago": (None, ago(31)),
            "expired_lately": (None, ago(29)),
            "unused_and_valid": (None, iso(datetime.fromtimestamp(NOW + 600, timezone.utc))),
        }
        await self.store.write(lambda tx: tx.conn.executemany(
            "INSERT INTO grants (id, session_id, tool, input_sha256, created_at, expires_at, used_at) "
            "VALUES (?, ?, 'Bash', 'sha', ?, ?, ?)",
            [(gid, SESSION, ago(40), expires, used) for gid, (used, expires) in grants.items()]))

        self.assertEqual((await self.prune()).grants, 2)
        self.assertEqual({g for (g,) in await self.rows("SELECT id FROM grants")},
                         {"used_lately", "expired_lately", "unused_and_valid"})

    async def test_consumed_and_dropped_wakes_go_after_30_days_and_live_ones_stay(self) -> None:
        await self.turn("turn_running", "running")
        wakes = {  # id: (state, done_at, turn)
            "consumed_long_ago": ("consumed", ago(31), None),
            "dropped_long_ago": ("dropped", ago(31), None),
            "consumed_lately": ("consumed", ago(29), None),
            "dropped_lately": ("dropped", ago(29), None),
            "queued_long_ago": ("queued", None, None),
            "requeued_long_ago": ("requeued", None, None),
            "taken_long_ago": ("taken", None, "turn_running"),
        }
        await self.store.write(lambda tx: tx.conn.executemany(
            "INSERT INTO wakes (id, session_id, priority, reason, state, turn_id, created_at, done_at) "
            "VALUES (?, ?, 'now', 'message', ?, ?, ?, ?)",
            [(wid, SESSION, state, turn, ago(60), done) for wid, (state, done, turn) in wakes.items()]))

        self.assertEqual((await self.prune()).wakes, 2)
        self.assertEqual({w for (w,) in await self.rows("SELECT id FROM wakes")},
                         set(wakes) - {"consumed_long_ago", "dropped_long_ago"})

    async def test_raw_turn_logs_go_14_days_after_their_last_write_but_a_running_turns(self) -> None:
        await self.turn("turn_running", "running")
        self.logs.mkdir(parents=True)
        ages = {"turn_old.jsonl": 15, "turn_recent.jsonl": 13, "turn_running.jsonl": 20, "notes.txt": 20}
        for name, days in ages.items():
            path = self.logs / name
            path.write_text("{}\n")
            os.utime(path, (NOW - days * DAY_S, NOW - days * DAY_S))
        (self.logs / "turn_folder.jsonl").mkdir()
        os.utime(self.logs / "turn_folder.jsonl", (NOW - 20 * DAY_S, NOW - 20 * DAY_S))

        result = await self.prune()

        self.assertEqual((result.turn_logs, result.turn_logs_left), (1, 0))
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()),
                         ["notes.txt", "turn_folder.jsonl", "turn_recent.jsonl", "turn_running.jsonl"])

    async def test_a_turn_log_that_cannot_be_removed_is_counted_and_tried_again_next_time(self) -> None:
        self.logs.mkdir(parents=True)
        for name in ("turn_a.jsonl", "turn_b.jsonl"):
            (self.logs / name).write_text("{}\n")
            os.utime(self.logs / name, (NOW - 15 * DAY_S, NOW - 15 * DAY_S))
        self.logs.chmod(0o500)                  # nothing in it can be removed
        self.addCleanup(self.logs.chmod, 0o700)
        with self.assertLogs(retention.log, logging.WARNING) as logs:
            result = await self.prune()
        self.assertEqual((result.turn_logs, result.turn_logs_left), (0, 2))
        self.assertEqual(len(logs.records), 1)  # one warning a run, whatever the count
        self.logs.chmod(0o700)
        self.assertEqual(((await self.prune()).turn_logs, list(self.logs.iterdir())), (2, []))

    async def test_no_turn_log_folder_is_nothing_to_do(self) -> None:
        self.assertEqual(await self.prune(), retention.Pruned(events=0, grants=0, wakes=0, turn_logs=0,
                                                              turn_logs_left=0))
        self.assertFalse(self.logs.exists())


class KeptAlwaysTests(unittest.IsolatedAsyncioTestCase):
    """The schema's sample store, aged 60 days: only what §6 times out goes."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "engine" / "xo.db"
        db.parent.mkdir()
        load_sample(db)
        self.store = await Store.open(db)
        self.addAsyncCleanup(self.store.close)
        self.lock = EngineLock.acquire(Path(tmp.name) / "run" / "engine.lock")
        self.addCleanup(self.lock.release)

    async def dump(self) -> dict[str, list[tuple]]:
        def read(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
            tables = [t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT "
                                                 "LIKE 'sqlite_%' ORDER BY name")]
            return {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
        return await self.store.read(read)

    async def test_conversation_audit_and_engine_rows_are_kept_whatever_their_age(self) -> None:
        now = datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp()      # the sample is from 2026-10-15
        await self.store.write(lambda tx: tx.append("pool", {}))            # a newer head
        before = await self.dump()

        result = await retention.prune(self.store, self.lock, now=now, logs=Path(self.store.path.parent, "turns"))
        after = await self.dump()

        # The sample's turn is running, with an open review item: its steps and
        # runs stay. Only the run.finished event, which names no turn, goes.
        self.assertEqual(result.events, 1)
        self.assertEqual([r[0] for r in before["events"] if r not in after["events"]], [13])
        self.assertEqual({t: rows for t, rows in after.items() if t != "events"},
                         {t: rows for t, rows in before.items() if t != "events"})

        # Once the turn is over and the item decided, its steps and runs go too.
        await self.store.write(lambda tx: (
            tx.execute("UPDATE turns SET status = 'finished'"),
            tx.execute("UPDATE review_items SET state = 'decided'")))
        await retention.prune(self.store, self.lock, now=now, logs=Path(self.store.path.parent, "turns"))
        self.assertEqual(await self.store.read(lambda c: c.execute(
            "SELECT type FROM events WHERE type LIKE 'step.%' OR type LIKE 'run.%'").fetchall()), [])


class BatchesAndFailuresTests(_Store):
    async def test_another_write_lands_between_two_batches(self) -> None:
        for _ in range(100):
            await self.event("step.tool", 40)
        await self.event("pool", 0)
        seen: list[int] = []
        done = asyncio.Event()

        async def writer() -> None:
            while not done.is_set():
                seen.append(await self.store.write(lambda tx: (
                    tx.append("pool"),
                    tx.execute("SELECT count(*) FROM events WHERE type = 'step.tool'").fetchone()[0])[1]))

        with mock.patch.object(retention, "BATCH_ROWS", 10):
            task = asyncio.create_task(writer())
            await asyncio.sleep(0)
            pruned = await self.prune()
            done.set()
            await task
        self.assertEqual(pruned.events, 100)
        # Writes landed with the prune part done, and only ever saw whole batches.
        self.assertTrue(any(0 < left < 100 for left in seen), seen)
        self.assertTrue(all(left % 10 == 0 for left in seen), seen)

    async def test_a_failure_mid_prune_keeps_the_batches_done_and_the_store_whole(self) -> None:
        for _ in range(35):
            await self.event("step.tool", 40)
        await self.event("pool", 0)
        real, calls = retention._events_batch, []

        def dies_in_the_second_batch(tx, *args):
            deleted = real(tx, *args)
            calls.append(deleted)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")    # after the delete, before the commit
            return deleted

        with mock.patch.object(retention, "BATCH_ROWS", 10), \
                mock.patch.object(retention, "_events_batch", dies_in_the_second_batch):
            with self.assertRaises(StoreFailed):
                await self.prune()
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'step.tool'"), [(25,)])
        self.assertEqual(await self.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

        self.assertEqual((await self.prune()).events, 25)          # the next run finishes the work
        self.assertEqual(await self.types_left(), ["pool"])

    async def test_a_store_whose_lock_this_process_does_not_hold_is_never_pruned(self) -> None:
        await self.event("step.tool", 40)
        await self.event("pool", 0)
        self.lock.release()
        with self.assertRaises(RuntimeError):
            await self.prune()
        self.assertEqual(await self.types_left(), ["step.tool", "pool"])

    async def test_each_run_logs_one_line_of_counts(self) -> None:
        await self.turn("turn_secret_name", "finished")
        await self.event("step.tool", 40, turn_id="turn_secret_name")
        await self.event("pool", 0)
        with self.assertLogs(retention.log, logging.DEBUG) as logs:
            await self.prune()
        (line,) = logs.output
        self.assertIn("pruned 1 event(s), 0 grant(s), 0 wake(s) and 0 raw turn log(s) (0 could not be removed)", line)
        self.assertNotIn("turn_secret_name", line)


class DailyTaskTests(unittest.IsolatedAsyncioTestCase):
    """The prune is one of the engine's loops: it starts with it, runs daily,
    survives a failed run, and stops with it."""

    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.addAsyncCleanup(self.kit.close)
        self.runs = 0
        self.ran = asyncio.Event()
        real = retention.prune

        async def counted(store, lock, *, now):
            self.runs += 1
            try:
                if self.runs == 1 and self.fail_first:
                    raise sqlite3.OperationalError("disk I/O error")
                return await real(store, lock, now=now)
            finally:
                self.ran.set()
        self.fail_first = False
        for patch in (mock.patch.object(retention, "prune", counted),
                      mock.patch.object(retention, "PRUNE_FIRST_S", 0),
                      mock.patch.object(retention, "PRUNE_INTERVAL_S", 0.01)):
            patch.start()
            self.addCleanup(patch.stop)

    async def old_step(self, engine: storm_engine.Engine) -> None:
        now = self.kit.clock.time()
        await engine.write(lambda w: w.append("step.notice", {"text": "old"}))
        await engine.write(lambda w: w.execute("UPDATE events SET ts = ? WHERE type = 'step.notice'", (ago(31, now),)))
        await engine.write(lambda w: w.append("pool", {}))

    async def steps_left(self, engine: storm_engine.Engine) -> int:
        return await engine.store.read(lambda c: c.execute(
            "SELECT count(*) FROM events WHERE type = 'step.notice'").fetchone()[0])

    async def test_it_starts_with_the_engine_prunes_and_stops_with_it(self) -> None:
        with mock.patch.object(retention, "PRUNE_FIRST_S", 3600):
            engine = await self.kit.boot()
        (task,) = [t for t in engine.background_loops() if t.get_name() == "storm-retention"]
        await asyncio.sleep(0.05)
        self.assertEqual((self.runs, task.done()), (0, False))      # its first run waits an hour
        await storm_engine.shutdown()
        self.assertTrue(task.cancelled())

        engine = await self.kit.boot()
        await self.old_step(engine)
        await asyncio.wait_for(self.pruned(engine), 10)
        (task,) = [t for t in engine.background_loops() if t.get_name() == "storm-retention"]
        await storm_engine.shutdown()
        self.assertTrue(task.cancelled())
        self.assertFalse(engine.lock.held)

    async def test_a_failed_run_is_logged_and_the_next_one_prunes(self) -> None:
        self.fail_first = True
        with self.assertLogs("services.cowork_agent.storm.engine.engine", logging.WARNING) as logs:
            engine = await self.kit.boot()
            await self.old_step(engine)
            await asyncio.wait_for(self.pruned(engine), 10)
        self.assertTrue(any("storm retention prune: tick failed" in line for line in logs.output), logs.output)
        self.assertGreaterEqual(self.runs, 2)

    async def pruned(self, engine: storm_engine.Engine) -> None:
        """Until a run has pruned the old step (runs come every 10 ms)."""
        while await self.steps_left(engine):
            self.ran.clear()
            await self.ran.wait()


if __name__ == "__main__":
    unittest.main()
