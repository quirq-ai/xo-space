"""A runtime's health (``storm/health.py``): usable now, and why not."""

from __future__ import annotations

import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import health
from services.cowork_agent.storm.engine.admission import Pool, Window

RESET = "2026-11-03T18:00:00Z"


class FakeRuntime:
    def __init__(self, *, logged_in: bool = True, account: str | None = "acct-1", fails: bool = False) -> None:
        self.logged_in, self.account, self.fails = logged_in, account, fails

    async def describe(self) -> c.RuntimeInfo:
        if self.fails:
            raise c.RuntimeFailure("cli_missing", "the CLI isn't on PATH")
        return c.RuntimeInfo(cli_path="/bin/fake", cli_version="1.2.3", logged_in=self.logged_in,
                             account=self.account)


def pools(found: dict[tuple[str, str | None], Pool]):
    return lambda agent, account: found.get((agent, account))


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_logged_in_runtime_with_an_open_pool_is_usable(self) -> None:
        got = await health.check("a", FakeRuntime(), pools({}))
        self.assertTrue(got.usable)
        self.assertEqual((got.limit, got.account, got.cli_version, got.why_not), ("open", "acct-1", "1.2.3", None))

    async def test_each_reason_it_is_not_usable(self) -> None:
        closed = Pool("a:acct-1", "closed", RESET, windows={"five_hour": Window("closed", RESET, RESET)})
        cases = {
            "it has no storm runtime": await health.check("a", None, pools({})),
            "it isn't installed (the CLI isn't on PATH)": await health.check("a", FakeRuntime(fails=True), None),
            "it isn't logged in": await health.check("a", FakeRuntime(logged_in=False), pools({})),
            f"its usage limit is closed until {RESET}":
                await health.check("a", FakeRuntime(), pools({("a", "acct-1"): closed})),
        }
        for why, got in cases.items():
            with self.subTest(why=why):
                self.assertFalse(got.usable)
                self.assertEqual(got.why_not, why)
        self.assertEqual(cases[f"its usage limit is closed until {RESET}"].windows["five_hour"]["state"], "closed")

    async def test_a_missing_cli_is_not_installed_and_another_failure_is_unreachable(self) -> None:
        missing = await health.check("a", FakeRuntime(fails=True), None)
        self.assertEqual((missing.installed, missing.error["code"]), (False, "cli_missing"))

        class Broken(FakeRuntime):
            async def describe(self):
                raise c.RuntimeFailure("timeout", "it crashed")
        broken = await health.check("a", Broken(), None)
        self.assertEqual((broken.installed, broken.why_not), (True, "it can't be reached (it crashed)"))

    async def test_a_warn_pool_is_usable_and_no_engine_means_an_unknown_limit(self) -> None:
        warn = await health.check("a", FakeRuntime(), pools({("a", "acct-1"): Pool("a:acct-1", "warn")}))
        self.assertEqual((warn.usable, warn.limit), (True, "warn"))
        unknown = await health.check("a", FakeRuntime(), None)
        self.assertEqual((unknown.usable, unknown.limit), (True, None))

    async def test_each_windows_share_used_comes_from_the_advisory(self) -> None:
        from services.advisory.advisory import Advisory
        found = Advisory(clock=lambda: 1_794_400_000.0)            # 2026-11-11T12:26:40Z
        found._book.record_read(("a", "acct-1"), c.UsageSnapshot(account="acct-1", source="measured", windows=(
            c.UsageWindow(name="five_hour", utilization=0.51, resets_at="2026-11-12T01:40:00.198816+00:00"),
            c.UsageWindow(name="seven_day", status="warning", utilization=0.08, resets_at="2026-11-17T18:00:00Z"),
            c.UsageWindow(name="seven_day_opus", status=None, utilization=0.2, resets_at="2026-11-17T18:00:00Z"))),
            1_794_400_000.0)
        found._publish()
        pool = Pool("a:acct-1", "open", windows={"five_hour": Window("open", "2026-11-12T01:40:00Z",
                                                                     "2026-11-11T12:00:00Z")})
        got = await health.check("a", FakeRuntime(), pools({("a", "acct-1"): pool}), advisory=found)
        self.assertEqual(got.windows["five_hour"], {
            "state": "open", "resets_at": "2026-11-12T01:40:00Z", "since": "2026-11-11T12:00:00Z",
            "utilization": 0.51, "source": "measured", "observed_at": "2026-11-11T12:26:40Z"})
        self.assertEqual((got.windows["seven_day"]["state"], got.windows["seven_day"]["utilization"]), ("warn", 0.08))
        opus = got.windows["seven_day_opus"]                       # the vendor gave a share used, no status
        self.assertEqual((opus["state"], opus["utilization"], opus["source"]), ("open", 0.2, "measured"))
        self.assertEqual(got.limit, "open")                          # the pool's state stays the engine's
        alone = await health.check("a", FakeRuntime(), pools({("a", "acct-1"): pool}), advisory=Advisory())
        self.assertEqual(alone.windows["five_hour"]["source"], "pool")
        # An hour on, with nothing published since: the advisory's facts are too old, the pool's stand.
        later = health.windows(pool, found.snapshot().account("a", "acct-1"), now_s=1_794_400_000.0 + 3600)
        self.assertEqual(set(later), {"five_hour"})
        self.assertEqual(later["five_hour"]["source"], "pool")

    async def test_a_windows_source_is_its_share_useds(self) -> None:
        from services.advisory.advisory import Advisory
        found = Advisory(clock=lambda: 1_794_400_000.0)
        found.record_usage("a", "acct-1", c.RateLimit(session="s", ts="t", seq=1, status="warning",
                                                      window="weekly", resets_at="2026-11-17T18:00:00Z"))
        found._book.record_read(("a", "acct-1"), c.UsageSnapshot(account="acct-1", source="measured", windows=(
            c.UsageWindow(name="weekly", status=None, utilization=0.93, resets_at="2026-11-17T18:00:00Z"),)),
            1_794_400_000.0)
        found._publish()
        got = health.windows(None, found.snapshot().account("a", "acct-1"), now_s=1_794_400_010.0)
        self.assertEqual((got["weekly"]["state"], got["weekly"]["utilization"], got["weekly"]["source"]),
                         ("warn", 0.93, "measured"))

    async def test_check_all_reads_every_agent(self) -> None:
        got = await health.check_all(["a", "b"], lambda name: FakeRuntime() if name == "a" else None, None)
        self.assertEqual([(h.agent, h.usable) for h in got], [("a", True), ("b", False)])
        self.assertEqual(set(got[0].as_dict()), {"agent", "usable", "why_not", "installed", "logged_in", "account",
                                                 "cli_version", "pool", "limit", "resets_at", "windows", "error",
                                                 "checked_at"})


if __name__ == "__main__":
    unittest.main()
