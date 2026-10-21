"""Storm's engine: an input ends at the runtime's ``SessionIdle``, not when
the engine sees no turn running (storm PR 20; ch 07 §2, §4.2, §5.2, §7, §8;
ch 01 §5.2; ch 02 §4.3). claude's worker gap, between a background worker's
end and claude's own second turn, is part of the input: the session is busy
in it, so neither the idle release, the memory guard, a reconnect recycle
nor a pending effort change closes it, and admission hands it nothing as if
it were idle. On the fake claude, which gives one input two results the way
the live capture (B2 ``s16_5``) shows: no idle after the first.
"""

from __future__ import annotations

import unittest
import unittest.mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import engine as engine_module
from services.cowork_agent.storm.engine import session as session_module
from services.cowork_agent.storm.engine.memory import MemoryReading
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message

#: How long the fake waits between the worker's end and claude's own turn.
GAP_S = 2.0


def started(*after: dict) -> list[dict]:
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


def worker_finished() -> list[dict]:
    """The input's first result, with a background worker that then ends:
    the gap opens (claude sends no idle here)."""
    return [claude.task_started(SID, "task-1"), *claude.text_turn(SID, "Asked a worker.", message_id="m_w"),
            claude.system(SID, "task_notification", task_id="task-1", status="completed", summary="read")]


def own_turn(*inside: dict) -> list[dict]:
    """claude's own second turn (``inside`` runs in it before its result), then its idle."""
    return [*claude.streamed(SID, "m2", [("text", "The worker is back.")]), *inside,
            claude.result(SID, "The worker is back.", origin="task-notification"), claude.idle(SID)]


def gap(*then: dict) -> list[dict]:
    return started(*worker_finished(), {"sleep": GAP_S}, *own_turn(), *then)


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    async def in_the_gap(self, sid: str) -> None:
        """The worker ended and claude's own turn hasn't started."""
        await self.kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual(len(self.kit.recorder.of("turn.started", sid)), 1, "the gap closed before the test acted")

    def statuses(self, sid: str) -> list[tuple[str, str | None]]:
        return [(e.data["status"], e.data.get("reason")) for e in self.kit.recorder.of("session", sid)]

    def origins(self, sid: str) -> list[str]:
        return [e.data["origin"] for e in self.kit.recorder.of("turn.started", sid)]


class GapTests(Case):
    async def test_claudes_own_turn_after_the_idle_release_and_the_memory_guard_still_runs(self) -> None:
        kit = self.kit
        kit.scenario(gap())
        engine = await kit.boot()
        sid, other = await kit.session(), await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        self.assertEqual((await engine.get_session(sid))["state"], "running")     # busy, as the person sees it
        kit.clock.advance(engine_module.IDLE_RELEASE_S + 1)                     # the idle release's chance
        kit.reading = MemoryReading(pressure_avg10=100.0)                       # and the memory guard's:
        kit.pace()
        await engine.deliver(other, message(2))                                 # a new process needs memory
        await kit.recorder.seen("session", session_id=other, status="queued", reason=engine_module.MEMORY_WAIT)
        self.assertEqual(len(kit.recorder.of("turn.started", sid)), 1)

        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual((self.origins(sid), second.data["status"]), (["person", "injected"], "finished"))
        answer = [e for e in kit.recorder.of("step.text", sid) if e.turn_id == second.turn_id]
        self.assertEqual([e.data["text"] for e in answer], ["The worker is back."])
        # Released by the guard only once its input ended, never in the gap.
        released = await kit.recorder.seen("session", session_id=sid, status="cold", reason="memory")
        self.assertGreater(released.seq, second.seq)
        self.assertEqual(self.statuses(sid)[2:], [("warm", "attached"), ("running", None), ("warm", None),
                                                  ("cold", "memory")])
        self.assertEqual(len(kit.runs()), 1)

    async def test_the_idle_release_counts_from_the_inputs_end(self) -> None:
        kit = self.kit
        kit.scenario(gap())
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        self.assertFalse(any(key.startswith("cse_") for key in engine._timers))   # no release armed in the gap
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        await kit.idle_timer()
        kit.clock.advance(engine_module.IDLE_RELEASE_S)
        await kit.recorder.seen("session", session_id=sid, status="cold", reason="idle")
        self.assertEqual(len(kit.recorder.of("turn.finished", sid)), 2)

    async def test_a_wake_in_the_gap_waits_for_the_inputs_end(self) -> None:
        """ch 07 §4.2: a wake for a busy session waits for its next turn; in
        the gap that is after claude's own turn, never into the gap as a new
        input of its own."""
        kit = self.kit
        kit.scenario(gap(claude.user_message(), *claude.text_turn(SID, "second", message_id="m3"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        await engine.deliver(sid, message(2))
        third = await kit.recorder.seen("turn.started", session_id=sid, count=3)
        self.assertEqual(self.origins(sid), ["person", "injected", "person"])
        self.assertEqual(kit.rows("SELECT turn_id FROM wakes WHERE id = 'msg:cmsg_2'"), [(third.turn_id,)])
        own = kit.recorder.of("turn.finished", sid)[1]
        self.assertLess(own.seq, third.seq)

    async def test_an_add_in_the_gap_joins_claudes_own_turn(self) -> None:
        """"Add to this turn" with no turn yet: it goes in when claude's own
        turn starts, steering it (ch 07 §4.2), not as an input of its own."""
        kit = self.kit
        kit.scenario(started(*worker_finished(), {"sleep": GAP_S}, *own_turn(claude.user_message())))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        await engine.deliver(sid, message(2, delivery="add"))
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(self.origins(sid), ["person", "injected"])
        self.assertEqual(kit.rows("SELECT turn_id, state FROM wakes WHERE id = 'msg:cmsg_2'"),
                         [(second.turn_id, "consumed")])

    async def test_a_reconnect_recycle_waits_for_the_inputs_end(self) -> None:
        kit = self.kit
        kit.scenario(gap())
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        await engine.credentials_changed(engine._live[sid])          # Connect Claude, ch 07 §8
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        recycled = await kit.recorder.seen("session", session_id=sid, status="cold", reason="recycle")
        self.assertGreater(recycled.seq, second.seq)
        self.assertEqual(second.data["status"], "finished")

    async def test_an_effort_change_waits_for_the_inputs_end(self) -> None:
        kit = self.kit
        kit.scenario(gap())
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        await engine.update(sid, effort="high")
        self.assertEqual(engine._live[sid].pending_effort, "high")
        second = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        self.assertEqual(second.data["status"], "finished")
        await kit.until(lambda: len(kit.runs()) == 2)                # restarted after it, resumed, with the effort
        argv = kit.runs()[1]["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")


class WorkerRunningTests(Case):
    """After the input's first result a worker still runs (claude sends no
    idle then, B2 ``s16_5``: the worker ran 24 events past the result): the
    session is busy, and takes input as before; only the gap after the
    worker's end holds input back."""

    def first_result_with_a_worker(self) -> list[dict]:
        return [claude.task_started(SID, "task-1"), *claude.text_turn(SID, "Asked a worker.", message_id="m_w")]

    async def test_a_message_while_the_worker_runs_is_sent_as_its_own_turn(self) -> None:
        kit = self.kit
        kit.scenario(started(*self.first_result_with_a_worker(), claude.user_message(),
                             *claude.text_turn(SID, "Still reading.", message_id="m3"),
                             claude.system(SID, "task_notification", task_id="task-1", status="completed",
                                           summary="read"), *own_turn()))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((await engine.get_session(sid))["live_runs"], 1)
        await engine.deliver(sid, message(2))
        second = await kit.recorder.seen("turn.started", session_id=sid, count=2)
        self.assertEqual(second.data["origin"], "person")
        self.assertEqual(kit.rows("SELECT turn_id FROM wakes WHERE id = 'msg:cmsg_2'"), [(second.turn_id,)])
        await kit.recorder.seen("turn.finished", session_id=sid, count=3)          # then claude's own turn
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertEqual(self.origins(sid), ["person", "person", "injected"])

    async def test_a_stop_while_the_worker_runs_lets_claudes_own_turn_finish(self) -> None:
        """Stopping the worker wakes claude's own turn (ch 02 §4.5; live
        e2e5 took ~30 s to go idle): it runs to its end, and isn't closed."""
        kit = self.kit
        kit.scenario(started(*self.first_result_with_a_worker(), *claude.host_control("stop_task"),
                             *claude.task_killed(SID, "task-1"), {"sleep": 0.3},
                             *claude.streamed(SID, "m2", [("text", "The worker was stopped.")]), {"sleep": 0.5},
                             claude.result(SID, "The worker was stopped.", origin="task-notification"),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue((await engine.stop(sid, by="person")).stopped)
        await kit.recorder.seen("run.finished", session_id=sid, status="stopped")
        await kit.recorder.seen("turn.started", session_id=sid, count=2)
        kit.clock.advance(2 * session_module.STOP_GRACE_S)
        own = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertEqual(own.data["status"], "finished")
        self.assertEqual(len(kit.runs()), 1)
        self.assertNotIn(("cold", session_module.STOP_CLOSE), self.statuses(sid))


class StopInTheGapTests(Case):
    async def test_a_stop_in_the_gap_is_claudes_own_stop_and_the_input_ends_at_its_idle(self) -> None:
        kit = self.kit
        kit.scenario(started(*worker_finished(), *claude.host_control("interrupt"), claude.idle(SID),
                             {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        result = await engine.stop(sid, by="person")
        self.assertEqual((result.stopped, result.turn_id), (True, None))
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertEqual(self.statuses(sid)[3:], [("running", None), ("stopping", "stop"), ("warm", None)])
        sent = [line["request"]["subtype"] for run in kit.runs() for line in run["stdin"] if "request" in line]
        self.assertEqual(sent, ["initialize", "interrupt"])
        await kit.idle_timer()

    async def test_a_stop_in_the_gap_that_claude_never_ends_closes_its_process(self) -> None:
        kit = self.kit
        kit.scenario(started(*worker_finished(), *claude.host_control("interrupt"), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        self.assertTrue((await engine.stop(sid, by="person")).stopped)
        self.assertEqual((await engine.get_session(sid))["state"], "stopping")
        kit.clock.advance(session_module.STOP_GRACE_S)
        await kit.recorder.seen("session", session_id=sid, status="cold", reason=session_module.STOP_CLOSE)
        await kit.until(lambda: sid not in engine._live)


    async def test_a_turn_claude_starts_after_a_stopped_turn_runs_and_is_not_closed(self) -> None:
        """A Stop of a running turn leaves the session resumable and warm
        (ch 07 §7): a turn the CLI then starts on its own is new work, shown
        running, and the input ends at its idle."""
        kit = self.kit
        kit.scenario(started(claude.tool_call(SID, "t1", "Bash", {"command": "make test"}),
                             *claude.host_control("interrupt"),
                             claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_tools"),
                             {"sleep": 0.3}, *claude.streamed(SID, "m2", [("text", "Stopped, as asked.")]),
                             {"sleep": 1.0}, claude.result(SID, "Stopped, as asked.", origin="task-notification"),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("step.tool", session_id=sid)
        self.assertTrue((await engine.stop(sid, by="person")).stopped)
        await kit.recorder.seen("turn.started", session_id=sid, count=2)
        kit.clock.advance(2 * session_module.STOP_GRACE_S)
        own = await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        self.assertEqual(own.data["status"], "finished")
        self.assertEqual([e.data["status"] for e in kit.recorder.of("turn.finished", sid)], ["stopped", "finished"])
        self.assertEqual(self.statuses(sid)[3:], [("running", None), ("stopping", "stop"), ("running", None),
                                                  ("warm", None)])
        self.assertEqual(len(kit.runs()), 1)


class EndTests(Case):
    async def test_a_process_that_dies_in_the_gap_ends_the_input(self) -> None:
        kit = self.kit
        kit.scenario(started(*worker_finished(), {"sleep": 0.5}, {"exit": 1}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await self.in_the_gap(sid)
        await kit.recorder.seen("session", session_id=sid, status="cold", count=2)      # after "created"
        async with engine.between_inputs(sid) as between:
            self.assertTrue(between)

    async def test_a_repeated_idle_ends_nothing_and_the_listeners_hear_one_per_input(self) -> None:
        """A runtime that says idle twice: the second is not the end of the
        next input, and the reply gate (a listener) runs once per input."""
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "one"), claude.idle(SID), claude.idle(SID),
                             claude.user_message(), *worker_finished(), {"sleep": GAP_S}, *own_turn()))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
        await engine.deliver(sid, message(2))
        await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual((await engine.get_session(sid))["state"], "running")    # the second input is open
        await kit.recorder.seen("turn.finished", session_id=sid, count=3)
        # Heard in order: input 1's idle, the repeat (were it passed on), input 2's idle.
        await kit.recorder.wait(lambda: sum(isinstance(e, c.SessionIdle) for e in kit.recorder.ephemerals) >= 2)
        self.assertEqual(sum(isinstance(e, c.SessionIdle) for e in kit.recorder.ephemerals), 2)


if __name__ == "__main__":
    unittest.main()
