"""An allocated native id may be replaced only before any input could reach it."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm.engine import records
from services.cowork_agent.storm.engine.engine import MAX_FAILED_STARTS
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.store import StoreFailed
from tests.storm_engine_kit import Kit, message


MISSING = "allocated-but-not-durable"


class NativeSession:
    def __init__(self, session_id, native_id, report):
        self.id, self.native_id, self.report = session_id, native_id, report
        self.state = "idle"
        self.queue = asyncio.Queue()
        self.seq = 0
        self.sent = []

    async def emit(self, cls, **fields):
        self.seq += 1
        await self.queue.put(cls(session=self.id, native_id=self.native_id, seq=self.seq,
                                 ts="2026-10-29T00:00:00Z", **fields))

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if isinstance(event, c.SessionClosed):
                return

    async def send(self, user_input):
        self.sent.append(user_input)
        turn_id = "native-turn"
        await self.emit(c.TurnStarted, turn_id=turn_id, origin=user_input.origin)
        await self.emit(c.TurnFinished, turn_id=turn_id, status="completed", text="done")
        await self.emit(c.SessionIdle)
        return turn_id

    async def close(self, grace_s=5):
        if self.state != "closed":
            self.state = "closed"
            await self.emit(c.SessionClosed, reason="closed", exit_code=0)


class MissingRuntime(ClaudeRuntime):
    def __init__(self, home):
        super().__init__(claude_home=home)
        self.opens = []
        self.sessions = []
        self.failure = c.RuntimeFailure("session_not_found", "no durable native history")

    async def open(self, spec, host, *, session_id):
        self.opens.append(spec.resume)
        if spec.resume:
            raise self.failure
        session = NativeSession(session_id, f"new-native-{len(self.sessions) + 1}", self.check(spec))
        self.sessions.append(session)
        await session.emit(c.SessionStarted, cwd=spec.cwd, model=None, cli_version="test")
        return session


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kit = Kit()
        self.kit.scenario([])
        self.runtime = MissingRuntime(self.kit.home / ".claude")
        self.engine = await self.kit.boot_with(self.runtime)
        self.sid = await self.kit.session()

    async def asyncTearDown(self):
        await self.kit.close()

    async def seed(self, state=None, *, turn=False):
        sid = self.sid

        def write(w):
            w.execute("UPDATE sessions SET native_id = ? WHERE id = ?", (MISSING, sid))
            if state:
                w.append("session", {"native_id": MISSING, "native_input": state}, session_id=sid, epoch=0)
            if turn:
                records.ensure_turn(w, records.Ids(sid), "unacknowledged-turn", "injected", 0)
        await self.engine.write(write)

    async def test_missing_never_submitted_id_is_replaced_and_queued_message_runs_once(self):
        await self.seed("never_submitted")
        await self.engine.deliver(self.sid, message(1, "hello"))
        await self.kit.recorder.seen("turn.finished", session_id=self.sid)
        self.assertEqual(self.runtime.opens, [MISSING, None])
        self.assertEqual(len(self.runtime.sessions[0].sent), 1)
        self.assertEqual(self.kit.rows("SELECT native_id FROM sessions"), [("new-native-1",)])
        self.assertEqual(self.kit.rows("SELECT state FROM wakes"), [("consumed",)])

    async def test_legacy_or_possibly_submitted_or_history_bearing_ids_are_preserved(self):
        for state, turn in ((None, False), ("possibly_submitted", False), ("never_submitted", True)):
            with self.subTest(state=state, turn=turn):
                # Reset only this fixture's evidence between cases.
                await self.engine.write(lambda w: w.execute("DELETE FROM events WHERE session_id = ? "
                                                            "AND type = 'session'", (self.sid,)))
                await self.seed(state, turn=turn)
                before = len(self.runtime.opens)
                self.assertIsNone(await self.engine._attach(self.sid))
                self.assertEqual(self.runtime.opens[before:], [MISSING])
                self.assertEqual(self.kit.rows("SELECT native_id FROM sessions"), [(MISSING,)])

    async def test_unrelated_open_error_never_replaces_a_native_id(self):
        await self.seed("never_submitted")
        self.runtime.failure = c.RuntimeFailure("protocol", "permission denied opening history")
        self.assertIsNone(await self.engine._attach(self.sid))
        self.assertEqual(self.runtime.opens, [MISSING])

    async def test_an_attach_by_an_older_build_invalidates_earlier_empty_proof(self):
        await self.seed("never_submitted")
        await self.engine.write(lambda w: w.append("session", {
            "reason": "attached", "native_id": MISSING}, session_id=self.sid, epoch=0))
        self.assertIsNone(await self.engine._attach(self.sid))
        self.assertEqual(self.runtime.opens, [MISSING])

    async def test_allocation_proof_survives_close_and_fresh_attach(self):
        live = await self.engine._attach(self.sid)
        await live.close("test_before_first_send")
        self.assertTrue(await self.engine.store.read(
            lambda conn: records.native_never_submitted(conn, self.sid, "new-native-1")))
        second = await self.engine._attach(self.sid)
        self.assertIsNotNone(second)
        self.assertEqual(self.runtime.opens, [None, "new-native-1", None])
        self.assertEqual(second.native_id, "new-native-2")

    async def test_allocation_proof_survives_engine_restart(self):
        await self.engine._attach(self.sid)
        await storm_engine.shutdown()
        self.engine = await self.kit.boot_with(self.runtime)
        await self.engine.deliver(self.sid, message(1, "hello after restart"))
        await self.kit.recorder.seen("turn.finished", session_id=self.sid)
        self.assertEqual(self.runtime.opens, [None, "new-native-1", None])
        self.assertEqual(len(self.runtime.sessions[1].sent), 1)

    async def test_send_boundary_is_committed_before_cli_even_when_acknowledgement_is_lost(self):
        live = await self.engine._attach(self.sid)

        async def lost_ack(user_input):
            self.assertFalse(await self.engine.store.read(
                lambda conn: records.native_never_submitted(conn, self.sid, live.native_id)))
            self.assertEqual(self.kit.rows("SELECT count(*) FROM turns"), [(0,)])
            raise c.RuntimeFailure("process_died", "lost acknowledgement")
        with mock.patch.object(live.session, "send", side_effect=lost_ack):
            with self.assertRaises(c.RuntimeFailure):
                await live.send(c.UserInput(text="hello"), [])
        await live.close("lost_ack")
        self.assertIsNone(await self.engine._attach(self.sid))
        self.assertEqual(self.runtime.opens, [None, "new-native-1"])

    async def test_failed_boundary_write_prevents_any_input_reaching_cli(self):
        live = await self.engine._attach(self.sid)
        with mock.patch.object(live, "write", side_effect=StoreFailed("disk full")):
            with self.assertRaises(StoreFailed):
                await live.send(c.UserInput(text="hello"), [])
        self.assertEqual(live.session.sent, [])
        self.assertTrue(await self.engine.store.read(
            lambda conn: records.native_never_submitted(conn, self.sid, live.native_id)))

    async def test_transient_readiness_failure_retries_before_submitting_queued_input(self):
        original = self.runtime.open
        attempts = []

        async def opening(spec, host, *, session_id):
            attempts.append(1)
            if len(attempts) < 3:
                raise c.RuntimeFailure("protocol", "waiting for conversation tools", retryable=True)
            return await original(spec, host, session_id=session_id)
        with mock.patch.object(self.runtime, "open", side_effect=opening):
            await self.engine.deliver(self.sid, message(1, "hello"))
            async with self.kit.pacing():
                await self.kit.recorder.seen("turn.finished", session_id=self.sid)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.runtime.sessions[0].sent), 1)
        self.assertEqual(self.kit.rows("SELECT state FROM wakes"), [("consumed",)])

    async def test_persistent_readiness_failure_stops_after_bounded_attempts(self):
        failure = c.RuntimeFailure("protocol", "waiting for conversation tools", retryable=True)
        with mock.patch.object(self.runtime, "open", side_effect=failure) as opening:
            await self.engine.deliver(self.sid, message(1, "hello"))
            async with self.kit.pacing():
                await self.kit.recorder.seen("session", session_id=self.sid, reason="start_failed")
            self.assertEqual(opening.await_count, MAX_FAILED_STARTS)
        self.assertEqual(self.kit.rows("SELECT state FROM wakes"), [("queued",)])
