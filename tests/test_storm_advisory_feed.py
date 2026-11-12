"""The engine tells the space advisory what its turns see: each rate-limit
event, each turn's numbers at its end, and the process each session runs in
until it closes, each under the session's agent and the login it started on
(the advisory's names, never storm's pool id). None of it can fail a turn.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from services.advisory.advisory import Advisory
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.engine import engine as engine_module
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message


def unified(resets_at: float) -> dict:
    """claude's event as 2.1.3xx sends it (recorded 11-11): both windows."""
    return {"emit": {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": int(resets_at), "rateLimitType": "five_hour", "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.41, "resetsAt": int(resets_at), "status": "allowed"},
                           "seven_day": {"utilization": 0.03, "resetsAt": int(resets_at) + 6 * 86400,
                                         "status": "allowed"}}}}}


class EngineFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    async def test_usage_turns_and_processes_reach_the_advisory(self) -> None:
        kit = self.kit
        advisory = Advisory(clock=kit.clock.time)
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      unified(kit.clock.time() + 3600), *claude.text_turn(SID, "ok"), claude.idle(SID)])
        engine = await kit.boot(advisory=advisory)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        snapshot = advisory.snapshot()
        live = engine._live[sid]
        named = (live.meta.agent, live.account)
        self.assertEqual(set(snapshot.accounts), {named})
        windows = snapshot.account(*named).windows
        self.assertEqual({n: w.utilization.value for n, w in windows.items()}, {"five_hour": 0.41, "seven_day": 0.03})
        self.assertEqual(snapshot.account(*named).paid.possible.value, False)
        session = snapshot.sessions[sid]
        self.assertEqual(((session.agent, session.account), session.turn_cost_usd.value), (named, 0.0001))
        self.assertEqual(advisory._sessions[sid].root.pid, engine._live[sid].pid)
        self.assertNotEqual(advisory._sessions[sid].root.pid, os.getpid())
        await storm_engine.shutdown()                       # its process ends: the session isn't live any more
        await kit.until(lambda: sid not in advisory.snapshot().sessions)

    async def test_the_advisory_never_depends_on_how_storm_names_its_pools(self) -> None:
        kit = self.kit
        advisory = Advisory(clock=kit.clock.time)
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      unified(kit.clock.time() + 3600), *claude.text_turn(SID, "ok"), claude.idle(SID)])
        with mock.patch.object(engine_module, "pool_id", lambda agent, account: f"pool/{agent}/{account}"):
            engine = await kit.boot(advisory=advisory)
            sid = await kit.session()
            await engine.deliver(sid, message(1))
            await kit.recorder.seen("turn.finished", session_id=sid)
        live = engine._live[sid]
        self.assertTrue(live.pool.startswith("pool/"))
        found = advisory.snapshot().account(live.meta.agent, live.account)
        self.assertEqual(found.windows["five_hour"].utilization.value, 0.41)

    async def test_a_broken_advisory_never_fails_a_turn(self) -> None:
        kit = self.kit
        advisory = Advisory(clock=kit.clock.time)
        advisory._book = None                     # every publish raises inside the advisory
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      unified(kit.clock.time() + 3600), *claude.text_turn(SID, "ok"), claude.idle(SID)])
        engine = await kit.boot(advisory=advisory)
        sid = await kit.session()
        with self.assertLogs("services.advisory.advisory", "ERROR"):
            await engine.deliver(sid, message(1))
            finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(finished.data["status"], "finished")


if __name__ == "__main__":
    unittest.main()
