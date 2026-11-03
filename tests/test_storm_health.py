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
            "it can't be reached (the CLI isn't on PATH)": await health.check("a", FakeRuntime(fails=True), None),
            "it isn't logged in": await health.check("a", FakeRuntime(logged_in=False), pools({})),
            f"its usage limit is closed until {RESET}":
                await health.check("a", FakeRuntime(), pools({("a", "acct-1"): closed})),
        }
        for why, got in cases.items():
            with self.subTest(why=why):
                self.assertFalse(got.usable)
                self.assertEqual(got.why_not, why)
        self.assertEqual(cases[f"its usage limit is closed until {RESET}"].windows["five_hour"]["state"], "closed")

    async def test_a_warn_pool_is_usable_and_no_engine_means_an_unknown_limit(self) -> None:
        warn = await health.check("a", FakeRuntime(), pools({("a", "acct-1"): Pool("a:acct-1", "warn")}))
        self.assertEqual((warn.usable, warn.limit), (True, "warn"))
        unknown = await health.check("a", FakeRuntime(), None)
        self.assertEqual((unknown.usable, unknown.limit), (True, None))

    async def test_check_all_reads_every_agent(self) -> None:
        got = await health.check_all(["a", "b"], lambda name: FakeRuntime() if name == "a" else None, None)
        self.assertEqual([(h.agent, h.usable) for h in got], [("a", True), ("b", False)])
        self.assertEqual(set(got[0].as_dict()), {"agent", "usable", "why_not", "installed", "logged_in", "account",
                                                 "cli_version", "pool", "limit", "resets_at", "windows", "error",
                                                 "checked_at"})


if __name__ == "__main__":
    unittest.main()
