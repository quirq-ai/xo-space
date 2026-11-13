"""A session a gate holds back (a usage limit, the paid gate, near a limit,
memory, the founder) is ``queued`` with no process: it isn't busy, and its
view, its ``session`` event and its thread say what it waits for and until
when. A retired session never waits: nothing admits it again."""

from __future__ import annotations

import unittest

from services.advisory import Paid
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.chats import rows, thread_status
from services.cowork_agent.storm.engine import Wake, admission, records
from services.cowork_agent.storm.engine.admission import Pool, Wait
from services.cowork_agent.storm.engine.engine import MEMORY
from services.cowork_agent.storm.engine.memory import MemoryReading
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import Kit, message

RESET = "2099-01-01T05:00:00Z"
LIMIT = Wait("limit", RESET, "claude:acct", "usage limit reached", f"pool closed until {RESET}")


class WaitValueTests(unittest.TestCase):
    def test_each_pool_hold_is_a_wait_with_its_until_and_its_old_sentence(self) -> None:
        cases = [
            (Pool("p", "closed", RESET, admission.LIMIT),
             ("limit", RESET, "usage limit reached", f"pool closed until {RESET}")),
            (Pool("p", "paid", RESET, admission.PAID_THRESHOLD_REACHED),
             ("paid", RESET, "held at the paid-usage threshold",
              f"pool paid until {RESET}: held at the paid-usage threshold")),
            (Pool("p", "paid", None, admission.PAID_UNKNOWN),
             ("paid", None, admission.SAYS[admission.PAID_UNKNOWN], f"pool paid: {admission.SAYS[admission.PAID_UNKNOWN]}")),
            (Pool("p", "warn", RESET, admission.WARNING),
             ("near_limit", RESET, "near the usage limit: later work waits", f"pool warn until {RESET}")),
            (Pool("p", "warn", None, admission.OVERLOADED),
             ("near_limit", None, "the service is overloaded: later work waits", "pool warn")),
        ]
        for pool, (kind, until, why, reason) in cases:
            with self.subTest(pool=pool.reason):
                wait = pool.wait()
                self.assertEqual((wait.kind, wait.until, wait.pool, wait.why, wait.reason), (kind, until, "p", why, reason))
                self.assertEqual(wait.as_dict(), {"for": kind, "until": until, "pool": "p", "why": why})

    def test_a_queued_event_reads_back_as_its_wait_and_an_older_builds_sentence_too(self) -> None:
        self.assertEqual(Wait.from_event({"status": "queued", "reason": LIMIT.reason, "waiting": LIMIT.as_dict()}),
                         LIMIT)
        legacy = {
            f"pool closed until {RESET}": ("limit", RESET, "usage limit reached"),
            f"pool paid until {RESET}: held while paid usage is in use": ("paid", RESET,
                                                                          "held while paid usage is in use"),
            "pool warn": ("near_limit", None, "pool warn"),
            "waiting for memory": ("memory", None, "waiting for memory"),
            "paused by the founder (chat c1)": ("founder", None, "paused by the founder (chat c1)"),
        }
        for reason, (kind, until, why) in legacy.items():
            with self.subTest(reason=reason):
                wait = Wait.from_event({"status": "queued", "reason": reason})
                self.assertEqual((wait.kind, wait.until, wait.why, wait.reason), (kind, until, why, reason))
        self.assertIsNone(Wait.from_event({"status": "queued"}))

    def test_the_paid_gate_still_holds_as_before(self) -> None:
        """``Pool.wait`` replaced ``Pool.why``; the gate itself is unchanged."""
        seen = admission.Seen(RESET, 0.96, None, "test", None)
        pool = admission.standing("p", {"weekly": seen}, Paid(),
                                  mode="never", answers={}, readable=False, now_s=0.0)
        self.assertEqual((pool.state, pool.wait().kind, pool.wait().until), ("paid", "paid", RESET))


class EngineWaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.kit.scenario([])
        self.kit.reading = MemoryReading(pressure_avg10=100.0)     # no process starts: every session waits on memory

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def status(self, sid: str) -> str:
        return self.kit.rows("SELECT status FROM sessions WHERE id = ?", (sid,))[0][0]

    async def queued(self, engine, sid: str) -> None:
        await engine.deliver(sid, message(1))
        await self.kit.recorder.seen("session", session_id=sid, status="queued")
        await self.kit.until(lambda: engine._queued.get(sid) == MEMORY)

    async def test_the_view_reads_the_wait_from_its_event_once_the_engine_has_forgotten_it(self) -> None:
        engine = await self.kit.boot()
        sid = await self.kit.session()
        await self.queued(engine, sid)
        self.assertEqual((await engine.get_session(sid))["waiting"], MEMORY.as_dict())
        engine._queued.clear()                                     # as after a restart
        view = await engine.get_session(sid)
        self.assertEqual((view["state"], view["busy"], view["waiting"]), ("queued", False, MEMORY.as_dict()))
        # A queued event an older build wrote has only its sentence: it reads back as the wait it said.
        await engine.write(lambda w: records.set_status(w, records.Ids(sid), "queued", 0, {"reason": LIMIT.reason}))
        engine._queued.clear()
        view = (await engine.get_sessions([sid]))[sid]
        self.assertEqual(view["waiting"], {"for": "limit", "until": RESET, "pool": None, "why": "usage limit reached"})
        (listed,) = [s for s in await engine.list_sessions() if s["id"] == sid]
        self.assertEqual(listed["waiting"], view["waiting"])

    async def test_a_later_status_ends_the_wait(self) -> None:
        engine = await self.kit.boot()
        sid = await self.kit.session()
        await self.queued(engine, sid)
        await engine.write(lambda w: records.set_status(w, records.Ids(sid), "cold", 0, {"reason": "test"}))
        await self.kit.until(lambda: sid not in engine._queued)
        view = await engine.get_session(sid)
        self.assertEqual((view["state"], view["busy"], view["waiting"]), ("cold", False, None))

    async def test_a_retired_session_is_never_made_to_wait(self) -> None:
        """A wait the admission pass decided before the session was retired
        doesn't land after it: nothing would ever admit it again."""
        engine = await self.kit.boot()
        sid = await self.kit.session()
        await self.queued(engine, sid)
        await engine.write(lambda w: records.set_status(w, records.Ids(sid), "cold", 0, {"reason": "test"}))
        await engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?", (w.now, sid)))
        await self.kit.until(lambda: sid not in engine._queued)
        await engine._wait(sid, LIMIT)
        self.assertEqual(self.status(sid), "cold")
        self.assertNotIn(sid, engine._queued)

    async def test_the_boot_ends_the_wait_of_a_retired_session_left_queued(self) -> None:
        """As two coordinators retired by an older build on 11-12: retired
        while ``queued``, their wakes long consumed, still busy since."""
        engine = await self.kit.boot()
        stuck, live = await self.kit.session(), await self.kit.session()
        await storm_engine.shutdown()
        self.kit.execute("UPDATE sessions SET status = 'queued', retired_at = '2026-11-12T14:25:19Z', "
                         "retired_reason = 'legacy' WHERE id = ?", (stuck,))
        engine = await self.kit.boot()
        self.assertEqual(self.status(stuck), "cold")
        ended = [e.data for e in self.kit.recorder.of("session", stuck)]
        self.assertEqual(ended[-1], {"status": "cold", "reason": "retired_wait_ended"})
        view = await engine.get_session(stuck)
        self.assertEqual((view["busy"], view["waiting"]), (False, None))
        self.assertEqual(self.status(live), "cold")                 # a live one is left as it is
        await storm_engine.shutdown()
        engine = await self.kit.boot()                              # the next boot finds nothing to end
        self.assertEqual(len(self.kit.recorder.of("session", stuck)), len(ended))

    async def test_the_boot_move_sends_the_retired_session_cold_and_its_successor_runs_its_wakes(self) -> None:
        engine = await self.kit.boot()
        old, new = await self.kit.session(), await self.kit.session()
        await storm_engine.shutdown()
        self.kit.execute("UPDATE sessions SET status = 'queued', retired_at = '2026-11-12T14:22:43Z' WHERE id = ?",
                         (old,))
        self.kit.execute("UPDATE sessions SET predecessor = ? WHERE id = ?", (old, new))
        self.kit.execute("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, created_at) "
                         "VALUES ('review:rv_1:1', ?, 'next', 'review', 'answer', '{\"origin\": \"system\"}', "
                         "'queued', '2026-11-12T14:22:00Z')", (old,))
        engine = await self.kit.boot()
        self.assertEqual(self.kit.rows("SELECT session_id, state FROM wakes"), [(new, "queued")])
        self.assertEqual(self.status(old), "cold")
        await self.kit.until(lambda: engine._queued.get(new) == MEMORY)   # the successor waits for it instead
        self.assertEqual((await engine.get_session(new))["waiting"], MEMORY.as_dict())


class ThreadWaitTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()                       # the memory guard holds every session back
        self.chat = await self.project("held")
        pc = await self.token(await self.coordinator(self.chat))
        self.root = await self.owner_message(self.chat, "Fix the login.")
        made = await self.call(pc, "create_task", message_id=self.root["id"], title="Login", instructions="x",
                               **CHOICE)
        self.sid = made["session_id"]
        self.engine = self.kit.engine
        await self.kit.until(lambda: self.engine._queued.get(self.sid) == MEMORY)

    async def status(self) -> dict:
        return await self.engine.store.read(lambda conn: thread_status.read(
            conn, rows.get_thread(conn, self.chat["id"], self.root["id"]), now=self.kit.clock.time()))

    async def thread(self) -> dict:
        return (await self.http.get(f"/api/chat/chats/{self.chat['id']}/threads/{self.root['id']}")).json()

    async def test_a_pool_held_thread_waits_for_its_reset_and_isnt_busy(self) -> None:
        found = await self.status()
        self.assertEqual((found["activity"], found["state"], found["wait_reason"], found["waiting_until"]),
                         ("waiting", "working", MEMORY.why, None))
        await self.engine._wait(self.sid, LIMIT)
        found = await self.status()
        self.assertEqual((found["activity"], found["state"], found["wait_reason"], found["waiting_until"]),
                         ("waiting", "working", "usage limit reached", RESET))
        # The queued event re-emits the thread's state keys to the stream.
        sent = await self.kit.recorder.seen("thread", wait_reason="usage limit reached")
        self.assertEqual((sent.data["activity"], sent.data["waiting_until"]), ("waiting", RESET))
        thread = await self.thread()
        self.assertEqual((thread["busy"], thread["session"]["busy"], thread["session"]["state"]),
                         (False, False, "queued"))
        self.assertEqual(thread["session"]["waiting"], LIMIT.as_dict())
        (listed,) = (await self.http.get("/api/chat/sessions", params={"thread": self.root["id"]})).json()["sessions"]
        self.assertEqual((listed["busy"], listed["waiting"]), (False, LIMIT.as_dict()))

    async def test_queued_input_with_no_gate_still_says_queued_input(self) -> None:
        await self.engine.write(lambda w: records.set_status(w, records.Ids(self.sid), "cold", 0, {"reason": "test"}))
        found = await self.status()
        self.assertEqual((found["activity"], found["wait_reason"], found["waiting_until"]),
                         ("waiting", "queued input", None))

    async def test_a_removed_projects_queued_session_goes_cold(self) -> None:
        from services import project_management
        self.assertEqual(self.rows("SELECT status FROM sessions WHERE id = ?", (self.sid,)), [("queued",)])
        await project_management.remove_project("held", "held")
        self.assertEqual(self.rows("SELECT status FROM sessions WHERE id = ?", (self.sid,)), [("cold",)])
        view = await self.engine.get_session(self.sid)
        self.assertEqual((view["busy"], view["waiting"]), (False, None))
        retired = [e for e in self.kit.recorder.of("session", self.sid) if e.data.get("reason") == "retired"]
        self.assertEqual(retired[-1].data["status"], "cold")


if __name__ == "__main__":
    unittest.main()
