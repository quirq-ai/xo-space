"""The store's checks on open and on migration (W11, the gaps left in 454478a).

A store at this build's schema version is compared whole, not by its columns
only: CHECK constraints (as a multiset, since ALTER TABLE rewrites a table's
SQL), indexes with their WHERE, foreign keys, views and triggers. A
migration also runs SQLite's foreign-key and integrity checks before it
commits. A refused store that holds rows is never called disposable, and a
fault in this build's own checking code refuses the store instead of
crashing the boot.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.storm.store import SCHEMA_VERSION, Store, StoreRefused
from services.cowork_agent.storm.store import db, migrations
from tests.test_storm_store import V1_SAMPLE, _Temp, columns_of, digest, load_sample

SCHEMA = db.SCHEMA_FILE.read_text(encoding="utf-8")
#: threads.state as schema.sql declares it: a column with a CHECK of its own.
STATE = ("    state         TEXT NOT NULL CHECK (state IN ('working', 'needs_reply', 'needs_approval', 'done', "
         "'failed',\n                                                 'resolved', 'paused')),\n")
DB_LOG = "services.cowork_agent.storm.store.db"


def memory(schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    return conn


def migrated_v1() -> sqlite3.Connection:
    """The v1 sample brought to this build's schema the way a real store is."""
    conn = memory(V1_SAMPLE.read_text(encoding="utf-8"))
    conn.executescript("BEGIN;\n" + "\n".join(migrations.steps_from(1, SCHEMA_VERSION)) + "\nCOMMIT;")
    return conn


def changed(old: str, new: str, schema: str = SCHEMA) -> str:
    assert schema.count(old) == 1, f"{old!r} isn't in schema.sql exactly once"
    return schema.replace(old, new)


def row_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                             "'sqlite_%'")]
        return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


class CheckConstraintTests(unittest.TestCase):
    def test_constraints_read_the_same_whatever_their_order_spacing_case_and_comments(self) -> None:
        one = ("CREATE TABLE t (a TEXT CHECK (a IN ('x', 'y')),   -- a check (not one)\n"
               "  b INT, /* check (b) */ CHECK(b>0))")
        other = "create table t (b int, a text check(a in ('x','y')), check ( b > 0 ))"
        self.assertEqual(db._checks(one), db._checks(other))
        self.assertEqual(db._checks(one), ("a in ( 'x' , 'y' )", "b > 0"))

    def test_a_paren_or_the_word_check_inside_a_string_or_a_name_is_not_a_constraint(self) -> None:
        sql = "CREATE TABLE t (a TEXT CHECK (a <> ')' AND a <> 'CHECK (x)'), checked_at TEXT, check_ok INT)"
        self.assertEqual(db._checks(sql), ("a <> ')' and a <> 'CHECK (x)'",))

    def test_a_value_spelled_differently_is_another_constraint(self) -> None:
        self.assertNotEqual(db._checks("CREATE TABLE t (a CHECK (a = 'x'))"),
                            db._checks("CREATE TABLE t (a CHECK (a = 'X'))"))

    def test_a_new_store_and_a_migrated_one_have_the_same_constraints(self) -> None:
        """ALTER TABLE wrote the migrated threads table's SQL in another
        order; its constraints still compare equal to a new store's."""
        fresh, old = memory(SCHEMA), migrated_v1()
        self.addCleanup(fresh.close)
        self.addCleanup(old.close)
        self.assertNotEqual(
            old.execute("SELECT sql FROM sqlite_master WHERE name = 'threads'").fetchone(),
            fresh.execute("SELECT sql FROM sqlite_master WHERE name = 'threads'").fetchone())
        self.assertIn("summary is null or length ( summary ) <= 90", db._structure(old).tables["threads"].checks)
        self.assertEqual(db._structure(old), db._structure(fresh))
        self.assertIsNone(db._structure_difference(old))

    def test_a_dropped_column_takes_its_own_check_and_matches_a_schema_that_never_had_it(self) -> None:
        """ALTER TABLE ... DROP COLUMN (how W1 drops threads.state once it is
        derived): on a new store and on a migrated one alike, what is left
        is exactly a schema written without that column."""
        never_had_it = memory(changed(STATE, ""))
        self.addCleanup(never_had_it.close)
        want = db._structure(never_had_it)
        for name, conn in (("new", memory(SCHEMA)), ("migrated from v1", migrated_v1())):
            with self.subTest(name):
                self.addCleanup(conn.close)
                conn.execute("ALTER TABLE threads DROP COLUMN state")
                found = db._structure(conn)
                self.assertFalse([c for c in found.tables["threads"].checks if c.startswith("state")])
                self.assertEqual(found, want)


class CurrentVersionTests(_Temp):
    """A store at this build's version is opened only if it is exactly this
    build's structure, and is left untouched when it isn't."""

    def make(self, schema: str, *, rows: bool) -> None:
        self.db.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(f"{schema}\nPRAGMA user_version = {SCHEMA_VERSION};")
        if rows:
            conn.execute("INSERT INTO chats (id, kind, created_at) VALUES ('chat_1', 'workspace', 't')")
            conn.commit()
        conn.close()

    async def test_a_store_that_differs_beyond_its_columns_is_refused_untouched(self) -> None:
        cases = (
            ("a CHECK lost a value", changed("'resolved', 'paused')),", "'resolved')),"),
             "its threads table's CHECK constraints differ from this build's (lacks state in ( 'working' , "
             "'needs_reply' , 'needs_approval' , 'done' , 'failed' , 'resolved' , 'paused' ), and has state in"),
            ("a CHECK more", changed("    label         TEXT,", "    label         TEXT CHECK (length(label) < 40),"),
             "its threads table's CHECK constraints differ from this build's (has length ( label ) < 40)"),
            ("a partial index lost its WHERE", changed("ON sessions (native_id) WHERE native_id IS NOT NULL",
                                                       "ON sessions (native_id)"),
             "its sessions table's indexes differ"),
            ("a foreign key lost its cascade", changed(
                "chat_id  TEXT PRIMARY KEY REFERENCES chats (id) ON DELETE CASCADE",
                "chat_id  TEXT PRIMARY KEY REFERENCES chats (id)"),
             "its chat_seen table's foreign keys differ"),
            ("a view this build doesn't make",
             SCHEMA + "\nCREATE VIEW open_items AS SELECT id FROM review_items WHERE state = 'open';",
             "its views or triggers differ from this build's (has create view open_items"),
        )
        for case, schema, why in cases:
            with self.subTest(case):
                if self.db.exists():
                    self.db.unlink()
                self.make(schema, rows=False)
                before = digest(self.db)
                with self.assertRaises(StoreRefused) as caught:
                    await Store.open(self.db)
                self.assertIn(why, caught.exception.message)
                self.assertEqual(digest(self.db), before)

    async def test_a_refused_store_that_holds_rows_is_kept_and_an_empty_one_may_go(self) -> None:
        schema = changed("'resolved', 'paused')),", "'resolved')),")
        for rows, advice, never in ((True, "Keep the file", "Move it aside"), (False, "Move it aside", "Keep the file")):
            with self.subTest(rows=rows):
                if self.db.exists():
                    self.db.unlink()
                self.make(schema, rows=rows)
                with self.assertRaises(StoreRefused) as caught:
                    await Store.open(self.db)
                self.assertIn(advice, caught.exception.message)
                self.assertNotIn(never, caught.exception.message)

    async def test_a_fault_in_the_comparison_refuses_the_store_instead_of_crashing_the_boot(self) -> None:
        await (await self.open()).close()
        before = digest(self.db)
        with patch.object(db, "_structure", side_effect=TypeError("a bug in this build")), \
                self.assertLogs(DB_LOG, "ERROR"):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("couldn't be compared with this build's (TypeError: a bug in this build)",
                      caught.exception.message)
        self.assertEqual(digest(self.db), before)


class MigrationCheckTests(_Temp):
    """What a migration finds before its COMMIT refuses the store, rolled
    back: the file keeps its version and every row."""

    def v1_store(self) -> None:
        self.db.parent.mkdir(exist_ok=True)
        load_sample(self.db, V1_SAMPLE)

    def assert_kept_as_v1(self, rows: dict[str, int], columns: dict) -> None:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual({t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in rows}, rows)
        self.assertEqual(columns_of(self.db), columns)

    def counts(self) -> dict[str, int]:
        return row_counts(self.db)

    async def test_rows_pointing_nowhere_refuse_the_migration(self) -> None:
        """A row whose parent is gone (written while foreign keys were off)
        is found by foreign_key_check before the COMMIT."""
        self.v1_store()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) "
                     "VALUES ('turn_orphan', 'cse_gone', 0, 'wake', 'finished', 't')")
        conn.commit()
        conn.close()
        rows, columns = self.counts(), columns_of(self.db)
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("rows point at rows that aren't there: 1 in turns (to sessions)", caught.exception.message)
        self.assertIn("Keep the file", caught.exception.message)
        self.assert_kept_as_v1(rows, columns)

    async def test_a_damaged_file_refuses_the_migration(self) -> None:
        """An index page SQLite can't read: the steps never touch it, but
        quick_check finds it before the COMMIT."""
        self.v1_store()
        conn = sqlite3.connect(self.db)
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'events_by_type_ts'").fetchone()[0]
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()
        rows, columns = self.counts(), columns_of(self.db)
        with open(self.db, "r+b") as f:
            f.seek(page * (root - 1) + 3)           # the index's root page: its count of cells
            f.write(b"\x7f\x7f")
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("SQLite's integrity check found", caught.exception.message)
        self.assert_kept_as_v1(rows, columns)

    async def test_a_fault_in_the_check_of_a_migrated_store_refuses_it_rolled_back(self) -> None:
        self.v1_store()
        rows, columns = self.counts(), columns_of(self.db)
        with patch.object(db, "_refuse_unknown", lambda conn, path: 1), \
                patch.object(db, "_structure_difference", side_effect=KeyError("threads")), \
                self.assertLogs(DB_LOG, "ERROR"):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("couldn't be compared with this build's (KeyError: 'threads')", caught.exception.message)
        self.assert_kept_as_v1(rows, columns)

    async def test_a_fault_in_the_migrations_own_code_refuses_the_store_rolled_back(self) -> None:
        self.v1_store()
        rows, columns = self.counts(), columns_of(self.db)
        with patch.object(migrations, "_findings", side_effect=ValueError("a bug in this build")), \
                self.assertLogs(DB_LOG, "ERROR"):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("ValueError: a bug in this build", caught.exception.message)
        self.assertIn("Nothing in it was changed", caught.exception.message)
        self.assert_kept_as_v1(rows, columns)

    async def test_a_fault_in_the_rehearsal_refuses_the_store_untouched(self) -> None:
        self.v1_store()
        before = digest(self.db)
        with patch.object(migrations, "rehearse", side_effect=RuntimeError("a bug in this build")):
            with self.assertRaises(StoreRefused) as caught:
                await Store.open(self.db)
        self.assertIn("a bug in this build", caught.exception.message)
        self.assertIn("Keep the file", caught.exception.message)
        self.assertEqual(digest(self.db), before)

    async def test_an_empty_older_store_a_step_doesnt_fit_may_be_moved_aside(self) -> None:
        """The keep-it advice is for a store with rows; an older store with
        none holds nothing to keep."""
        self.v1_store()
        conn = sqlite3.connect(self.db)
        for table in self.counts():
            conn.execute(f'DELETE FROM "{table}"')
        conn.execute("CREATE TABLE made (x)")           # what step 2 adds is there already
        conn.commit()
        conn.close()
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("schema 1 can't be migrated", caught.exception.message)
        self.assertIn("Move it aside", caught.exception.message)


class SchemaThreeTests(_Temp):
    """Schema 3 (record design v2 §7): the founder's intents, the ledger with
    its append-only triggers, and ``review_items.subject`` with its index,
    added by one step to a store of either earlier schema."""

    def older_store(self, version: int) -> Path:
        """The v1 sample, brought to ``version`` by the steps as a build of
        that schema did."""
        path = self.dir / f"v{version}.db"
        load_sample(path, V1_SAMPLE)
        conn = sqlite3.connect(path)
        conn.executescript("BEGIN;\n" + "\n".join(migrations.steps_from(1, version))
                           + f"\nPRAGMA user_version = {version};\nCOMMIT;")
        conn.close()
        return path

    async def test_a_schema_2_and_a_schema_1_store_migrate_to_3_keeping_every_row(self) -> None:
        await (await self.open()).close()
        fresh = sqlite3.connect(self.db)
        self.addCleanup(fresh.close)
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.older_store(version)
                before = row_counts(path)
                self.assertNotIn("intents", before)
                await (await Store.open(path)).close()
                after = row_counts(path)
                self.assertEqual({t: after[t] for t in before}, before)
                self.assertEqual((after["intents"], after["ledger"]), (0, 0))
                migrated = sqlite3.connect(path)
                self.addCleanup(migrated.close)
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(db._structure(migrated), db._structure(fresh))
                self.assertEqual(migrated.execute("SELECT count(*) FROM review_items WHERE subject IS NULL").fetchone(),
                                 (before["review_items"],))

    async def test_pools_that_kept_their_own_windows_migrate_without_them_keeping_every_row(self) -> None:
        """Schema 3 drops the pools' own copy of usage (``snapshot``, the old
        ``paid_continue_until``): a pool is computed from the space
        advisory's facts. A store whose pools hold both (as :5002's Codex pool
        did, paid since 11-10, its weekly window kept under its old name)
        migrates with every pool's row, and nothing of the old windows can
        hold a pool after it."""
        snapshot = ('{"status": "ok", "window": "weekly", "utilization": 1.0, "overage": true, "windows": '
                    '{"weekly": {"state": "paid", "resets_at": "2026-11-16T06:52:23Z", "since": '
                    '"2026-11-10T05:21:48Z", "utilization": 1.0}}}')
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.older_store(version)
                conn = sqlite3.connect(path)
                conn.execute("INSERT INTO pools (id, state, resets_at, snapshot, paid_continue_until, updated_at) "
                             "VALUES ('codex:acct-4d3aa4c0d340', 'paid', '2026-11-16T06:52:23Z', json(?), "
                             "'2026-11-16T06:52:23Z', '2026-11-10T05:22:02Z')", (snapshot,))
                conn.commit()
                conn.close()
                before = row_counts(path)
                await (await Store.open(path)).close()
                migrated = sqlite3.connect(path)
                self.addCleanup(migrated.close)
                self.assertEqual([r[1] for r in migrated.execute("PRAGMA table_info(pools)")],
                                 ["id", "state", "resets_at", "updated_at"])
                self.assertEqual(row_counts(path)["pools"], before["pools"])
                self.assertIn(("codex:acct-4d3aa4c0d340", "paid", "2026-11-16T06:52:23Z", "2026-11-10T05:22:02Z"),
                              migrated.execute("SELECT id, state, resets_at, updated_at FROM pools").fetchall())
                self.assertIsNone(db._structure_difference(migrated))

    async def test_a_store_at_3_passes_the_structure_check_and_reopens(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 3)
        migrated = self.older_store(2)
        for path in (self.db, migrated):
            with self.subTest(path.name):
                await (await Store.open(path)).close()
                await (await Store.open(path)).close()        # at 3 now: checked whole, opened as it is
                conn = sqlite3.connect(path)
                self.addCleanup(conn.close)
                self.assertIsNone(db._structure_difference(conn))
                self.assertEqual(sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = "
                                                                   "'trigger'")),
                                 ["ledger_no_delete", "ledger_no_update"])

    async def test_a_store_at_3_without_the_ledgers_guard_is_refused(self) -> None:
        await (await self.open()).close()
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER ledger_no_delete")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreRefused) as caught:
            await Store.open(self.db)
        self.assertIn("its views or triggers differ from this build's (lacks create trigger ledger_no_delete",
                      caught.exception.message)

    async def test_one_open_or_decided_item_per_subject(self) -> None:
        store = await self.open()
        seq = iter(range(1, 100))

        async def item(state: str, subject: str | None) -> str:
            n = next(seq)
            await store.write(lambda tx: tx.execute(
                "INSERT INTO review_items (id, seq, origin, kind, title, state, created_at, subject) "
                "VALUES (?, ?, 'engine', 'usage', 'Go on past the plan?', ?, 't', ?)",
                (f"rv_{n}", n, state, subject)))
            return f"rv_{n}"

        first = await item("open", "paid:agent_a:acct-1:2026-11-12T18:00:00Z")
        for state in ("open", "decided"):
            with self.subTest(state), self.assertRaises(sqlite3.IntegrityError):
                await item(state, "paid:agent_a:acct-1:2026-11-12T18:00:00Z")
        for state in ("withdrawn", "expired"):
            await item(state, "paid:agent_a:acct-1:2026-11-12T18:00:00Z")
        await item("open", "paid:agent_a:acct-1:2026-11-19T18:00:00Z")      # the next window: a new subject
        await item("open", None)
        await item("open", None)
        await store.write(lambda tx: tx.execute("UPDATE review_items SET state = 'decided' WHERE id = ?", (first,)))
        with self.assertRaises(sqlite3.IntegrityError):
            await item("open", "paid:agent_a:acct-1:2026-11-12T18:00:00Z")      # answered once in its window
        await store.write(lambda tx: tx.execute("UPDATE review_items SET state = 'withdrawn' WHERE id = ?", (first,)))
        await item("open", "paid:agent_a:acct-1:2026-11-12T18:00:00Z")


if __name__ == "__main__":
    unittest.main()
