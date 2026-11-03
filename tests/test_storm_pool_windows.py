"""A pool's usage windows (``engine/admission.py``): the pool keeps each
window's state and stands at the worst, and a milder report racing a limit
doesn't undo it.

The replay cases follow what the store recorded on 2026-10-27/28: claude's
processes sharing one account reported the 5-hour window as warning and
limited within the same second, and a 5-hour "allowed" overwrote a 7-day
warning, so the pool flipped state several times a second and forgot the
7-day window.
"""

from __future__ import annotations

import json
import unittest

from services.cowork_agent.storm.engine import admission
from services.cowork_agent.storm.engine.admission import SETTLE_S, Window, report, standing
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message

T0 = 1_793_000_000.0
FIVE_H = "2026-10-28T01:00:00Z"
SEVEN_D = "2026-11-03T18:00:00Z"


def rate_limit(status: str, window: str, resets_at: float, utilization: float) -> dict:
    """claude's ``rate_limit_event`` with its own utilization (the fake's
    helper always reports 90 %, which warns whatever the window)."""
    return {"emit": {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": window, "isUsingOverage": False, "utilization": utilization,
        "resetsAt": resets_at}}}


def run(reports: list[tuple[float, str, str, str | None]]) -> list[str]:
    """The pool's state after each report ``(at, window, state, resets_at)``,
    without repeats: the transitions a pool event would record."""
    windows: dict[str, Window] = {}
    states: list[str] = []
    for at, window, state, resets in reports:
        windows = report(windows, window, state, resets, T0 + at)
        now, _ = standing(windows, T0 + at)
        if not states or states[-1] != now:
            states.append(now)
    return states


class ReplayTests(unittest.TestCase):
    def test_racing_reports_at_the_limit_settle_on_closed(self) -> None:
        # 22:03:36 warning, then within two seconds warning and limited interleaved (Oct 27).
        states = run([(0, "five_hour", "warn", FIVE_H), (67, "five_hour", "closed", FIVE_H),
                      (68, "five_hour", "warn", FIVE_H), (69, "five_hour", "closed", FIVE_H),
                      (69, "five_hour", "warn", FIVE_H), (69, "five_hour", "closed", FIVE_H)])
        self.assertEqual(states, ["warn", "closed"])

    def test_one_window_never_undoes_another(self) -> None:
        # The 7-day window at warning, the 5-hour one allowed: the pool stays warn (Oct 28, 06:14).
        states = run([(0, "seven_day", "warn", SEVEN_D), (1, "five_hour", "open", FIVE_H),
                      (5, "seven_day", "warn", SEVEN_D), (5, "five_hour", "open", FIVE_H)])
        self.assertEqual(states, ["warn"])

    def test_a_limit_lifted_later_than_the_settle_time_is_believed(self) -> None:
        # 21:50:49 limited, 21:51:55 allowed for the same reset, and turns went on finishing (Oct 27).
        states = run([(0, "five_hour", "closed", FIVE_H), (66, "five_hour", "open", FIVE_H)])
        self.assertEqual(states, ["closed", "open"])


class WindowTests(unittest.TestCase):
    def test_a_milder_report_within_the_settle_time_is_dropped(self) -> None:
        windows = report({}, "five_hour", "closed", FIVE_H, T0)
        self.assertEqual(report(windows, "five_hour", "open", FIVE_H, T0 + SETTLE_S - 1), windows)
        later = report(windows, "five_hour", "open", FIVE_H, T0 + SETTLE_S)
        self.assertEqual(later["five_hour"].state, "open")

    def test_a_new_reset_is_a_new_window_at_once(self) -> None:
        windows = report({}, "five_hour", "closed", FIVE_H, T0)
        fresh = report(windows, "five_hour", "open", "2026-10-28T06:00:00Z", T0 + 1)
        self.assertEqual(fresh["five_hour"].state, "open")

    def test_a_worse_report_is_taken_at_once_and_keeps_its_since(self) -> None:
        windows = report({}, "five_hour", "warn", FIVE_H, T0)
        closed = report(windows, "five_hour", "closed", FIVE_H, T0 + 1)
        self.assertEqual(closed["five_hour"].state, "closed")
        again = report(closed, "five_hour", "closed", FIVE_H, T0 + 9)
        self.assertEqual(again["five_hour"].since, closed["five_hour"].since)

    def test_a_named_report_settles_an_unnamed_limit_once_it_is_old_enough(self) -> None:
        windows = report({}, None, "closed", FIVE_H, T0)
        self.assertIn(admission.UNNAMED, report(windows, "five_hour", "open", FIVE_H, T0 + 1))
        self.assertNotIn(admission.UNNAMED, report(windows, "five_hour", "open", FIVE_H, T0 + SETTLE_S))

    def test_standing_is_the_worst_window_until_its_last_reset(self) -> None:
        windows = {"five_hour": Window("closed", FIVE_H, FIVE_H), "seven_day": Window("closed", SEVEN_D, FIVE_H),
                   "other": Window("warn", FIVE_H, FIVE_H)}
        self.assertEqual(standing(windows, T0), ("closed", SEVEN_D))
        windows["unnamed"] = Window("closed", None, FIVE_H)
        self.assertEqual(standing(windows, T0), ("closed", None))     # one with no known reset

    def test_a_window_past_its_reset_no_longer_counts(self) -> None:
        reset_s = admission.parse_ts(FIVE_H).timestamp()
        windows = {"five_hour": Window("closed", FIVE_H, FIVE_H)}
        self.assertEqual(standing(windows, reset_s - 1)[0], "closed")
        self.assertEqual(standing(windows, reset_s), ("open", None))
        self.assertEqual(admission.next_reset(windows, reset_s + 10), FIVE_H)   # looked at again at once

    def test_paid_usage_the_person_continued_counts_as_open(self) -> None:
        windows = report({}, "five_hour", "paid", FIVE_H, T0)
        self.assertEqual(standing(windows, T0)[0], "paid")
        self.assertEqual(standing(windows, T0, paid_continue_until=FIVE_H)[0], "open")
        warned = report(windows, "five_hour", "warn", FIVE_H, T0 + 1, paid_continue_until=FIVE_H)
        self.assertEqual(standing(warned, T0 + 1, paid_continue_until=FIVE_H)[0], "warn")

    def test_an_overloaded_service_makes_an_open_pool_warn(self) -> None:
        self.assertEqual(standing({}, T0, overloaded=True), ("warn", None))

    def test_windows_round_trip_through_the_snapshot_and_skip_garbage(self) -> None:
        windows = report(report({}, "five_hour", "closed", FIVE_H, T0, 0.97), "seven_day", "warn", SEVEN_D, T0)
        stored = json.loads(json.dumps({"windows": {k: w.as_dict() for k, w in windows.items()},
                                        "status": "limited"}))
        stored["windows"]["bad"] = {"state": "nope", "since": "x"}
        self.assertEqual(admission.windows_of(stored), windows)
        self.assertEqual(admission.windows_of(None), {})
        self.assertEqual(admission.windows_of({"status": "ok"}), {})        # a snapshot from before windows


class EngineWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    async def test_a_five_hour_report_never_clears_a_seven_day_warning(self) -> None:
        kit = self.kit
        seven_day = kit.clock.time() + 7 * 86400
        kit.scenario(self.started(rate_limit("allowed_warning", "seven_day", seven_day, 0.85),
                                  rate_limit("allowed", "five_hour", kit.clock.time() + 3600, 0.2),
                                  *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        pool = engine._pools[next(iter(engine._pools))]
        self.assertEqual((pool.state, sorted(pool.windows)), ("warn", ["five_hour", "seven_day"]))
        self.assertEqual([e.data["state"] for e in kit.recorder.of("pool")], ["warn"])

    async def test_the_windows_are_stored_and_read_back_at_boot(self) -> None:
        kit = self.kit
        resets = kit.clock.time() + 3600
        kit.scenario(self.started(claude.rate_limit("rejected", window="five_hour", resets_at=resets),
                                  *claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        (snapshot,) = kit.rows("SELECT snapshot FROM pools")
        self.assertEqual(json.loads(snapshot[0])["windows"]["five_hour"]["state"], "closed")
        from services.cowork_agent.storm import engine as storm_engine
        await storm_engine.shutdown()
        engine = await kit.boot()
        pool = engine._pools[next(iter(engine._pools))]
        self.assertEqual((pool.state, pool.windows["five_hour"].state), ("closed", "closed"))
        await kit.until(lambda: any(key.startswith("pool:") for key in engine._timers))   # reopens at its reset


if __name__ == "__main__":
    unittest.main()
