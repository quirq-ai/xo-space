"""Storm's records: the founder's intents and the ledger (record design v2
§2, §5 rules 1, 2, 3, 7 and 8, §6 invariants 2, 8 and 9; w2-w3-design §1
and §2).

An intent is written with its ``intent`` event and its ledger entry in the
caller's one transaction, or not at all. The ledger chains every entry to
the one before it and names the hash of the record's row, so ``verify``
finds an edited or removed entry, and an intent row changed, added or
removed outside it. The resolution (which intent holds for a session, and
a pool's paid mode) is pure, and tested on its own.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.engine.session import Meta
from services.cowork_agent.storm.records import intents, ledger
from services.cowork_agent.storm.records.intents import Intent, IntentInvalid
from services.cowork_agent.storm.store import Store
from services.errors import ServiceError
from services.timestamps import iso

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
AT = iso(NOW)
LATER = iso(NOW + timedelta(hours=6))
EARLIER = iso(NOW - timedelta(hours=6))
CHAT = "chat_p0000000000"
THREAD = f"{CHAT}/1760518860.000100"
POOL = "agent_a:acct-1"


def stop(w: Writes, scope: str = "chat", scope_id: str = CHAT, **kw: Any) -> Intent:
    return intents.set_intent(w, scope=scope, scope_id=scope_id, key="run", value="stopped", **kw)


class _Store(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = await self.fresh()

    async def fresh(self) -> Store:
        """A new, empty store (one per subtest that tampers with it)."""
        self.store = await Store.open(self.dir / f"xo-{len(list(self.dir.iterdir()))}.db")
        self.addAsyncCleanup(self.store.close)
        return self.store

    async def write(self, fn: Callable[[Writes], Any]) -> Any:
        return await self.store.write(lambda tx: fn(Writes(tx, AT)))

    async def behind_the_records(self, *sql: str) -> None:
        """Change the store the way a confused agent or a mistaken script
        would: plain SQL, past the records module."""
        def run(tx) -> None:
            for statement in sql:
                tx.execute(statement)
        await self.store.write(run)

    async def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await self.store.read(lambda conn: conn.execute(sql, params).fetchall())

    async def findings(self) -> list[ledger.Finding]:
        return await self.store.read(ledger.verify)


class LedgerTests(_Store):
    async def test_every_change_is_chained_and_the_chain_verifies(self) -> None:
        await self.write(stop)
        paused = await self.write(lambda w: intents.set_intent(
            w, scope="thread", scope_id=THREAD, key="run", value="paused", cites="cmsg_1"))
        await self.write(lambda w: intents.set_intent(w, scope="pool", scope_id=POOL, key="paid", value="never",
                                                      until=LATER))
        await self.write(lambda w: intents.clear_intent(w, scope="chat", scope_id=CHAT, key="run"))

        entries = await self.rows("SELECT seq, at, actor, action, record, row_sha256, prev_sha256, sha256 FROM ledger "
                                  "ORDER BY seq")
        self.assertEqual([e[0] for e in entries], [1, 2, 3, 4])
        self.assertEqual({(e[1], e[2]) for e in entries}, {(AT, ledger.FOUNDER)})
        self.assertEqual([(e[3], e[4]) for e in entries], [
            ("set", f"intent:chat:{CHAT}:run"), ("set", f"intent:thread:{THREAD}:run"),
            ("set", f"intent:pool:{POOL}:paid"), ("clear", f"intent:chat:{CHAT}:run")])
        self.assertEqual(entries[1][5], ledger.row_sha256(dataclasses.asdict(paused)))
        self.assertEqual(entries[3][5], ledger.REMOVED)
        self.assertEqual([e[6] for e in entries], [ledger.GENESIS, *(e[7] for e in entries[:-1])])
        self.assertEqual(await self.findings(), [])

    async def test_an_entry_is_never_changed_or_removed(self) -> None:
        await self.write(stop)
        for statement in ("UPDATE ledger SET actor = 'cse_someone'", "DELETE FROM ledger"):
            with self.subTest(statement), self.assertRaises(sqlite3.IntegrityError) as caught:
                await self.behind_the_records(statement)
            self.assertIn("the ledger is append-only", str(caught.exception))
        self.assertEqual(len(await self.rows("SELECT seq FROM ledger")), 1)

    async def test_an_intent_row_changed_added_or_removed_outside_the_ledger_is_found(self) -> None:
        record = f"intent:chat:{CHAT}:run"
        cases = (
            ("changed", [stop], "UPDATE intents SET value = 'paused'", "changed"),
            ("added", [], f"INSERT INTO intents VALUES ('chat', '{CHAT}', 'run', 'stopped', NULL, NULL, '{AT}')",
             "no ledger entry"),
            ("removed", [stop], "DELETE FROM intents", "gone"),
            ("brought back after a clear",
             [stop, lambda w: intents.clear_intent(w, scope="chat", scope_id=CHAT, key="run")],
             f"INSERT INTO intents VALUES ('chat', '{CHAT}', 'run', 'stopped', NULL, NULL, '{AT}')", "cleared"),
        )
        for case, writes, tamper, words in cases:
            with self.subTest(case):
                await self.fresh()
                for fn in writes:
                    await self.write(fn)
                self.assertEqual(await self.findings(), [])
                await self.behind_the_records(tamper)
                found = await self.findings()
                self.assertEqual([f.record for f in found], [record])
                self.assertIn(words, found[0].problem)

    async def test_an_edited_entry_is_found(self) -> None:
        await self.write(stop)
        await self.write(lambda w: stop(w, scope="space", scope_id=""))
        await self.write(lambda w: stop(w, scope="thread", scope_id=THREAD))
        await self.behind_the_records("DROP TRIGGER ledger_no_update",
                                      "UPDATE ledger SET actor = 'cse_someone' WHERE seq = 2")
        self.assertEqual([(f.seq, f.record) for f in await self.findings()], [(2, "intent:space::run")])

        await self.fresh()
        for fn in (stop, lambda w: stop(w, scope="space", scope_id=""), lambda w: stop(w, scope="thread",
                                                                                         scope_id=THREAD)):
            await self.write(fn)
        await self.behind_the_records("DROP TRIGGER ledger_no_update",
                                      f"UPDATE ledger SET sha256 = '{'1' * 64}' WHERE seq = 2")
        found = await self.findings()
        self.assertEqual([f.seq for f in found], [2, 3])        # its own hash, and the next entry's link to it

    async def test_a_removed_entry_is_found_wherever_it_was(self) -> None:
        for removed in (1, 2, 3):
            with self.subTest(removed=removed):
                await self.fresh()
                await self.write(stop)
                await self.write(lambda w: stop(w, scope="space", scope_id=""))
                await self.write(lambda w: stop(w, scope="thread", scope_id=THREAD))
                await self.behind_the_records("DROP TRIGGER ledger_no_delete",
                                              f"DELETE FROM ledger WHERE seq = {removed}")
                found = await self.findings()
                self.assertTrue(any("missing" in f.problem for f in found), found)

    async def test_an_entry_after_a_removed_last_one_never_takes_its_place(self) -> None:
        """The ledger's seq is never reused: an entry removed from the end
        stays found after the next change, which would otherwise cover it."""
        await self.write(stop)
        await self.write(lambda w: stop(w, scope="space", scope_id=""))
        await self.behind_the_records("DROP TRIGGER ledger_no_delete", "DELETE FROM ledger WHERE seq = 2")
        await self.write(lambda w: stop(w, scope="thread", scope_id=THREAD))
        self.assertEqual([seq for (seq,) in await self.rows("SELECT seq FROM ledger ORDER BY seq")], [1, 3])
        self.assertTrue(any("missing" in f.problem for f in await self.findings()))


class WriteTests(_Store):
    async def test_set_writes_the_row_its_event_and_its_ledger_entry(self) -> None:
        heard = []
        self.store.on_commit(heard.extend)
        intent = await self.write(lambda w: intents.set_intent(
            w, scope="thread", scope_id=THREAD, key="run", value="paused", until="2026-11-12T18:00:00+00:00",
            cites="cmsg_1"))

        self.assertEqual(intent, Intent("thread", THREAD, "run", "paused", LATER, "cmsg_1", AT))
        self.assertEqual(await self.store.read(intents.load), {("thread", THREAD, "run"): intent})
        [event] = heard
        self.assertEqual((event.type, event.chat_id, event.thread_ts), ("intent", CHAT, "1760518860.000100"))
        self.assertEqual(event.data, {"action": "set", "scope": "thread", "scope_id": THREAD, "key": "run",
                                      "value": "paused", "until": LATER, "cites": "cmsg_1"})
        self.assertEqual(await self.rows("SELECT record, row_sha256 FROM ledger"),
                         [(f"intent:thread:{THREAD}:run", ledger.row_sha256(dataclasses.asdict(intent)))])

    async def test_setting_again_replaces_the_row_and_adds_an_entry(self) -> None:
        await self.write(lambda w: intents.set_intent(w, scope="chat", scope_id=CHAT, key="run", value="paused"))
        stopped = await self.write(stop)
        self.assertEqual(list((await self.store.read(intents.load)).values()), [stopped])
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'intent'"), [(2,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(2,)])

    async def test_clear_removes_the_row_with_its_event_and_entry(self) -> None:
        await self.write(lambda w: stop(w, scope="session", scope_id="cse_plain"))
        heard = []
        self.store.on_commit(heard.extend)
        cleared = await self.write(lambda w: intents.clear_intent(w, scope="session", scope_id="cse_plain",
                                                                  key="run"))
        self.assertTrue(cleared)
        self.assertEqual(await self.store.read(intents.load), {})
        [event] = heard
        self.assertEqual((event.type, event.session_id), ("intent", "cse_plain"))
        self.assertEqual(event.data, {"action": "clear", "scope": "session", "scope_id": "cse_plain", "key": "run",
                                      "value": None, "until": None, "cites": None})
        self.assertEqual(await self.rows("SELECT action, row_sha256 FROM ledger WHERE seq = 2"),
                         [("clear", ledger.REMOVED)])
        self.assertEqual(await self.findings(), [])

    async def test_clearing_what_isnt_set_writes_nothing(self) -> None:
        self.assertFalse(await self.write(lambda w: intents.clear_intent(w, scope="chat", scope_id=CHAT, key="run")))
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'intent'"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(0,)])

    async def test_a_failed_transaction_keeps_none_of_the_three(self) -> None:
        def then_fail(fn: Callable[[Writes], Any]) -> Callable[[Writes], None]:
            def write(w: Writes) -> None:
                fn(w)
                raise RuntimeError("the rest of the change failed")
            return write

        with self.assertRaises(RuntimeError):
            await self.write(then_fail(stop))
        self.assertEqual(await self.store.read(intents.load), {})
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'intent'"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(0,)])

        kept = await self.write(stop)
        with self.assertRaises(RuntimeError):
            await self.write(then_fail(lambda w: intents.clear_intent(w, scope="chat", scope_id=CHAT, key="run")))
        self.assertEqual(list((await self.store.read(intents.load)).values()), [kept])
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'intent'"), [(1,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(1,)])
        self.assertEqual(await self.findings(), [])


class ValidationTests(_Store):
    async def test_an_invalid_scope_key_value_or_time_is_refused_saying_what_is_allowed(self) -> None:
        run, paid = {"key": "run", "value": "stopped"}, {"key": "paid", "value": "never"}
        cases = (
            ("an unknown scope", {"scope": "project", "scope_id": CHAT, **run},
             "space, chat, coordinator, thread, session or pool"),
            ("an unknown key", {"scope": "chat", "scope_id": CHAT, "key": "budget", "value": "stopped"},
             "run or paid"),
            ("a run value", {"scope": "chat", "scope_id": CHAT, "key": "run", "value": "never"}, "stopped or paused"),
            ("a paid value", {"scope": "pool", "scope_id": POOL, "key": "paid", "value": "stopped"}, "never or ask"),
            ("paid on a chat", {"scope": "chat", "scope_id": CHAT, **paid}, "space or pool"),
            ("run on a pool", {"scope": "pool", "scope_id": POOL, **run},
             "space, chat, coordinator, thread or session"),
            ("the space with an id", {"scope": "space", "scope_id": "x", **run}, "''"),
            ("a chat without one", {"scope": "chat", "scope_id": "", **run}, "needs its id"),
            ("a thread without its chat", {"scope": "thread", "scope_id": "1760518860.000100", **run},
             "<chat_id>/<thread_ts>"),
            ("until that isn't a time", {"scope": "chat", "scope_id": CHAT, **run, "until": "tomorrow"},
             "ISO time"),
        )
        for case, kwargs, words in cases:
            with self.subTest(case), self.assertRaises(IntentInvalid) as caught:
                await self.write(lambda w, kwargs=kwargs: intents.set_intent(w, **kwargs))
            self.assertIsInstance(caught.exception, ServiceError)
            self.assertEqual((caught.exception.code, caught.exception.status), ("intent_invalid", 400))
            self.assertIn(words, caught.exception.message)
        with self.assertRaises(IntentInvalid) as caught:
            await self.write(lambda w: intents.clear_intent(w, scope="project", scope_id=CHAT, key="run"))
        self.assertIn("space, chat, coordinator, thread, session or pool", caught.exception.message)
        self.assertEqual(await self.rows("SELECT count(*) FROM intents"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'intent'"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(0,)])

    async def test_the_store_refuses_a_row_the_code_would(self) -> None:
        for values in (f"('project', '{CHAT}', 'run', 'stopped')", f"('chat', '{CHAT}', 'run', 'never')",
                       f"('chat', '{CHAT}', 'paid', 'never')", f"('pool', '{POOL}', 'run', 'stopped')",
                       "('space', 'x', 'run', 'stopped')", "('chat', '', 'run', 'stopped')"):
            with self.subTest(values), self.assertRaises(sqlite3.IntegrityError):
                await self.behind_the_records(f"INSERT INTO intents (scope, scope_id, key, value, set_at) "
                                              f"VALUES {values[:-1]}, '{AT}')")


def meta(role: str, *, chat_id: str | None = CHAT, thread_ts: str | None = None, id: str = "cse_x") -> Meta:
    return Meta(id=id, agent="agent_a", role=role, preset=role, chat_id=chat_id, thread_ts=thread_ts,
                project_pid=None, project="demo")


def row(scope: str, scope_id: str, key: str, value: str, until: str | None = None) -> Intent:
    return Intent(scope, scope_id, key, value, until, None, AT)


def keyed(*rows: Intent) -> dict[tuple[str, str, str], Intent]:
    return {(r.scope, r.scope_id, r.key): r for r in rows}


class ResolutionTests(unittest.TestCase):
    NOW_S = NOW.timestamp()
    THREAD_CHAIN = intents.scope_chain(meta("thread", thread_ts="1760518860.000100", id="cse_t"))

    def test_the_chain_runs_from_the_space_to_the_session(self) -> None:
        space = ("space", "")
        self.assertEqual(self.THREAD_CHAIN, (space, ("chat", CHAT), ("thread", THREAD), ("session", "cse_t")))
        for role in ("project-coordinator", "workspace-coordinator"):
            with self.subTest(role):
                self.assertEqual(intents.scope_chain(meta(role, id="cse_c")),
                                 (space, ("chat", CHAT), ("coordinator", CHAT), ("session", "cse_c")))
        self.assertEqual(intents.scope_chain(meta("plain", chat_id=None, id="cse_p")), (space, ("session", "cse_p")))

    def test_stopped_beats_paused_wherever_each_is(self) -> None:
        state = lambda *rows: intents.run_state(self.THREAD_CHAIN, keyed(*rows), self.NOW_S)  # noqa: E731
        self.assertIsNone(state())
        chat_paused = row("chat", CHAT, "run", "paused")
        thread_stopped = row("thread", THREAD, "run", "stopped")
        self.assertEqual(state(chat_paused), chat_paused)
        self.assertEqual(state(chat_paused, thread_stopped), thread_stopped)
        session_paused, space_stopped = row("session", "cse_t", "run", "paused"), row("space", "", "run", "stopped")
        self.assertEqual(state(session_paused, space_stopped), space_stopped)

    def test_of_equal_holds_the_broadest_decides(self) -> None:
        """It is the one still holding once the narrower is lifted (your
        message in a thread lifts that thread's only)."""
        chat_stopped = row("chat", CHAT, "run", "stopped")
        self.assertEqual(intents.run_state(self.THREAD_CHAIN, keyed(row("thread", THREAD, "run", "stopped"),
                                                                    chat_stopped), self.NOW_S), chat_stopped)

    def test_a_row_whose_until_passed_no_longer_counts(self) -> None:
        lapsed, holding = row("chat", CHAT, "run", "stopped", until=EARLIER), row("thread", THREAD, "run", "paused",
                                                                                   until=LATER)
        self.assertEqual(intents.run_state(self.THREAD_CHAIN, keyed(lapsed, holding), self.NOW_S), holding)
        self.assertIsNone(intents.run_state(self.THREAD_CHAIN, keyed(lapsed), self.NOW_S))

    def test_rows_outside_the_chain_or_of_the_other_key_dont_count(self) -> None:
        others = keyed(row("thread", f"{CHAT}/2.0", "run", "stopped"), row("chat", "chat_other", "run", "stopped"),
                       row("coordinator", CHAT, "run", "stopped"), row("session", "cse_other", "run", "stopped"),
                       row("space", "", "paid", "ask"))
        self.assertIsNone(intents.run_state(self.THREAD_CHAIN, others, self.NOW_S))

    def test_paid_the_pool_row_then_the_space_row_then_never(self) -> None:
        mode = lambda *rows: intents.paid_mode(POOL, keyed(*rows), self.NOW_S)  # noqa: E731
        self.assertIsNone(mode())
        self.assertEqual(intents.PAID_DEFAULT, "never")
        space_ask, pool_never = row("space", "", "paid", "ask"), row("pool", POOL, "paid", "never", until=LATER)
        self.assertEqual(mode(space_ask), space_ask)
        self.assertEqual(mode(space_ask, pool_never), pool_never)
        self.assertEqual(mode(space_ask, row("pool", POOL, "paid", "never", until=EARLIER)), space_ask)
        self.assertIsNone(mode(row("pool", "agent_b:acct-2", "paid", "ask"), row("space", "", "run", "stopped")))


if __name__ == "__main__":
    unittest.main()
